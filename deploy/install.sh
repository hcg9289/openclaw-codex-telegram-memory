#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -d -o root -g root -m 750 /opt/codex-telegram-gateway
install -d -o root -g root -m 750 /opt/openclaw-memory/scripts
install -d -o root -g root -m 750 /opt/openclaw-memory/logs
install -d -o root -g root -m 750 /opt/openclaw-memory/config
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway

find "$REPO_DIR/gateway" -maxdepth 1 -type f -name '*.py' -print0 \
  | xargs -0 -I{} install -o root -g root -m 750 "{}" /opt/codex-telegram-gateway/

find "$REPO_DIR/memory/scripts" -maxdepth 1 -type f -name '*.py' -print0 \
  | xargs -0 -I{} install -o root -g root -m 750 "{}" /opt/openclaw-memory/scripts/

find "$REPO_DIR/systemd" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) -print0 \
  | xargs -0 -I{} install -o root -g root -m 644 "{}" /etc/systemd/system/

if [ ! -f /opt/codex-telegram-gateway/.env ]; then
  install -o root -g root -m 600 "$REPO_DIR/config/gateway.env.example" /opt/codex-telegram-gateway/.env
  echo "Created /opt/codex-telegram-gateway/.env from example. Edit it before starting gateway."
fi

if [ ! -f /opt/openclaw-memory/config/memory.env ]; then
  install -o root -g root -m 600 "$REPO_DIR/config/memory.env.example" /opt/openclaw-memory/config/memory.env
  echo "Created /opt/openclaw-memory/config/memory.env from example. Edit it before starting memory services."
fi

systemctl daemon-reload
systemctl enable openclaw-memory-codex-proxy.service
systemctl enable codex-telegram-gateway.service
systemctl enable codex-telegram-task-supervisor.service
systemctl enable codex-telegram-memory-writer.timer
systemctl enable codex-telegram-memory-curator.timer

echo "Install complete. Edit env files, then run scripts/smoke_test.sh."
