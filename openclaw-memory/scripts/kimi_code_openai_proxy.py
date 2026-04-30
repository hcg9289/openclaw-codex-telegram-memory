#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("KIMI_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("KIMI_PROXY_PORT", "18080"))
MODEL = os.environ.get("KIMI_MODEL", "kimi-for-coding")
BASE_URL = os.environ.get("KIMI_CODE_ENDPOINT", "https://api.kimi.com/coding/v1").rstrip("/")
API_KEY = os.environ.get("KIMI_API_KEY", "")


def _json_response(handler, status, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _extract_json_text(text):
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


def _first_tool(req):
    tools = req.get("tools") or []
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "function":
                fn = tool.get("function") or {}
                name = fn.get("name")
                if name:
                    return name, fn.get("parameters") or {}

    functions = req.get("functions") or []
    if isinstance(functions, list) and functions:
        fn = functions[0] or {}
        name = fn.get("name")
        if name:
            return name, fn.get("parameters") or {}
    return None, None


def _json_schema(req):
    response_format = req.get("response_format") or {}
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema") or {}
        schema = json_schema.get("schema")
        if schema:
            return schema
    return None


def _content_to_text(content):
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(text_parts)
    return str(content or "")


def _to_anthropic_messages(req):
    messages = req.get("messages") or []
    wants_json = bool(req.get("response_format") or req.get("tools") or req.get("functions"))
    tool_name, tool_schema = _first_tool(req)
    response_schema = _json_schema(req)
    system_parts = []
    out = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = _content_to_text(msg.get("content", ""))
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            out.append({"role": role, "content": content})
        elif role == "tool":
            out.append({"role": "user", "content": content})

    if not out:
        out.append({"role": "user", "content": "Respond with OK."})

    if wants_json:
        system_parts.append(
            "Return only valid JSON. Do not use markdown fences. Do not include explanations."
        )
    if tool_name:
        system_parts.append(
            "The caller expects an OpenAI tool/function call. Return JSON arguments only "
            f"for function `{tool_name}`."
        )
        if tool_schema:
            system_parts.append("Function JSON schema:\n" + json.dumps(tool_schema, ensure_ascii=False))
    elif response_schema:
        system_parts.append("Response JSON schema:\n" + json.dumps(response_schema, ensure_ascii=False))

    return "\n\n".join(p for p in system_parts if p), out


def _requested_model(req):
    requested = str(req.get("model") or MODEL)
    if "/" in requested:
        requested = requested.split("/", 1)[1]
    return requested or MODEL


def _call_kimi(req):
    system, messages = _to_anthropic_messages(req)
    max_tokens = req.get("max_tokens") or req.get("max_completion_tokens") or 2048
    payload = {
        "model": _requested_model(req),
        "max_tokens": min(int(max_tokens), 8192),
        "messages": messages,
    }
    if system:
        payload["system"] = system

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/messages",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-code/0.1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _openai_payload(req, kimi):
    text = ""
    for part in kimi.get("content", []):
        if isinstance(part, dict) and part.get("type") == "text":
            text += part.get("text", "")
    text = _extract_json_text(text)
    now = int(time.time())
    tool_name, _ = _first_tool(req)

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
            "tool_calls": [{
                "id": f"call_{now}",
                "type": "function",
                "function": {"name": tool_name, "arguments": args},
            }],
        }
        finish_reason = "tool_calls"

    usage = kimi.get("usage", {}) or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    return {
        "id": kimi.get("id", f"chatcmpl-{now}"),
        "object": "chat.completion",
        "created": now,
        "model": req.get("model") or MODEL,
        "system_fingerprint": None,
        "choices": [{
            "index": 0,
            "message": message,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenClawKimiProxy/0.2"

    def log_message(self, fmt, *args):
        print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), fmt % args), flush=True)

    def do_GET(self):
        if self.path in {"/healthz", "/v1/healthz"}:
            _json_response(self, 200, {"ok": True, "status": "live"})
            return
        if self.path == "/v1/models":
            _json_response(self, 200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            return
        _json_response(self, 404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            _json_response(self, 404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            kimi = _call_kimi(req)
            _json_response(self, 200, _openai_payload(req, kimi))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            _json_response(self, exc.code, {"error": {"message": detail[:1000]}})
        except Exception as exc:
            _json_response(self, 500, {"error": {"message": str(exc)}})


def main():
    if not API_KEY:
        raise SystemExit("KIMI_API_KEY is required")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"kimi proxy listening on http://{HOST}:{PORT}/v1 model={MODEL}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
