#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import facts_store
import job_store
import memory_adapter
import orchestrator
import router
import task_store


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(item.strip())
    for item in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if item.strip()
}
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/bin/codex")
CODEX_WORKDIR = os.environ.get("CODEX_WORKDIR", "/root")
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "300"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
MEMORY_DIR = STATE_DIR / "memory"
OFFSET_FILE = STATE_DIR / "offset.txt"
RECENT_MESSAGES_FILE = MEMORY_DIR / "recent_messages.jsonl"
LAST_CONTEXT_FILE = MEMORY_DIR / "last_context.json"
API_BASE = f"https://api.telegram.org/bot{TOKEN}"
MAX_MESSAGE = 3900
MAX_RECENT_TURNS = int(os.environ.get("MAX_RECENT_TURNS", "20"))
MAX_ACTIVE_WORK_TASKS = int(os.environ.get("MAX_ACTIVE_WORK_TASKS", "2"))
STOP = False


def handle_signal(_signum, _frame):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def api_call(method, data=None, timeout=35):
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(f"{API_BASE}/{method}", data=payload)
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
            timeout=20,
        )


def read_offset():
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_offset(offset):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    os.replace(tmp, OFFSET_FILE)


def _jsonl_append(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False) + "\n")


def _read_jsonl(path, limit=None):
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit:
        lines = lines[-limit:]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _trim_recent_messages():
    items = _read_jsonl(RECENT_MESSAGES_FILE)
    keep = items[-MAX_RECENT_TURNS * 2 :]
    tmp = RECENT_MESSAGES_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in keep) + ("\n" if keep else ""), encoding="utf-8")
    os.replace(tmp, RECENT_MESSAGES_FILE)


def append_recent(role, text, chat_id, user_id):
    safe_text = memory_adapter.redact_secrets(text)
    _jsonl_append(
        RECENT_MESSAGES_FILE,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "role": role,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": safe_text,
        },
    )
    _trim_recent_messages()


def clear_recent_memory():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    RECENT_MESSAGES_FILE.write_text("", encoding="utf-8")
    LAST_CONTEXT_FILE.write_text("{}", encoding="utf-8")


def format_recent_context(limit_turns=MAX_RECENT_TURNS):
    items = _read_jsonl(RECENT_MESSAGES_FILE, limit_turns * 2)
    if not items:
        return "(no recent Telegram context)"
    lines = []
    for item in items:
        role = item.get("role", "unknown")
        text = (item.get("text") or "").strip()
        if len(text) > 1200:
            text = text[:1200] + "...[truncated]"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def read_agents_excerpt():
    path = Path("/root/AGENTS.md")
    if not path.exists():
        return "(no /root/AGENTS.md found)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = []
    for line in lines:
        if any(keyword in line for keyword in ["本機", "Must", "禁止", "secrets", "Skills", "指令", "驗證", "目標系統"]):
            selected.append(line)
        if len(selected) >= 35:
            break
    return "\n".join(selected) or "\n".join(lines[:40])


def build_recall_query(user_text):
    recent = format_recent_context(limit_turns=3)
    return f"Current user message:\n{user_text}\n\nRecent Telegram context:\n{recent}"


def build_prompt(user_text, recall_text, recent_context):
    return f"""你正在 VPS 的 Codex Telegram Gateway 中執行。

請嚴格遵守 /root/AGENTS.md；以下是重點摘錄：
{read_agents_excerpt()}

[AUTHORITATIVE_CURRENT_FACTS]
以下是 facts.json 的權威當前事實。若這裡與 cognee 歷史記憶衝突，必須以這裡為準：
{facts_store.format_facts_for_prompt()}
[/AUTHORITATIVE_CURRENT_FACTS]

[RECENT_TELEGRAM_CONTEXT]
{recent_context}
[/RECENT_TELEGRAM_CONTEXT]

[HISTORICAL_COGNEE_MEMORY_NON_AUTHORITATIVE]
以下是 cognee recall 找到的歷史/背景記憶，可能相關，也可能過時；不可把它當作 current truth：
{recall_text or "(no relevant cognee memory)"}
[/HISTORICAL_COGNEE_MEMORY_NON_AUTHORITATIVE]

使用者本次訊息：
{user_text}
"""


