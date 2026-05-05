#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import json
import os
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
LOCK_PATH = Path(os.environ.get("MEMORY_WRITER_LOCK_PATH", "/var/lib/codex-telegram-gateway/memory/memory_writer.lock"))


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


@contextlib.contextmanager
def writer_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def emit(event, payload=None):
    print(json.dumps({"event": event, **(payload or {})}, ensure_ascii=False), flush=True)


def process_once(limit=10):
    load_env_file()
    sys.path.insert(0, str(APP_DIR))
    import memory_adapter
    import memory_queue

    timeout = int(os.environ.get("MEMORY_WRITER_COGNEE_TIMEOUT_SECONDS", "300"))
    processed = 0
    with writer_lock():
        for path, item in memory_queue.due_pending_items(limit=limit):
            memory_id = item.get("memory_id")
            try:
                result = memory_adapter.remember_fact(
                    item.get("text", ""),
                    metadata=item.get("metadata") or {},
                    session_id=str((item.get("metadata") or {}).get("chat_id") or ""),
                    timeout=timeout,
                )
                if result.get("ok"):
                    memory_queue.mark_processed(path, item, result=result)
                    emit("processed", {"memory_id": memory_id})
                else:
                    memory_queue.mark_failed_or_retry(path, item, result.get("error") or result)
                    emit("failed_or_retry", {"memory_id": memory_id, "error": str(result.get("error") or result)[:300]})
            except Exception as exc:
                memory_queue.mark_failed_or_retry(path, item, str(exc))
                emit("failed_or_retry", {"memory_id": memory_id, "error": str(exc)[:300]})
            processed += 1
    return processed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    count = process_once(limit=args.limit)
    emit("done", {"processed_count": count})


if __name__ == "__main__":
    main()
