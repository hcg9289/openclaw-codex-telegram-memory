#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"


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


def main():
    load_env_file()
    sys.path.insert(0, str(APP_DIR))

    import gateway
    import memory_adapter
    import task_store
    import telegram_sender

    if len(sys.argv) != 2:
        raise SystemExit("usage: task_worker.py <task_id>")
    task_id = sys.argv[1]
    task = task_store.get_task(task_id)
    if not task:
        raise SystemExit(f"task not found: {task_id}")

    chat_id = task.get("chat_id")
    user_id = task.get("user_id")
    user_text = task.get("user_text") or ""
    parent_task_id = task.get("supervisor_for")

    task_store.update_task(task_id, status="running", started_at=task_store.utc_now(), error=None)
    task_store.append_event(task_id, "started", {})

    result = ""
    status = "failed"
    returncode = None
    started = time.time()
    out_path = None
    try:
        referenced = task_store.get_task(parent_task_id) if parent_task_id else task_store.resolve_referenced_task(user_text, exclude_task_id=task_id)
        if parent_task_id:
            recent_context = "(omitted for supervisor child task; use referenced task context instead)"
            recall_text = "(omitted for supervisor child task; cognee is historical memory, not live task state)"
        else:
            recent_context = gateway.format_recent_context()
            recall_query = gateway.build_recall_query(user_text)
            if referenced:
                recall_query += "\n\nReferenced task context:\n" + task_store.task_context(referenced["task_id"], limit_chars=8000)
            recall_result = memory_adapter.recall_memory(recall_query, session_id=str(chat_id), top_k=5, timeout=45)
            recall_text = recall_result.get("text") if recall_result.get("ok") else f"(cognee recall failed: {recall_result.get('error')})"
            gateway.save_last_context(chat_id, user_id, recall_result)

        task_context = task_store.task_context(referenced["task_id"], limit_chars=12000) if referenced else "(no referenced task context)"
        prompt = gateway.build_prompt(user_text, recall_text, recent_context)
        prompt += f"""

[STRICT_SCOPE_RULE]
Do exactly what the user asked.
Do not expand scope.
Do not start unrelated work.
Do not modify files unless the user explicitly asked.
If the user asks for progress, only inspect and explain progress.
If the user asks for result, only read and summarize existing result.
If evidence is insufficient, say so instead of doing extra work.
[/STRICT_SCOPE_RULE]

[TELEGRAM_REPLY_STYLE]
用繁體中文自然回覆，像工程拍檔向手機上的人報告，不要像制式報告。
- 第一句先講人話結論，不要以「最終目標：」開頭。
- 除非使用者要求，不要輸出「使用 Skill：」「證據：」這類固定欄位。
- 如需證據，最多列 3 個短項目；避免大段 code block 或 JSON。
- 對進度/巡檢回覆，必須包含目前進度、完成百分比；資料足夠時估 ETA，資料不足時明講不能估。
- task_id 由 gateway 放在訊息最後，你的正文不用重複 task_id。
- 任務完成後的正文必須按第一性原理寫：先回答「是否成功/失敗/仍需觀察」，再說「這個判定的適用範圍」，最後才放最多 3 條能支撐判定的依據。
- 不要使用「人話結論」「證據」「使用 Skill」這類標籤；直接用自然語言和短項目。
- 不要貼半截 log、內部事件名長串、JSON tail、或超過 3 條的證據清單；需要細節時只給檔案路徑或說明可再查。
[/TELEGRAM_REPLY_STYLE]

[ENGINEERING_OWNER_PROTOCOL]
你是會持續接管結果的工程負責人，不是只回答問題的 worker。

回覆手機使用者時：
- 保留必要結構，但結構後必須先用一句自然繁體中文講人話結論。
- 用詞必須跟證據一致；不要把 started 說成 completed/stable/owned。
- 沒有 final report，不可暗示完成。

長任務 / overnight / 回測 / 掃描 / WF / 部署：
- 啟動不等於接管。
- 只有 process 或 unit 存活、progress/heartbeat 持續更新、checkpoint/resume 存在、final report detector 明確，才可說正在被接管。
- 發現 stale、PID 消失、假 running、沒有輸出、report 缺失時，必須先查 root cause：stderr、stdout、progress、events、journalctl、dmesg/OOM、traceback。
- 不可只回報問題；若安全，應加入或建議 checkpoint/resume/supervisor/heartbeat 後再重啟。
- 修復後未觀察足夠時間，只可說「已加入修復並重新啟動，仍需觀察」，不可說「已穩定」。

Progress / result 類問題：
- 必須讀取 task folder、progress file、runner log 或 final report。
- 若證據不足，直接說證據不足。
- 若任務中斷，列出最後更新時間、完成數、PID/unit 狀態、缺少哪個報告，以及下一步補救。
[/ENGINEERING_OWNER_PROTOCOL]

[TASK_WATCH_CONTRACT]
If this task starts any background process, overnight job, long scan, backtest, WF, deployment, data download, or any work that will continue after your Codex process exits, you MUST register it for supervision.

Write this JSON file before your final answer:
/var/lib/codex-telegram-gateway/tasks/{task_id}/watch.json

Schema:
{{
  "enabled": true,
  "kind": "external_process",
  "summary": "short human-readable description",
  "resource_key": "stable.unique.resource.name",
  "resource_dir": "/absolute/path/to/external/work/dir",
  "lock_mode": "exclusive",
  "pid": 12345,
  "pid_file": "/absolute/path/to/runner.pid",
  "progress_file": "/absolute/path/to/progress.json",
  "heartbeat_file": "/absolute/path/to/heartbeat.json",
  "log_file": "/absolute/path/to/stdout_or_runner.log",
  "stderr_file": "/absolute/path/to/stderr_or_error.log",
  "final_report": "/absolute/path/to/final_report.md",
  "resume_command": "safe command to resume from checkpoint",
  "stale_after_seconds": 900
}}

Only include paths that truly exist or will be created by the runner. Do not include secrets.
Before starting a second runner against the same resource_dir/final_report/progress_file, inspect existing task folders and resource_locks. If the resource is locked, continue/inspect the existing task instead of launching a duplicate runner.
If no long-running/background work is started, do not create watch.json.
[/TASK_WATCH_CONTRACT]

[THREAD_MEMORY_RULE]
Normal tasks may write concise conclusions into main recent memory.
Supervisor child tasks with supervisor_for=<parent_task_id> must not pollute main recent memory. They must write root cause, recovery actions, and final status into the parent task supervisor_thread.jsonl through the gateway runtime.
[/THREAD_MEMORY_RULE]

[REFERENCED_TASK_CONTEXT]
{task_context}
[/REFERENCED_TASK_CONTEXT]

[CURRENT_BACKGROUND_TASK]
task_id={task_id}
Write your final Telegram answer as the final response. Do not claim work is complete unless you have evidence.
[/CURRENT_BACKGROUND_TASK]
"""
        task_store.write_task_text(task_id, "prompt.md", prompt)

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
            out_path = out_file.name
        cmd = [
            os.environ.get("CODEX_BIN", "/usr/bin/codex"),
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--output-last-message",
            out_path,
            prompt,
        ]
        completed = subprocess.run(
            cmd,
            cwd=os.environ.get("CODEX_WORKDIR", "/root"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=int(os.environ.get("CODEX_TIMEOUT_SECONDS", "18000")),
            check=False,
        )
        returncode = completed.returncode
        stdout_text = memory_adapter.redact_secrets(completed.stdout or "")
        task_store.write_task_text(task_id, "stdout.log", stdout_text)
        try:
            answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            answer = ""
        answer = memory_adapter.redact_secrets(answer)
        if completed.returncode == 0:
            status = "succeeded"
            result = answer or stdout_text[-6000:] or "(Codex finished without text output)"
        else:
            result = answer or stdout_text[-6000:] or f"Codex failed with exit code {completed.returncode}."
    except subprocess.TimeoutExpired:
        status = "failed"
        result = f"Codex timed out after {os.environ.get('CODEX_TIMEOUT_SECONDS', '18000')} seconds."
    except Exception as exc:
        status = "failed"
        result = f"{type(exc).__name__}: {exc}"
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except FileNotFoundError:
                pass

    safe_result = memory_adapter.redact_secrets(result)
    watch = task_store.read_task_json(task_id, "watch.json", default={}) or {}
    if status == "succeeded" and isinstance(watch, dict) and watch.get("enabled", False):
        ok, lock, owner = task_store.acquire_resource_lock(task_store.get_task(task_id) or task, watch)
        if ok:
            if lock:
                watch["resource_key"] = lock.get("resource_key")
                watch["lock_file"] = lock.get("lock_file")
                watch.setdefault("lock_mode", lock.get("lock_mode") or "exclusive")
                task_store.write_task_json(task_id, "watch.json", watch)
            status = "watching"
            safe_result = (
                safe_result.rstrip()
                + "\n\n[supervisor]\n"
                + "此 task 已登記 watch.json，將由 codex-telegram-task-supervisor.service 持續監護，直到完成或明確失敗。\n"
                + (f"resource_key={lock.get('resource_key')}\n" if lock else "")
                + "[/supervisor]"
            )
            task_store.append_event(task_id, "watch_registered", {"watch": watch, "lock": lock})
        else:
            status = "blocked_by_resource"
            safe_result = (
                safe_result.rstrip()
                + "\n\n[resource_lock]\n"
                + "此 task 嘗試登記的外部資源已被另一個 active/watching task 持有，因此已阻止重複 runner。\n"
                + f"owner_task_id={owner.get('task_id') if owner else None}\n"
                + f"owner_title={owner.get('title') if owner else None}\n"
                + f"resource_key={(lock or {}).get('resource_key')}\n"
                + "請改為接續/查詢既有 task，或等待 lock 釋放。\n"
                + "[/resource_lock]"
            )
            task_store.append_event(task_id, "resource_lock_blocked", {"lock": lock, "owner": owner})
    task_store.write_task_text(task_id, "stderr.log", "")
    task_store.write_task_text(task_id, "result.md", safe_result)
    task_store.update_task(
        task_id,
        status=status,
        error=None if status in {"succeeded", "watching"} else safe_result[:1000],
        returncode=returncode,
        duration_seconds=round(time.time() - started, 2),
    )
    task_store.append_event(task_id, "finished", {"status": status, "returncode": returncode})

    if chat_id:
        if parent_task_id:
            parent = task_store.get_task(parent_task_id) or {}
            parent_status = parent.get("status") or "unknown"
            telegram_text = (
                f"任務巡檢完成：{parent.get('title') or task.get('title')}\n"
                f"父 task_id：{parent_task_id}\n"
                f"父任務狀態：{parent_status}\n"
                f"巡檢 task_id：{task_id}\n"
                f"巡檢狀態：{status}\n\n"
                f"{safe_result}"
            )
        elif status == "watching":
            heading = "任務已進入監護"
            telegram_text = f"{heading}：{task.get('title')}\ntask_id：{task_id}\n狀態：{status}\n\n{safe_result}"
        elif status == "blocked_by_resource":
            heading = "任務被資源鎖阻止"
            telegram_text = f"{heading}：{task.get('title')}\ntask_id：{task_id}\n狀態：{status}\n\n{safe_result}"
        elif status == "failed":
            heading = "任務失敗"
            telegram_text = f"{heading}：{task.get('title')}\ntask_id：{task_id}\n狀態：{status}\n\n{safe_result}"
        else:
            heading = "任務完成"
            telegram_text = f"{heading}：{task.get('title')}\ntask_id：{task_id}\n狀態：{status}\n\n{safe_result}"
        telegram_sender.send_message(chat_id, telegram_text)
    task_store.append_thread_message(task_id, "thread.jsonl", "assistant", safe_result, {"status": status})
    if parent_task_id:
        task_store.append_thread_message(
            parent_task_id,
            "supervisor_thread.jsonl",
            "assistant",
            safe_result,
            {"child_task_id": task_id, "reason": task.get("supervisor_reason"), "status": status},
        )
    else:
        gateway.append_recent("assistant", safe_result, chat_id, user_id)
    if not parent_task_id and gateway.should_auto_remember(user_text, safe_result):
        gateway.remember_async(
            f"Telegram background task:\nUser: {user_text}\nAssistant: {safe_result}",
            {"source": "telegram_task", "chat_id": chat_id, "user_id": user_id, "task_id": task_id},
        )

    subprocess.run(
        [sys.executable, str(APP_DIR / "task_starter.py")],
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


if __name__ == "__main__":
    main()