def should_use_orchestrator(user_text):
    text = str(user_text or "").strip()
    lowered = text.lower()
    heavy_keywords = [
        "檢查",
        "分析",
        "修",
        "修改",
        "實作",
        "實現",
        "部署",
        "安裝",
        "更新",
        "回測",
        "跑一次",
        "執行",
        "debug",
        "bug",
        "error",
        "錯誤",
        "服務",
        "systemd",
        "docker",
        "git",
        "vps",
        "/root/",
        "/opt/",
    ]
    if any(keyword in lowered for keyword in heavy_keywords):
        return True
    if len(text) >= 120 and any(mark in text for mark in ["?", "？", "請", "幫"]):
        return True
    return False


def direct_answer(user_text, chat_id, user_id, recent_context):
    recall_result = memory_adapter.recall_memory(user_text, session_id=str(chat_id), top_k=5, timeout=45)
    recall_text = recall_result.get("text") if recall_result.get("ok") else f"(cognee recall failed: {recall_result.get('error')})"
    save_last_context(chat_id, user_id, recall_result)
    answer = memory_adapter.redact_secrets(run_codex(build_prompt(user_text, recall_text, recent_context)))
    send_message(chat_id, answer)
    append_recent("user", user_text, chat_id, user_id)
    append_recent("assistant", answer, chat_id, user_id)
    if should_auto_remember(user_text, answer):
        remember_async(
            f"Telegram direct answer:\nUser: {user_text}\nAssistant: {answer}",
            {"source": "telegram_direct", "chat_id": chat_id, "user_id": user_id},
        )
    return answer


def run_codex(prompt):
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = out_file.name
    try:
        cmd = [
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "--output-last-message",
            out_path,
            prompt,
        ]
        completed = subprocess.run(
            cmd,
            cwd=CODEX_WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=CODEX_TIMEOUT,
            check=False,
        )
        answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        if answer:
            return answer
        tail = (completed.stdout or "").strip()[-3000:]
        if completed.returncode == 0:
            return tail or "(Codex finished without text output)"
        return f"Codex failed with exit code {completed.returncode}.\n\n{tail}"
    except subprocess.TimeoutExpired:
        return f"Codex timed out after {CODEX_TIMEOUT} seconds."
    finally:
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass


def should_auto_remember(user_text, answer):
    combined = f"{user_text}\n{answer}".lower()
    keywords = [
        "已完成",
        "完成",
        "已更新",
        "已安裝",
        "部署",
        "systemd",
        ".service",
        "/opt/",
        "/etc/",
        "/var/lib/",
        "passwordauthentication",
        "authorized_keys",
        "vps_狀態",
        "fingerprint",
        "active",
        "health",
    ]
    return any(keyword.lower() in combined for keyword in keywords) and not memory_adapter.contains_secret(combined)


def remember_async(text, metadata):
    def _worker():
        result = memory_adapter.remember_fact(text, metadata=metadata, session_id=str(metadata.get("chat_id")))
        if not result.get("ok"):
            print(f"memory remember skipped/failed: {result}", flush=True)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def save_last_context(chat_id, user_id, recall_result):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chat_id": chat_id,
        "user_id": user_id,
        "recall_ok": recall_result.get("ok"),
        "recall_error": recall_result.get("error"),
        "recall_items": recall_result.get("items", [])[:5],
        "recent": _read_jsonl(RECENT_MESSAGES_FILE, 10),
    }
    tmp = LAST_CONTEXT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LAST_CONTEXT_FILE)


def format_memory_status():
    status = memory_adapter.memory_status()
    status["facts_file"] = str(facts_store.FACTS_FILE)
    status["facts_events_file"] = str(facts_store.EVENTS_FILE)
    status["current_facts_count"] = len(facts_store.get_current_facts())
    safe = {key: value for key, value in status.items() if "token" not in key.lower() and "key" not in key.lower()}
    return json.dumps(safe, ensure_ascii=False, indent=2)


def parse_fact_assignment(text, command):
    payload = text[len(command) :].strip()
    if "=" not in payload:
        raise ValueError(f"用法：{command} <key> = <value>")
    key, value = payload.split("=", 1)
    return key.strip(), value.strip()


def _job_id_from_command(text, command):
    return text[len(command) :].strip().split()[0] if text[len(command) :].strip() else ""


def format_jobs():
    jobs = job_store.list_jobs(limit=10)
    if not jobs:
        return "(no jobs)"
    lines = []
    for job in jobs:
        lines.append(
            f"{job.get('title') or job_store.make_title(job.get('user_text'))}\n"
            f"  job_id：{job.get('job_id')}\n"
            f"  狀態：{job_store.status_label(job.get('status'))}｜更新：{job.get('updated_at')}"
        )
    return "\n\n".join(lines)


