#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/codex-telegram-gateway}"
MEMORY_DIR="${MEMORY_DIR:-/opt/openclaw-memory}"
SERVICE_NAME="${SERVICE_NAME:-codex-telegram-gateway.service}"
SUPERVISOR_SERVICE_NAME="${SUPERVISOR_SERVICE_NAME:-codex-telegram-task-supervisor.service}"

failures=0

check() {
  local name="$1"
  shift
  if "$@" >/tmp/openclaw_smoke_check.out 2>&1; then
    echo "OK   ${name}"
  else
    echo "FAIL ${name}"
    sed -n '1,20p' /tmp/openclaw_smoke_check.out
    failures=$((failures + 1))
  fi
}

check "gateway directory" test -d "${APP_DIR}"
check "gateway env exists" test -f "${APP_DIR}/.env"
check "gateway env token configured" bash -c "grep -q '^TELEGRAM_BOT_TOKEN=' '${APP_DIR}/.env' && ! grep -q '^TELEGRAM_BOT_TOKEN=replace_me$' '${APP_DIR}/.env'"
check "python compile gateway" python3 -m py_compile \
  "${APP_DIR}/gateway.py" \
  "${APP_DIR}/task_store.py" \
  "${APP_DIR}/task_worker.py" \
  "${APP_DIR}/task_supervisor.py" \
  "${APP_DIR}/telegram_sender.py" \
  "${APP_DIR}/memory_adapter.py" \
  "${APP_DIR}/facts_store.py"
check "systemd unit enabled" systemctl is-enabled "${SERVICE_NAME}"
check "systemd unit active" systemctl is-active "${SERVICE_NAME}"
check "supervisor unit enabled" systemctl is-enabled "${SUPERVISOR_SERVICE_NAME}"
check "supervisor unit active" systemctl is-active "${SUPERVISOR_SERVICE_NAME}"
check "systemd-run available" systemd-run --wait --collect --unit openclaw-smoke-true /bin/true
check "codex cli exists" command -v codex
check "memory env exists" test -f "${MEMORY_DIR}/config/memory.env"

if [[ -x "${MEMORY_DIR}/venv/bin/python" ]]; then
  check "memory python imports cognee" "${MEMORY_DIR}/venv/bin/python" -c "import cognee"
else
  echo "WARN memory venv missing: ${MEMORY_DIR}/venv"
fi

if [[ "${failures}" -ne 0 ]]; then
  echo "Smoke check failed: ${failures}"
  exit 1
fi

echo "Smoke check passed."
