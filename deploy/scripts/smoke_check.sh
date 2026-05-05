#!/usr/bin/env bash
set -euo pipefail

python3 -m py_compile /opt/codex-telegram-gateway/*.py
python3 -m py_compile /opt/openclaw-memory/scripts/codex_openai_proxy.py

systemctl restart openclaw-memory-codex-proxy.service
systemctl restart codex-telegram-task-supervisor.service
systemctl restart codex-telegram-gateway.service
systemctl restart codex-telegram-memory-writer.timer
systemctl restart codex-telegram-memory-curator.timer

systemctl is-active openclaw-memory-codex-proxy.service
systemctl is-active codex-telegram-task-supervisor.service
systemctl is-active codex-telegram-gateway.service
systemctl is-active codex-telegram-memory-writer.timer
systemctl is-active codex-telegram-memory-curator.timer

curl -sS --max-time 15 http://127.0.0.1:18082/healthz
echo
curl -sS --max-time 15 http://127.0.0.1:18082/v1/models
echo

echo "smoke test complete"
