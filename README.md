# OpenClaw Codex Telegram Memory

Sanitized source package for the VPS Codex Telegram Gateway and OpenClaw memory integration.

## New VPS Restore

This repository is designed to bootstrap a new VPS without committing secrets.

```bash
git clone https://github.com/hcg9289/openclaw-codex-telegram-memory.git
cd openclaw-codex-telegram-memory
sudo bash deploy/install.sh
```

Then fill the gateway secrets on the VPS:

```bash
sudo nano /opt/codex-telegram-gateway/.env
codex login --device-auth
sudo systemctl restart codex-telegram-gateway.service
sudo bash deploy/scripts/smoke_check.sh
```

For memory services, fill the memory env first:

```bash
sudo nano /opt/openclaw-memory/config/memory.env
sudo bash deploy/install.sh --with-memory
sudo bash deploy/scripts/smoke_check.sh
```

The installer keeps existing real env files. It only creates env files from examples when they do not already exist.
For memory Docker Compose, the installer links `/opt/openclaw-memory/.env` to `/opt/openclaw-memory/config/memory.env` so compose variable interpolation uses the same secret file.

## Included

- Codex Telegram Gateway Python source
- Background task worker/store/starter
- Current facts and cognee memory adapters
- OpenClaw memory helper scripts
- Example environment files
- New VPS installer
- systemd unit
- smoke check script
- Engineering owner prompt protocol for long task stewardship

## Excluded

- Telegram bot tokens
- Codex login/auth/session files
- .env files
- Postgres/pgvector data
- Kuzu graph data
- runtime logs, cache, queue, and task/job state

## Security

Copy example env files before use and fill secrets only on the target VPS.
Never commit real env files or Codex auth directories.

The Telegram task worker runs Codex with `--dangerously-bypass-approvals-and-sandbox` because this VPS is intentionally operated as a trusted automation host. Do not use this mode on an untrusted machine or with untrusted Telegram users.

## Engineering Owner Behavior

Telegram background tasks are prompted to behave like an engineering owner, not only an answer worker:

- A started process is not treated as an owned workflow.
- Long tasks require heartbeat/progress, checkpoint/resume, failure evidence, and final-report detection.
- Progress/result questions must inspect task folders, progress files, runner logs, or final reports.
- If a task is stale or interrupted, Codex must inspect root cause before restarting.
- Replies should keep useful structure, then give a natural Traditional Chinese conclusion for phone use.

## Runtime Layout

```text
/opt/codex-telegram-gateway/
  gateway.py
  task_worker.py
  .env

/opt/openclaw-memory/
  config/memory.env
  scripts/
  docker-compose.yml
  venv/

/var/lib/codex-telegram-gateway/
  memory/
  tasks/
  jobs/

/etc/systemd/system/codex-telegram-gateway.service
```

## What Cannot Be Restored Automatically

- Telegram bot token: set it in `/opt/codex-telegram-gateway/.env`.
- Telegram allowed user IDs: set them in `/opt/codex-telegram-gateway/.env`.
- Codex login: run `codex login --device-auth` on the new VPS.
- LLM API keys and database passwords: set them in `/opt/openclaw-memory/config/memory.env`.
- Existing vector/graph/database data: restore from a separate backup if needed.
