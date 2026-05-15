#!/usr/bin/env python3
import json
import os
import re
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.environ.get("CODEX_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_PROXY_PORT", "18082"))
MODEL = os.environ.get("CODEX_PROXY_MODEL", "gpt-5.5")
REASONING_EFFORT = os.environ.get("CODEX_PROXY_REASONING_EFFORT", "high")
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/bin/codex")
CODEX_WORKDIR = os.environ.get("CODEX_WORKDIR", "/root")
REQUEST_TIMEOUT = int(os.environ.get("CODEX_PROXY_REQUEST_TIMEOUT", "900"))


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def redact(text):
    text = str(text or "")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def json_response(handler, status, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def content_to_text(content):
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "input_text":
                    parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content or "")


def first_tool(req):
    tools = req.get("tools") or []
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "function":
                fn = tool.get("function") or {}
                if fn.get("name"):
                    return fn.get("name"), fn.get("parameters") or {}
    functions = req.get("functions") or []
    if isinstance(functions, list):
        for fn in functions:
            if isinstance(fn, dict) and fn.get("name"):
                return fn.get("name"), fn.get("parameters") or {}
    return None, None


def json_schema(req):
    response_format = req.get("response_format") or {}
    if isinstance(response_format, dict):
        schema = (response_format.get("json_schema") or {}).get("schema")
        if schema:
            return schema
    return None


def wants_json(req):
    response_format = req.get("response_format") or {}
    if isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}:
        return True
    return bool(req.get("tools") or req.get("functions"))


def build_prompt(req):
    messages = req.get("messages") or []
    system_parts = []
    conversation = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = content_to_text(msg.get("content", ""))
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant", "tool"}:
            conversation.append(f"{role.upper()}:\n{content}")

    if not conversation:
        conversation.append("USER:\nRespond with OK.")

    tool_name, tool_schema = first_tool(req)
    schema = json_schema(req)
    instructions = [
        "You are the internal LLM behind an OpenAI-compatible local proxy for cognee memory.",
        "Return only the requested answer. Do not mention Codex, tools, policies, or this proxy.",
        "Do not execute shell commands. Do not access files. Do not expose secrets.",
    ]
    if wants_json(req):
        instructions.append("Return only valid JSON. Do not use markdown fences. Do not include explanations.")
    if tool_name:
        instructions.append(f"The caller expects OpenAI function/tool arguments for `{tool_name}`. Return JSON arguments only.")
        if tool_schema:
            instructions.append("Function JSON schema:\n" + json.dumps(tool_schema, ensure_ascii=False))
    elif schema:
        instructions.append("Response JSON schema:\n" + json.dumps(schema, ensure_ascii=False))

    return "\n\n".join(
        [
            "[SYSTEM]",
            "\n\n".join(p for p in system_parts if p) or "(no system message)",
            "[/SYSTEM]",
            "[INSTRUCTIONS]",
            "\n".join(instructions),
            "[/INSTRUCTIONS]",
            "[CONVERSATION]",
            "\n\n".join(conversation),
            "[/CONVERSATION]",
        ]
    )


def extract_json_text(text):
    stripped = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.S | re.I)
    if fenced:
        stripped = fenced.group(1).strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
            return stripped
        except Exception:
            pass
    match = re.search(r"(\{.*\}|\[.*\])", stripped, re.S)
    if match:
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass
    return stripped


def parse_codex_jsonl(stdout_text):
    fallback = ""
    for raw_line in (stdout_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            fallback = item.get("text") or fallback
    return fallback


def call_codex(prompt):
    out_path = None
    try:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
            out_path = out_file.name
        cmd = [
            CODEX_BIN,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--model",
            MODEL,
            "-c",
            f'model_reasoning_effort="{REASONING_EFFORT}"',
            "--output-last-message",
            out_path,
            prompt,
        ]
        completed = subprocess.run(
            cmd,
            cwd=CODEX_WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=REQUEST_TIMEOUT,
            check=False,
        )
        stdout_text = redact(completed.stdout or "")
        answer = ""
        try:
            answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            pass
        if not answer:
            answer = parse_codex_jsonl(stdout_text)
        if completed.returncode != 0:
            raise RuntimeError((answer or stdout_text or f"codex exit={completed.returncode}")[-2000:])
        return answer.strip()
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except FileNotFoundError:
                pass


def openai_payload(req, text):
    now = int(time.time())
    text = extract_json_text(text) if wants_json(req) else (text or "").strip()
    tool_name, _ = first_tool(req)
    message = {"role": "assistant", "content": text}
    finish_reason = "stop"
    if tool_name:
        args = text if text else "{}"
        try:
            json.loads(args)
        except Exception:
            args = json.dumps({"value": args}, ensure_ascii=False)
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{now}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": args},
                }
            ],
        }
        finish_reason = "tool_calls"

    prompt_tokens = max(1, len(json.dumps(req.get("messages") or [], ensure_ascii=False)) // 4)
    completion_tokens = max(1, len(text) // 4)
    return {
        "id": f"chatcmpl-codex-{now}",
        "object": "chat.completion",
        "created": now,
        "model": req.get("model") or MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenClawCodexProxy/0.1"

    def log_message(self, fmt, *args):
        print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), fmt % args), flush=True)

    def do_GET(self):
        if self.path in {"/healthz", "/v1/healthz"}:
            json_response(self, 200, {"ok": True, "status": "live", "model": MODEL})
            return
        if self.path == "/v1/models":
            json_response(self, 200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            return
        json_response(self, 404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            json_response(self, 404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            if req.get("stream"):
                json_response(self, 400, {"error": {"message": "streaming is not supported"}})
                return
            answer = call_codex(build_prompt(req))
            json_response(self, 200, openai_payload(req, answer))
        except subprocess.TimeoutExpired:
            json_response(self, 504, {"error": {"message": f"codex proxy timed out after {REQUEST_TIMEOUT}s"}})
        except Exception as exc:
            json_response(self, 500, {"error": {"message": redact(str(exc))[:2000]}})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"codex proxy listening on http://{HOST}:{PORT}/v1 model={MODEL} reasoning={REASONING_EFFORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
