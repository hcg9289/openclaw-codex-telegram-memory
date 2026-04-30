#!/usr/bin/env python3
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
MAX_MESSAGE = 3900


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def api_call(method, data=None, timeout=20):
    load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=payload)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {parsed}")
    return parsed["result"]


def send_message(chat_id, text):
    text = text or "(empty response)"
    chunks = [text[i : i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)]
    for chunk in chunks:
        api_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            },
        )
