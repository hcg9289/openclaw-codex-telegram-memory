#!/usr/bin/env python3
import json
import os
import re
import time
import uuid
from pathlib import Path

import memory_adapter


STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
TASKS_DIR = STATE_DIR / "tasks"
ACTIVE_STATUSES = {"queued", "starting", "running", "watching", "needs_attention", "finalizing"}
FINAL_STATUSES = {"succeeded", "failed", "cancelled"}
OBSERVE_WORDS = [
    "剛才",
    "上一個",
    "最近",
    "進度",
    "做到哪",
    "結果",
    "報告",
    "log",
    "日誌",
    "卡住",
    "task",
    "task_id",
]


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_task_id():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def safe_name(value):
    value = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe[:80] or "task"


def task_dir(task_id):
    return TASKS_DIR / safe_name(task_id)


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(memory_adapter.redact_secrets(str(text)), encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, mode)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def is_observation_text(text):
    lowered = str(text or "").lower()
    return any(word.lower() in lowered for word in OBSERVE_WORDS)


def make_title(text, max_len=36):
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return "未命名任務"
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len].rstrip() + "..."


def create_task(user_text, chat_id, user_id):
    task_id = new_task_id()
    root = task_dir(task_id)
    root.mkdir(parents=True, exist_ok=True)
    task = {
        "task_id": task_id,
        "status": "queued",
        "title": make_title(user_text),
        "chat_id": chat_id,
        "user_id": user_id,
        "user_text": memory_adapter.redact_secrets(user_text),
        "is_observation": is_observation_text(user_text),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "unit_name": None,
    }
    write_json(root / "task.json", task)
    write_json(root / "status.json", task)
    append_event(task_id, "created", {"title": task["title"], "is_observation": task["is_observation"]})
    return task


def get_task(task_id):
    return read_json(task_dir(task_id) / "task.json")


def update_task(task_id, **fields):
    task = get_task(task_id) or {"task_id": task_id, "created_at": utc_now()}
    task.update(fields)
    task["updated_at"] = utc_now()
    if task.get("status") in FINAL_STATUSES and not task.get("finished_at"):
        task["finished_at"] = utc_now()
    write_json(task_dir(task_id) / "task.json", task)
    write_json(task_dir(task_id) / "status.json", task)
    return task


def append_event(task_id, event_type, payload=None):
    item = {
        "ts": utc_now(),
        "type": event_type,
        "payload": payload or {},
    }
    path = task_dir(task_id) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_task_text(task_id, filename, text):
    atomic_write(task_dir(task_id) / filename, text)


def append_task_text(task_id, filename, text):
    path = task_dir(task_id) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(memory_adapter.redact_secrets(str(text)))


def read_task_text(task_id, filename, limit_chars=12000):
    path = task_dir(task_id) / filename
    if not path.exists():
        return ""
    return memory_adapter.redact_secrets(path.read_text(encoding="utf-8", errors="replace")[-limit_chars:])


def read_events_tail(task_id, limit=20):
    path = task_dir(task_id) / "events.jsonl"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    return memory_adapter.redact_secrets("\n".join(lines))


def read_task_json(task_id, filename, default=None):
    return read_json(task_dir(task_id) / filename, default=default)


def write_task_json(task_id, filename, data):
    write_json(task_dir(task_id) / filename, data)


def list_tasks(limit=10, include_system=True):
    if not TASKS_DIR.exists():
        return []
    tasks = []
    for path in TASKS_DIR.iterdir():
        if not path.is_dir():
            continue
        task = read_json(path / "task.json")
        if task:
            tasks.append(task)
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return tasks[:limit]


def active_work_count():
    return sum(
        1
        for task in list_tasks(limit=1000)
        if task.get("status") in {"starting", "running"} and not task.get("is_observation")
    )


def queued_work_tasks(limit=10):
    return [
        task
        for task in reversed(list_tasks(limit=1000))
        if task.get("status") == "queued" and not task.get("is_observation")
    ][:limit]


def find_task_id_in_text(text):
    match = re.search(r"\b(20\d{6}T\d{6}Z-[0-9a-f]{8})\b", str(text or ""))
    return match.group(1) if match else None


def latest_relevant_task(exclude_task_id=None):
    for task in list_tasks(limit=100):
        if task.get("task_id") == exclude_task_id:
            continue
        if str(task.get("chat_id")) in {"0", "None"}:
            continue
        return task
    return None


def resolve_referenced_task(user_text, exclude_task_id=None):
    explicit = find_task_id_in_text(user_text)
    if explicit and get_task(explicit):
        return get_task(explicit)
    if is_observation_text(user_text):
        return latest_relevant_task(exclude_task_id=exclude_task_id)
    return None


def task_context(task_id, limit_chars=12000):
    task = get_task(task_id)
    if not task:
        return "(referenced task not found)"
    parts = [
        f"task_id={task.get('task_id')}",
        f"title={task.get('title')}",
        f"status={task.get('status')}",
        f"created_at={task.get('created_at')}",
        f"updated_at={task.get('updated_at')}",
        f"error={task.get('error')}",
        f"user_text={task.get('user_text')}",
        "",
        "[watch_json]",
        json.dumps(read_task_json(task_id, "watch.json", default={}) or {}, ensure_ascii=False, indent=2),
        "[/watch_json]",
        "",
        "[supervisor_state]",
        json.dumps(read_task_json(task_id, "supervisor_state.json", default={}) or {}, ensure_ascii=False, indent=2),
        "[/supervisor_state]",
        "",
        "[events_tail]",
        read_events_tail(task_id, limit=20) or "(empty)",
        "[/events_tail]",
        "",
        "[stdout_tail]",
        read_task_text(task_id, "stdout.log", limit_chars=4000) or "(empty)",
        "[/stdout_tail]",
        "",
        "[result_tail]",
        read_task_text(task_id, "result.md", limit_chars=5000) or "(empty)",
        "[/result_tail]",
    ]
    return "\n".join(parts)[-limit_chars:]


def natural_task_status(task):
    if not task:
        return "找不到這個 task。"
    lines = [
        f"任務：{task.get('title')}",
        f"task_id：{task.get('task_id')}",
        f"狀態：{task.get('status')}",
        f"建立時間：{task.get('created_at')}",
        f"更新時間：{task.get('updated_at')}",
    ]
    if task.get("started_at"):
        lines.append(f"開始時間：{task.get('started_at')}")
    if task.get("finished_at"):
        lines.append(f"完成時間：{task.get('finished_at')}")
    if task.get("error"):
        lines.append(f"原因：{task.get('error')}")
    return memory_adapter.redact_secrets("\n".join(lines))
