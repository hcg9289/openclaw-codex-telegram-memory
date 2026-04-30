# OpenClaw Codex Telegram Memory

Sanitized source package for the VPS Codex Telegram Gateway and OpenClaw memory integration.

## Included

- Codex Telegram Gateway Python source
- Background task worker/store/starter
- Current facts and cognee memory adapters
- OpenClaw memory helper scripts
- Example environment files

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
