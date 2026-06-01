#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -d -o root -g root -m 750 /opt/codex-telegram-gateway
install -d -o root -g root -m 750 /opt/openclaw-memory
install -d -o root -g root -m 750 /opt/openclaw-memory/scripts
install -d -o root -g root -m 750 /opt/openclaw-memory/logs
install -d -o root -g root -m 750 /opt/openclaw-memory/config
install -d -o root -g root -m 750 /opt/openclaw-memory/data
install -d -o root -g root -m 750 /opt/openclaw-memory/system
install -d -o root -g root -m 750 /opt/openclaw-memory/system/databases
install -d -o root -g root -m 750 /opt/openclaw-memory/cache
install -d -o root -g root -m 750 /opt/openclaw-memory/postgres
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/memory/write_queue/pending
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/memory/write_queue/processed
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/memory/write_queue/failed
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/memory/conversation_batches
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/tasks
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/jobs
install -d -o root -g root -m 750 /var/lib/codex-telegram-gateway/resource_locks

find "$REPO_DIR/codex-telegram-gateway" -maxdepth 1 -type f -name '*.py' -print0 \
  | xargs -0 -I{} install -o root -g root -m 750 "{}" /opt/codex-telegram-gateway/

find "$REPO_DIR/openclaw-memory/scripts" -maxdepth 1 -type f -name '*.py' -print0 \
  | xargs -0 -I{} install -o root -g root -m 750 "{}" /opt/openclaw-memory/scripts/

install -o root -g root -m 640 "$REPO_DIR/openclaw-memory/requirements.txt" /opt/openclaw-memory/requirements.txt
install -o root -g root -m 640 "$REPO_DIR/openclaw-memory/docker-compose.yml" /opt/openclaw-memory/docker-compose.yml

find "$REPO_DIR/deploy/systemd" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) -print0 \
  | xargs -0 -I{} install -o root -g root -m 644 "{}" /etc/systemd/system/

if [ -f "$REPO_DIR/root/AGENTS.md" ]; then
  if [ -f /root/AGENTS.md ] && ! cmp -s "$REPO_DIR/root/AGENTS.md" /root/AGENTS.md; then
    backup="/root/AGENTS.md.backup-$(date -u +%Y%m%dT%H%M%SZ)"
    install -o root -g root -m 640 /root/AGENTS.md "$backup"
    echo "Backed up existing /root/AGENTS.md to $backup"
  fi
  install -o root -g root -m 640 "$REPO_DIR/root/AGENTS.md" /root/AGENTS.md
fi

if [ ! -f /opt/codex-telegram-gateway/.env ]; then
  install -o root -g root -m 600 "$REPO_DIR/codex-telegram-gateway/.env.example" /opt/codex-telegram-gateway/.env
  echo "Created /opt/codex-telegram-gateway/.env from example. Edit it before starting gateway."
fi

if [ ! -f /opt/openclaw-memory/config/memory.env ]; then
  install -o root -g root -m 600 "$REPO_DIR/openclaw-memory/config/memory.env.example" /opt/openclaw-memory/config/memory.env
  echo "Created /opt/openclaw-memory/config/memory.env from example. Edit it before starting memory services."
fi

if [ ! -d /opt/openclaw-memory/venv ]; then
  python3 -m venv /opt/openclaw-memory/venv
fi
/opt/openclaw-memory/venv/bin/python -m pip install --upgrade pip setuptools wheel
/opt/openclaw-memory/venv/bin/pip install -r /opt/openclaw-memory/requirements.txt

systemctl daemon-reload
systemctl enable openclaw-memory-codex-proxy.service
systemctl enable codex-telegram-gateway.service
systemctl enable codex-telegram-task-supervisor.service
systemctl enable codex-telegram-memory-writer.timer
systemctl enable codex-telegram-memory-curator.timer

echo "Install complete. Edit env files, then run scripts/smoke_test.sh."
