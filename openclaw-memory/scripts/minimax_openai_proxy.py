#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MINIMAX_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("MINIMAX_PROXY_PORT", "18081"))
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")
BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
API_KEY = os.environ.get("MINIMAX_API_KEY", "")


def _json_response(handler, status, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _strip_thinking(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"^\s*<think>.*", "", text, flags=re.S | re.I) if text.lstrip().startswith("<think>") else text
    fenced = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if match:
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass
    return text.strip()


def _call_minimax(req):
    payload = dict(req)
    model = payload.get("model") or MODEL
    if isinstance(model, str) and "/" in model:
        model = model.split("/", 1)[1]
    payload["model"] = model or MODEL
    payload["stream"] = False
    extra_body = payload.get("extra_body") if isinstance(payload.get("extra_body"), dict) else {}
    extra_body["reasoning_split"] = True
    payload["extra_body"] = extra_body
    payload.setdefault("max_tokens", 2048)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_openai_response(req, upstream):
    for choice in upstream.get("choices", []) or []:
        msg = choice.get("message") or {}
        if "content" in msg and msg.get("content") is not None:
            msg["content"] = _strip_thinking(msg.get("content"))
        choice["message"] = msg
    upstream["model"] = req.get("model") or MODEL
    upstream.setdefault("object", "chat.completion")
    upstream.setdefault("created", int(time.time()))
    return upstream


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenClawMiniMaxProxy/0.1"

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
            upstream = _call_minimax(req)
            _json_response(self, 200, _normalize_openai_response(req, upstream))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            _json_response(self, exc.code, {"error": {"message": detail[:1000]}})
        except Exception as exc:
            _json_response(self, 500, {"error": {"message": str(exc)}})


def main():
    if not API_KEY:
        raise SystemExit("MINIMAX_API_KEY is required")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"minimax proxy listening on http://{HOST}:{PORT}/v1 model={MODEL}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
