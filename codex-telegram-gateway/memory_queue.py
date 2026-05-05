#!/usr/bin/env python3
import contextlib
import fcntl
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import memory_adapter


STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
MEMORY_DIR = STATE_DIR / "memory"
WRITE_QUEUE_DIR = MEMORY_DIR / "write_queue"
PENDING_DIR = WRITE_QUEUE_DIR / "pending"
PROCESSED_DIR = WRITE_QUEUE_DIR / "processed"
FAILED_DIR = WRITE_QUEUE_DIR / "failed"
CONVERSATION_BATCHES_DIR = MEMORY_DIR / "conversation_batches"
RECENT_STATE_FILE = MEMORY_DIR / "recent_state.json"
RECENT_STATE_LOCK_FILE = MEMORY_DIR / "recent_state.lock"


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def epoch_now():
    return int(time.time())


def safe_name(value):
    raw = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    return safe[:120] or "item"


def atomic_write_text(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(memory_adapter.redact_secrets(str(text)), encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, mode)


def write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def text_hash(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def read_recent_state():
    data = read_json(RECENT_STATE_FILE, default={})
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def recent_state_lock():
    RECENT_STATE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RECENT_STATE_LOCK_FILE.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_recent_state(state):
    state = dict(state or {})
    state["updated_at"] = utc_now()
    write_json(RECENT_STATE_FILE, state)


def next_recent_seq():
    with recent_state_lock():
        state = read_recent_state()
        seq = int(state.get("next_seq") or 1)
        state["next_seq"] = seq + 1
        write_recent_state(state)
        return seq


def enqueue_memory(text, metadata=None, source="unknown", max_attempts=3):
    safe_text = memory_adapter.redact_secrets(text)
    if memory_adapter.contains_secret(text):
        return {"ok": False, "skipped": True, "error": "secret-like content was not queued for cognee"}
    if not safe_text.strip():
        return {"ok": False, "skipped": True, "error": "empty memory text"}

    digest = text_hash(safe_text)
    memory_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}-{digest[:10]}"
    item = {
        "memory_id": memory_id,
        "status": "pending",
        "source": source,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "attempts": 0,
        "max_attempts": int(max_attempts),
        "text_hash": digest,
        "text": safe_text,
        "metadata": metadata or {},
        "next_attempt_at": None,
        "last_error": None,
    }
    path = PENDING_DIR / f"{safe_name(memory_id)}.json"
    write_json(path, item)
    return {"ok": True, "queued": True, "memory_id": memory_id, "path": str(path)}


def _json_files(path):
    path.mkdir(parents=True, exist_ok=True)
    return sorted([p for p in path.glob("*.json") if p.is_file()], key=lambda p: p.stat().st_mtime)


def due_pending_items(limit=10):
    now = epoch_now()
    items = []
    for path in _json_files(PENDING_DIR):
        item = read_json(path, default={})
        if not isinstance(item, dict):
            continue
        next_attempt_at = item.get("next_attempt_at")
        if next_attempt_at and int(next_attempt_at) > now:
            continue
        items.append((path, item))
        if len(items) >= limit:
            break
    return items


def mark_processed(path, item, result=None):
    item = dict(item or {})
    item["status"] = "processed"
    item["processed_at"] = utc_now()
    item["updated_at"] = utc_now()
    item["result"] = result or {}
    target = PROCESSED_DIR / path.name
    write_json(target, item)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return target


def mark_failed_or_retry(path, item, error):
    item = dict(item or {})
    attempts = int(item.get("attempts") or 0) + 1
    max_attempts = int(item.get("max_attempts") or 3)
    item["attempts"] = attempts
    item["last_error"] = str(error or "unknown error")[:1000]
    item["updated_at"] = utc_now()
    if attempts < max_attempts:
        item["status"] = "pending_retry"
        item["next_attempt_at"] = epoch_now() + min(900, 60 * attempts)
        write_json(path, item)
        return path
    item["status"] = "failed"
    item["failed_at"] = utc_now()
    target = FAILED_DIR / path.name
    write_json(target, item)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return target


def latest_json(path):
    files = _json_files(path)
    return read_json(files[-1], default={}) if files else None


def queue_status():
    pending = _json_files(PENDING_DIR)
    processed = _json_files(PROCESSED_DIR)
    failed = _json_files(FAILED_DIR)
    batches = _json_files(CONVERSATION_BATCHES_DIR)
    batch_counts = {}
    for path in batches:
        batch = read_json(path, default={})
        status = batch.get("status", "unknown") if isinstance(batch, dict) else "unknown"
        batch_counts[status] = batch_counts.get(status, 0) + 1
    latest_processed = latest_json(PROCESSED_DIR)
    latest_failed = latest_json(FAILED_DIR)
    return {
        "write_queue_pending": len(pending),
        "write_queue_processed": len(processed),
        "write_queue_failed": len(failed),
        "write_queue_latest_processed_at": latest_processed.get("processed_at") if latest_processed else None,
        "write_queue_latest_failed_at": latest_failed.get("failed_at") if latest_failed else None,
        "write_queue_last_error": latest_failed.get("last_error") if latest_failed else None,
        "conversation_batches": batch_counts,
        "recent_state": read_recent_state(),
    }


def create_conversation_batch(messages, related_task_ids, from_seq, to_seq):
    clean_messages = []
    for item in messages:
        clean = dict(item)
        clean["text"] = memory_adapter.redact_secrets(clean.get("text", ""))
        clean_messages.append(clean)
    digest = text_hash(json.dumps(clean_messages, ensure_ascii=False))
    batch_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{digest[:12]}"
    payload = {
        "batch_id": batch_id,
        "status": "pending_curator",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "started_at": clean_messages[0].get("ts") if clean_messages else None,
        "ended_at": clean_messages[-1].get("ts") if clean_messages else None,
        "turn_count": len([m for m in clean_messages if m.get("role") == "assistant"]),
        "from_seq": from_seq,
        "to_seq": to_seq,
        "related_task_ids": sorted(set(str(t) for t in related_task_ids if t)),
        "messages": clean_messages,
        "curator_result": None,
        "memory_queue_result": None,
        "error": None,
    }
    path = CONVERSATION_BATCHES_DIR / f"{safe_name(batch_id)}.json"
    write_json(path, payload)
    state = read_recent_state()
    state["last_batched_seq"] = int(to_seq)
    state["last_batch_id"] = batch_id
    state["last_batch_at"] = utc_now()
    write_recent_state(state)
    return payload


def pending_batches(limit=3):
    items = []
    for path in _json_files(CONVERSATION_BATCHES_DIR):
        batch = read_json(path, default={})
        if isinstance(batch, dict) and batch.get("status") == "pending_curator":
            items.append((path, batch))
            if len(items) >= limit:
                break
    return items


def update_batch(path_or_id, **fields):
    path = Path(path_or_id)
    if not path.exists():
        path = CONVERSATION_BATCHES_DIR / f"{safe_name(path_or_id)}.json"
    batch = read_json(path, default={}) or {}
    batch.update(fields)
    batch["updated_at"] = utc_now()
    write_json(path, batch)
    return batch
