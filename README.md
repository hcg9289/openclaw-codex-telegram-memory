# OpenClaw Codex Telegram Memory

這個 repo 保存 VPS 上的 Codex Telegram Gateway、Codex-only cognee 記憶接入、systemd unit template 與還原腳本。

## 架構

```text
Telegram
  -> codex-telegram-gateway
  -> per-task codex exec session
  -> facts.json + recent 20 turns + cognee recall
  -> task folder / result.md / watch.json
  -> codex-telegram-task-supervisor
  -> codex exec resume watcher every 10 minutes when needed

Memory write path:
recent 20 turns
  -> conversation batch
  -> Codex Memory Curator
  -> memory write queue
  -> cognee
  -> Codex OpenAI-compatible proxy on 127.0.0.1:18082
```

## 現場路徑

```text
/opt/codex-telegram-gateway
/opt/openclaw-memory
/var/lib/codex-telegram-gateway
/etc/systemd/system
/root/AGENTS.md
```

`root/AGENTS.md` 是 Codex 的全局行為規則模板，安裝時會同步到 `/root/AGENTS.md`。若目標 VPS 已有不同版本，安裝腳本會先建立 UTC 時間戳備份。

## 不在 repo 內的內容

以下內容必須只存在 VPS，不可提交：

```text
/opt/codex-telegram-gateway/.env
/opt/openclaw-memory/config/memory.env
/root/.codex
Telegram bot token
private keys
logs
task/job state
cognee database / pgvector / kuzu data
venv
```

## 安裝概念

1. 在新 VPS 安裝 Codex CLI，並完成 `codex login`。
2. 準備 `/opt/openclaw-memory/venv`、cognee、Ollama embedding、pgvector、Kuzu。
3. 複製 `config/gateway.env.example` 到 `/opt/codex-telegram-gateway/.env`，填入真實 Telegram token 和 allowed user。
4. 複製 `config/memory.env.example` 到 `/opt/openclaw-memory/config/memory.env`，按實際 DB/embedding 設定補齊。
5. 執行 `scripts/install_to_vps.sh`。
6. 執行 `scripts/smoke_test.sh`。

## 驗收命令

```bash
python3 -m py_compile /opt/codex-telegram-gateway/*.py
python3 -m py_compile /opt/openclaw-memory/scripts/codex_openai_proxy.py
systemctl is-active codex-telegram-gateway.service
systemctl is-active codex-telegram-task-supervisor.service
systemctl is-active openclaw-memory-codex-proxy.service
curl -sS http://127.0.0.1:18082/healthz
```

## 目前驗收狀態

2026-05-05 已在 VPS 驗收：

```text
Codex proxy /healthz: ok
Cognee memory write queue -> writer -> search: ok
Gateway Python compile: ok
Codex exec session start/resume: ok
Codex-only watcher resume + watch_decision.json: ok
MiniMax memory proxy: stopped and disabled
```
