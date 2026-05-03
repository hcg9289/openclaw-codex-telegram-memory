#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/codex-telegram-gateway}"
MEMORY_DIR="${MEMORY_DIR:-/opt/openclaw-memory}"
STATE_DIR="${STATE_DIR:-/var/lib/codex-telegram-gateway}"
SERVICE_NAME="${SERVICE_NAME:-codex-telegram-gateway.service}"
SUPERVISOR_SERVICE_NAME="${SUPERVISOR_SERVICE_NAME:-codex-telegram-task-supervisor.service}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_MEMORY=0
START_GATEWAY=1

for arg in "$@"; do
  case "$arg" in
    --with-memory) WITH_MEMORY=1 ;;
    --no-start) START_GATEWAY=0 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: sudo bash deploy/install.sh [--with-memory] [--no-start]" >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "This installer must run as root. Use: sudo bash deploy/install.sh" >&2
  exit 1
fi

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ca-certificates \
      curl \
      git \
      python3 \
      python3-pip \
      python3-venv \
      rsync
  fi
}

copy_gateway() {
  install -d -m 0755 "${APP_DIR}"
  rsync -a --delete \
    --exclude '__pycache__/' \
    --exclude '.env' \
    "${REPO_DIR}/codex-telegram-gateway/" "${APP_DIR}/"

  if [[ ! -f "${APP_DIR}/.env" ]]; then
    install -m 0600 "${REPO_DIR}/codex-telegram-gateway/.env.example" "${APP_DIR}/.env"
    echo "Created ${APP_DIR}/.env from example. Fill TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS before production use."
  else
    chmod 0600 "${APP_DIR}/.env"
    echo "Kept existing ${APP_DIR}/.env"
  fi
}

copy_memory() {
  install -d -m 0755 "${MEMORY_DIR}/config" "${MEMORY_DIR}/scripts"
  rsync -a --delete "${REPO_DIR}/openclaw-memory/scripts/" "${MEMORY_DIR}/scripts/"
  install -m 0644 "${REPO_DIR}/openclaw-memory/docker-compose.yml" "${MEMORY_DIR}/docker-compose.yml"

  if [[ ! -f "${MEMORY_DIR}/config/memory.env" ]]; then
    install -m 0600 "${REPO_DIR}/openclaw-memory/config/memory.env.example" "${MEMORY_DIR}/config/memory.env"
    echo "Created ${MEMORY_DIR}/config/memory.env from example. Fill LLM_API_KEY and database password before starting memory."
  else
    chmod 0600 "${MEMORY_DIR}/config/memory.env"
    echo "Kept existing ${MEMORY_DIR}/config/memory.env"
  fi
  ln -sfn "config/memory.env" "${MEMORY_DIR}/.env"

  if [[ ! -d "${MEMORY_DIR}/venv" ]]; then
    python3 -m venv "${MEMORY_DIR}/venv"
  fi
  "${MEMORY_DIR}/venv/bin/python" -m pip install --upgrade pip
  "${MEMORY_DIR}/venv/bin/python" -m pip install -r "${REPO_DIR}/openclaw-memory/requirements.txt"
}

install_service() {
  install -m 0644 "${REPO_DIR}/deploy/systemd/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
  install -m 0644 "${REPO_DIR}/deploy/systemd/${SUPERVISOR_SERVICE_NAME}" "/etc/systemd/system/${SUPERVISOR_SERVICE_NAME}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl enable "${SUPERVISOR_SERVICE_NAME}"
}

create_state_dirs() {
  install -d -m 0700 "${STATE_DIR}"
  install -d -m 0700 "${STATE_DIR}/memory" "${STATE_DIR}/tasks" "${STATE_DIR}/jobs" "${STATE_DIR}/resource_locks"
}

env_ready() {
  [[ -f "${APP_DIR}/.env" ]] &&
    grep -q '^TELEGRAM_BOT_TOKEN=' "${APP_DIR}/.env" &&
    ! grep -q '^TELEGRAM_BOT_TOKEN=replace_me$' "${APP_DIR}/.env"
}

start_memory_if_requested() {
  if [[ "${WITH_MEMORY}" -ne 1 ]]; then
    return
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed. Install Docker, then run: cd ${MEMORY_DIR} && docker compose up -d" >&2
    return
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose plugin is not available. Install it, then run: cd ${MEMORY_DIR} && docker compose up -d" >&2
    return
  fi
  (cd "${MEMORY_DIR}" && docker compose up -d)
}

main() {
  install_packages
  copy_gateway
  copy_memory
  create_state_dirs
  install_service
  start_memory_if_requested

  if [[ "${START_GATEWAY}" -eq 1 && env_ready ]]; then
    systemctl restart "${SERVICE_NAME}"
    systemctl restart "${SUPERVISOR_SERVICE_NAME}"
    systemctl --no-pager --full status "${SERVICE_NAME}" || true
    systemctl --no-pager --full status "${SUPERVISOR_SERVICE_NAME}" || true
  else
    echo "Gateway was installed but not started because ${APP_DIR}/.env is not ready or --no-start was used."
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "Codex CLI was not found. Install Codex CLI, then run: codex login --device-auth"
  else
    echo "Codex CLI found at: $(command -v codex)"
    echo "Verify login on this VPS with: codex login --device-auth"
  fi

  echo "Install complete."
}

main "$@"