def launch_task(task):
    if task.get("is_observation") or task_store.active_work_count() < MAX_ACTIVE_WORK_TASKS:
        result = subprocess.run(
            ["/usr/bin/python3", "/opt/codex-telegram-gateway/task_starter.py", task["task_id"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            task_store.update_task(task["task_id"], status="failed", error=("task_starter failed: " + (result.stdout or ""))[-1000:])
            return False, result.stdout or ""
        return True, result.stdout or ""
    return False, "queued because max active work tasks reached"


def format_tasks():
    tasks = task_store.list_tasks(limit=10)
    if not tasks:
        return "(no tasks)"
    lines = []
    for task in tasks:
        lines.append(
            f"{task.get('title')}\n"
            f"  task_id：{task.get('task_id')}\n"
            f"  狀態：{task.get('status')}｜更新：{task.get('updated_at')}"
        )
    return "\n\n".join(lines)


def _task_id_from_command(text, command):
    return text[len(command) :].strip().split()[0] if text[len(command) :].strip() else None


def finish_job_callback(job, final_report):
    append_recent("assistant", memory_adapter.redact_secrets(final_report), job.get("chat_id"), job.get("user_id"))


def handle_job_route(chat_id, route_result):
    job = router.resolve_job(route_result)
    if not job:
        send_message(chat_id, "找不到可控制的 job。")
        return "找不到可控制的 job。"
    job_id = job.get("job_id")
    intent = route_result.get("intent")
    phase = route_result.get("phase")
    if intent == "job_status":
        reply = job_store.natural_job_status(job)
        send_message(chat_id, reply)
        return reply
    if intent == "job_plan":
        reply = memory_adapter.redact_secrets(json.dumps(job_store.read_plan(job_id), ensure_ascii=False, indent=2))
        send_message(chat_id, reply)
        return reply
    if intent == "job_log":
        phase_id = phase or job.get("current_phase") or "_planner"
        reply = job_store.read_phase_text(job_id, phase_id, "stdout.log") or f"(no stdout.log for {phase_id})"
        send_message(chat_id, reply)
        return reply
    if intent == "job_result":
        if phase:
            result = job_store.read_phase_text(job_id, phase, "result.md") or f"(no result for {phase})"
        else:
            result = job_store.read_final_report(job_id) or "(no final report)"
        send_message(chat_id, result)
        return result
    if intent == "cancel_job":
        orchestrator.cancel_job(job_id)
        reply = f"已要求取消任務：{job.get('title') or job_store.make_title(job.get('user_text'))}\njob_id：{job_id}"
        send_message(chat_id, reply)
        return reply
    if intent == "rerun_phase":
        if not phase:
            plan = job_store.read_plan(job_id) or {}
            failed = []
            for item in plan.get("phases", []):
                phase_id = item.get("id")
                phase_state = job_store.get_phase(job_id, phase_id) or {}
                if phase_state.get("status") in {"failed", "cancelled"}:
                    failed.append(phase_id)
            if failed:
                phase = failed[0]
            else:
                reply = "找不到已中斷的 phase；請指定 explorer / auditor / runner / verifier / reporter。"
                send_message(chat_id, reply)
                return reply
        orchestrator.start_rerun_phase(job_id, phase, send_callback=send_message)
        reply = f"已開始重跑任務「{job.get('title') or job_store.make_title(job.get('user_text'))}」的 {job_store.phase_label(phase)}。\njob_id：{job_id}"
        send_message(chat_id, reply)
        return reply
    return None


def handle_message(message):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    if text in {"/start", "/help"}:
        send_message(
            chat_id,
            "Codex Telegram Gateway online.\n\n"
            "普通訊息會建立 background Codex task，並帶入 facts/recent/cognee 記憶。\n"
            "不使用 Orchestrator/子代理；/job-* 只保留用於查詢舊任務紀錄。\n"
            "Commands: /status, /ping, /whoami, /context, /remember <text>, /reset, /memory-status, "
            "/facts, /fact <key>, /setfact <key> = <value>, /setfact-verified <key> = <value>, "
            "/archivefact <key>, /fact-events <key>, /tasks, /task <id>, /task-log <id>, /task-result <id>, /cancel-task <id>, "
            "/jobs, /job <id>, /job-plan <id>, "
            "/job-log <id> [phase], /job-result <id> [phase], /cancel-job <id>, /rerun-phase <id> <phase>, /approve <id> <phase>",
        )
        return
    if text == "/ping":
        send_message(chat_id, "pong")
        return
    if text == "/whoami":
        send_message(chat_id, f"chat_id={chat_id}\nuser_id={user_id}")
        return
    if text == "/status":
        status = subprocess.run(
            [CODEX_BIN, "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        send_message(chat_id, status.stdout.strip() or f"status exit={status.returncode}")
        return
    if text == "/memory-status":
        send_message(chat_id, format_memory_status())
        return
    if text == "/tasks":
        send_message(chat_id, format_tasks())
        return
    if text.startswith("/task-result"):
        task_id = _task_id_from_command(text, "/task-result")
        if not task_id:
            send_message(chat_id, "用法：/task-result <task_id>")
            return
        send_message(chat_id, task_store.read_task_text(task_id, "result.md") or "(no task result)")
        return
    if text.startswith("/task-log"):
        task_id = _task_id_from_command(text, "/task-log")
        if not task_id:
            send_message(chat_id, "用法：/task-log <task_id>")
            return
        send_message(chat_id, task_store.read_task_text(task_id, "stdout.log") or "(no task stdout.log)")
        return
    if text.startswith("/cancel-task"):
        task_id = _task_id_from_command(text, "/cancel-task")
        if not task_id:
            send_message(chat_id, "用法：/cancel-task <task_id>")
            return
        task = task_store.get_task(task_id)
        if not task:
            send_message(chat_id, "找不到這個 task。")
            return
        task_store.update_task(task_id, status="cancelled", error="cancelled by user")
        unit = task.get("unit_name")
        if unit:
            subprocess.run(["systemctl", "stop", f"{unit}.service"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        send_message(chat_id, f"已要求取消 task。\ntask_id：{task_id}")
        return
    if text.startswith("/task "):
        task_id = _task_id_from_command(text, "/task")
        send_message(chat_id, task_store.natural_task_status(task_store.get_task(task_id)))
        return
    if text == "/jobs":
        send_message(chat_id, format_jobs())
        return
    if text.startswith("/job-plan"):
        job_id = _job_id_from_command(text, "/job-plan")
        if not job_id:
            send_message(chat_id, "用法：/job-plan <job_id>")
            return
        send_message(chat_id, memory_adapter.redact_secrets(json.dumps(job_store.read_plan(job_id), ensure_ascii=False, indent=2)))
        return
    if text.startswith("/job-log"):
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "用法：/job-log <job_id> [phase]")
            return
        job_id = parts[1]
        phase_id = parts[2] if len(parts) >= 3 else "_planner"
        log = job_store.read_phase_text(job_id, phase_id, "stdout.log") or "(no stdout.log)"
        send_message(chat_id, log)
        return
    if text.startswith("/job-result"):
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "用法：/job-result <job_id> [phase]")
            return
        job_id = parts[1]
        if len(parts) >= 3:
            result = job_store.read_phase_text(job_id, parts[2], "result.md") or "(no phase result)"
        else:
            result = job_store.read_final_report(job_id) or job_store.fallback_report(job_id) or "(no final report)"
        send_message(chat_id, result)
        return
    if text.startswith("/cancel-job"):
        job_id = _job_id_from_command(text, "/cancel-job")
        if not job_id:
            send_message(chat_id, "用法：/cancel-job <job_id>")
            return
        orchestrator.cancel_job(job_id)
        send_message(chat_id, f"已要求取消 job {job_id}。running worker 會在下一次檢查點停止。")
        return
    if text.startswith("/rerun-phase"):
        parts = text.split()
        if len(parts) < 3:
            send_message(chat_id, "用法：/rerun-phase <job_id> <phase>")
            return
        orchestrator.start_rerun_phase(parts[1], parts[2], send_callback=send_message)
        send_message(chat_id, f"已開始重跑 job {parts[1]} phase {parts[2]}。")
        return
    if text.startswith("/approve"):
        send_message(chat_id, "目前是授權自動模式，沒有 pending approval gate。")
        return
    if text.startswith("/job "):
        job_id = _job_id_from_command(text, "/job")
        send_message(chat_id, memory_adapter.redact_secrets(job_store.natural_job_status(job_store.get_job(job_id))))
        return
    if text == "/facts":
        send_message(chat_id, memory_adapter.redact_secrets(facts_store.format_facts()))
        return
    if text.startswith("/fact-events"):
        key = text[len("/fact-events") :].strip()
        if not key:
            send_message(chat_id, "用法：/fact-events <key>")
            return
        events = facts_store.fact_events(key=key, limit=10)
        send_message(chat_id, memory_adapter.redact_secrets(json.dumps(events, ensure_ascii=False, indent=2)))
        return
    if text.startswith("/fact "):
        key = text[len("/fact") :].strip()
        fact = facts_store.get_fact(key)
        send_message(chat_id, memory_adapter.redact_secrets(facts_store.format_fact(fact)))
        return
    if text.startswith("/setfact-verified"):
        try:
            key, value = parse_fact_assignment(text, "/setfact-verified")
            fact = facts_store.set_fact(
                key,
                value,
                source="verified_vps_command",
                confidence="verified",
                note=f"set by telegram user {user_id}",
            )
            send_message(chat_id, "已寫入 verified current fact：\n" + facts_store.format_fact(fact))
        except Exception as exc:
            send_message(chat_id, f"未能寫入 fact：{exc}")
        return
    if text.startswith("/setfact"):
        try:
            key, value = parse_fact_assignment(text, "/setfact")
            fact = facts_store.set_fact(
                key,
                value,
                source="telegram_manual",
                confidence="declared",
                note=f"set by telegram user {user_id}",
            )
            send_message(chat_id, "已寫入 declared current fact：\n" + facts_store.format_fact(fact))
        except Exception as exc:
            send_message(chat_id, f"未能寫入 fact：{exc}")
        return
    if text.startswith("/archivefact"):
        key = text[len("/archivefact") :].strip()
        if not key:
            send_message(chat_id, "用法：/archivefact <key>")
            return
        try:
            fact = facts_store.archive_fact(key, source="telegram_manual", note=f"archived by telegram user {user_id}")
            send_message(chat_id, "已封存 fact：\n" + facts_store.format_fact(fact))
        except Exception as exc:
            send_message(chat_id, f"未能封存 fact：{exc}")
        return
    if text == "/context":
        if LAST_CONTEXT_FILE.exists():
            context = LAST_CONTEXT_FILE.read_text(encoding="utf-8", errors="replace")
        else:
            context = json.dumps({"recent": _read_jsonl(RECENT_MESSAGES_FILE, 10)}, ensure_ascii=False, indent=2)
        send_message(chat_id, memory_adapter.redact_secrets(context))
        return
    if text == "/reset":
        clear_recent_memory()
        send_message(chat_id, "短期 Telegram 記憶已清空；cognee 長期記憶未清除。")
        return
    if text.startswith("/remember"):
        fact = text[len("/remember") :].strip()
        if not fact:
            send_message(chat_id, "用法：/remember <要寫入長期記憶的內容>")
            return
        result = memory_adapter.remember_fact(
            fact,
            metadata={"source": "telegram_manual", "chat_id": chat_id, "user_id": user_id},
            session_id=str(chat_id),
            timeout=180,
        )
        if result.get("ok"):
            send_message(chat_id, "已寫入 cognee 長期記憶。")
        else:
            send_message(chat_id, f"未寫入 cognee：{result.get('error') or result}")
        return

    safe_text = memory_adapter.redact_secrets(text)
    if memory_adapter.contains_secret(text):
        send_message(chat_id, "偵測到疑似 secret，已遮蔽後處理，且不會寫入長期記憶。")

    recent_context = format_recent_context()
    append_recent("user", safe_text, chat_id, user_id)
    task = task_store.create_task(safe_text, chat_id, user_id)
    launched, detail = launch_task(task)
    status = (task_store.get_task(task["task_id"]) or task).get("status")
    send_message(
        chat_id,
        f"已建立 background Codex task：{task.get('title')}\n"
        f"task_id：{task.get('task_id')}\n"
        f"狀態：{status}\n"
        f"你可以繼續對話；要查進度可直接問，或用 /task <task_id>。",
    )


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/python3", "/opt/codex-telegram-gateway/task_starter.py"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    api_call("deleteWebhook", {"drop_pending_updates": "false"}, timeout=20)
    me = api_call("getMe", timeout=20)
    print(f"Codex Telegram Gateway started as @{me.get('username')}", flush=True)

    offset = read_offset()
    while not STOP:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            updates = api_call("getUpdates", params, timeout=40)
            for update in updates:
                update_id = update["update_id"]
                offset = update_id + 1
                write_offset(offset)
                if "message" in update:
                    handle_message(update["message"])
        except Exception as exc:
            print(f"gateway error: {exc}", flush=True)
            time.sleep(5)

    print("Codex Telegram Gateway stopped", flush=True)


if __name__ == "__main__":
    main()
