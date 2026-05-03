#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
STOP = False


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


load_env_file()
sys.path.insert(0, str(APP_DIR))

import memory_adapter  # noqa: E402
import task_store  # noqa: E402
import telegram_sender  # noqa: E402


POLL_SECONDS = int(os.environ.get("TASK_SUPERVISOR_INTERVAL_SECONDS", "60"))
TASK_STALE_SECONDS = int(os.environ.get("TASK_STALE_SECONDS", "900"))
WATCH_STALE_SECONDS = int(os.environ.get("WATCH_STALE_SECONDS", "900"))
SUPERVISION_COOLDOWN_SECONDS = int(os.environ.get("SUPERVISION_COOLDOWN_SECONDS", "900"))
CODEX_WATCHER_INTERVAL_SECONDS = int(os.environ.get("CODEX_WATCHER_INTERVAL_SECONDS", "300"))
MAX_SUPERVISION_TASKS_PER_PARENT = int(os.environ.get("MAX_SUPERVISION_TASKS_PER_PARENT", "3"))
MAX_ACTIVE_WORK_TASKS = int(os.environ.get("MAX_ACTIVE_WORK_TASKS", "2"))


def handle_signal(_signum, _frame):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def parse_ts(value):
    if not value:
        return 0.0
    try:
        return time.mktime(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0.0


def age_seconds(value):
    parsed = parse_ts(value)
    if not parsed:
        return 10**9
    return max(0.0, time.time() - parsed)


def file_age(path):
    if not path:
        return None
    try:
        return max(0.0, time.time() - Path(path).stat().st_mtime)
    except Exception:
        return None


def file_nonempty(path):
    if not path:
        return False
    try:
        return Path(path).is_file() and Path(path).stat().st_size > 0
    except Exception:
        return False


def read_text_tail(path, limit=3000):
    if not path:
        return ""
    try:
        return memory_adapter.redact_secrets(Path(path).read_text(encoding="utf-8", errors="replace")[-limit:])
    except Exception:
        return ""


def pid_alive(pid):
    try:
        pid = int(str(pid).strip())
    except Exception:
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def pid_from_watch(watch):
    if watch.get("pid"):
        return watch.get("pid")
    pid_file = watch.get("pid_file")
    if pid_file:
        try:
            return Path(pid_file).read_text(encoding="utf-8", errors="replace").strip().split()[0]
        except Exception:
            return None
    return None


def unit_active(unit_name):
    if not unit_name:
        return None
    unit = str(unit_name)
    if not unit.endswith(".service"):
        unit = unit + ".service"
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() == "active"


def supervision_state(task_id):
    return task_store.read_task_json(task_id, "supervisor_state.json", default={}) or {}


def write_supervision_state(task_id, state):
    task_store.write_task_json(task_id, "supervisor_state.json", state)


def active_child_task(parent_id, reasons=None):
    reasons = set(reasons or [])
    for task in task_store.list_tasks(limit=1000):
        if task.get("supervisor_for") != parent_id:
            continue
        if reasons and task.get("supervisor_reason") not in reasons:
            continue
        if task.get("status") in task_store.ACTIVE_STATUSES:
            return task
    return None


def can_spawn_supervision(task_id, reason):
    state = supervision_state(task_id)
    incidents = state.setdefault("incidents", {})
    item = incidents.setdefault(reason, {"count": 0, "last_ts": 0})
    now = time.time()
    if item["count"] >= MAX_SUPERVISION_TASKS_PER_PARENT:
        return False, state
    if now - float(item.get("last_ts") or 0) < SUPERVISION_COOLDOWN_SECONDS:
        return False, state
    item["count"] += 1
    item["last_ts"] = now
    return True, state


def can_spawn_periodic_watch(task_id):
    state = supervision_state(task_id)
    now = time.time()
    last_ts = float(state.get("last_codex_watch_spawn_ts") or 0)
    if now - last_ts < CODEX_WATCHER_INTERVAL_SECONDS:
        return False, state
    state["last_codex_watch_spawn_ts"] = now
    state["codex_watch_count"] = int(state.get("codex_watch_count") or 0) + 1
    return True, state


def start_task(task):
    result = subprocess.run(
        [sys.executable, str(APP_DIR / "task_starter.py"), task["task_id"]],
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0, result.stdout[-1000:]


def spawn_supervision_task(parent, reason, evidence):
    parent_id = parent.get("task_id")
    existing = active_child_task(parent_id, reasons={reason})
    if existing:
        task_store.append_event(
            parent_id,
            "supervision_existing_active",
            {"child_task_id": existing.get("task_id"), "reason": reason},
        )
        return existing.get("task_id")
    allowed, state = can_spawn_supervision(parent_id, reason)
    if not allowed:
        task_store.append_event(parent_id, "supervision_suppressed", {"reason": reason})
        write_supervision_state(parent_id, state)
        return None

    user_text = f"""你是 Codex Telegram Gateway 的通用 task supervisor。

父 task 需要工程監護：
task_id={parent_id}
title={parent.get('title')}
status={parent.get('status')}
reason={reason}

證據：
{evidence}

請只處理這個父 task 的監護，不要做無關工作。

你必須：
1. 讀取父 task folder、watch.json、progress file、runner log、stderr/stdout、final report。
2. 查 root cause：程式錯誤、資源/OOM、timeout、Telegram/Codex lifecycle、資料缺失、權限或命令問題。
3. 若安全，修復 checkpoint/resume/heartbeat/supervisor 後重啟或恢復。
4. 若不可安全補救，把父 task 明確標記 failed，寫出原因與下一步。
5. 回覆 Telegram 時先用一句人話講結論，再列證據。

嚴禁無腦重跑同一條會再次中斷的命令。
"""
    child = task_store.create_task(user_text, parent.get("chat_id"), parent.get("user_id"))
    child_id = child["task_id"]
    task_store.update_task(
        child_id,
        is_observation=True,
        title=f"監護補救：{parent.get('title')}",
        supervisor_for=parent_id,
        supervisor_reason=reason,
    )
    task_store.update_task(parent_id, status="needs_attention", supervisor_child_task_id=child_id)
    task_store.append_event(parent_id, "supervision_task_created", {"child_task_id": child_id, "reason": reason})
    task_store.append_thread_message(
        parent_id,
        "supervisor_thread.jsonl",
        "supervisor",
        f"Created supervision task {child_id} for reason={reason}.\n\nEvidence:\n{evidence}",
        {"child_task_id": child_id, "reason": reason},
    )
    write_supervision_state(parent_id, state)
    start_task(task_store.get_task(child_id))
    return child_id


def spawn_periodic_watch_task(parent, reason, evidence):
    parent_id = parent.get("task_id")
    existing = active_child_task(parent_id, reasons={"periodic_codex_watch", "final_report_detected"})
    if existing:
        task_store.append_event(
            parent_id,
            "codex_watch_existing_active",
            {"child_task_id": existing.get("task_id"), "reason": existing.get("supervisor_reason")},
        )
        return existing.get("task_id")
    allowed, state = can_spawn_periodic_watch(parent_id)
    if not allowed:
        return None

    user_text = f"""你是 Codex Telegram Gateway 的定期 watcher。Python supervisor 只負責每 5 分鐘喚醒你；真正的工程判斷由你做。

父 task 未完成，需要你巡檢：
task_id={parent_id}
title={parent.get('title')}
status={parent.get('status')}
reason={reason}

機械證據：
{evidence}

請只巡檢這個父 task，不要做無關工作。

你必須：
1. 讀取父 task folder、watch.json、progress file、heartbeat、runner log、stderr/stdout、final report path。
2. 判斷現在是正常推進、假 running、stale、已完成、還是明確失敗。
3. 若仍正常推進，只用簡短繁體中文回報「目前進度 + 證據 + 下次應檢查什麼」。
4. 若 final report 已存在，讀取並總結結果；不要只說檔案存在。
5. 若卡住或失敗，說清楚 root cause 證據與下一步；只有在 resource lock 不衝突且 resume_command 明確安全時才可補救。
6. 不要啟動第二個相同 runner；先查 resource lock 和既有 PID。

這是 watcher，不是新任務；不要擴大範圍。
"""
    child = task_store.create_task(user_text, parent.get("chat_id"), parent.get("user_id"))
    child_id = child["task_id"]
    task_store.update_task(
        child_id,
        is_observation=True,
        title=f"監護巡檢：{parent.get('title')}",
        supervisor_for=parent_id,
        supervisor_reason=reason,
    )
    task_store.append_event(parent_id, "codex_watch_task_created", {"child_task_id": child_id, "reason": reason})
    task_store.append_thread_message(
        parent_id,
        "supervisor_thread.jsonl",
        "supervisor",
        f"Created periodic Codex watcher task {child_id} for reason={reason}.\n\nEvidence:\n{evidence}",
        {"child_task_id": child_id, "reason": reason},
    )
    write_supervision_state(parent_id, state)
    start_task(task_store.get_task(child_id))
    return child_id


def append_main_recent(task, text):
    if not task.get("chat_id"):
        return
    try:
        import gateway

        gateway.append_recent("assistant", text, task.get("chat_id"), task.get("user_id"))
    except Exception as exc:
        task_store.append_event(task.get("task_id"), "main_recent_append_failed", {"error": f"{type(exc).__name__}: {exc}"})


def finalize_watched_task(task, watch):
    task_id = task["task_id"]
    report_path = watch.get("final_report") or watch.get("final_report_path") or watch.get("report_path")
    report_tail = read_text_tail(report_path, limit=5000)
    text = (
        "任務完成：supervisor 已偵測到 final report。\n\n"
        f"父 task：{task.get('title')}\n"
        f"task_id：{task_id}\n"
        f"final_report：{report_path}\n\n"
        f"{report_tail or '(final report exists but could not be read)'}"
    )
    safe = memory_adapter.redact_secrets(text)
    task_store.write_task_text(task_id, "result.md", safe)
    task_store.update_task(task_id, status="succeeded", error=None, supervisor_completed_at=task_store.utc_now())
    task_store.append_thread_message(task_id, "thread.jsonl", "assistant", safe, {"source": "supervisor_finalized"})
    task_store.append_event(task_id, "supervisor_finalized", {"final_report": report_path})
    released = task_store.release_resource_locks_for_task(task_id, reason="supervisor_finalized")
    if released:
        task_store.append_event(task_id, "resource_locks_released", {"resource_keys": released})
    append_main_recent(task, f"任務已完成：{task.get('title')}\ntask_id：{task_id}\nfinal_report：{report_path}")
    if task.get("chat_id"):
        telegram_sender.send_message(task.get("chat_id"), safe)


def inspect_watch(task):
    task_id = task["task_id"]
    watch = task_store.read_task_json(task_id, "watch.json", default={}) or {}
    if not isinstance(watch, dict) or not watch.get("enabled", False):
        return

    lock_ok, lock, owner = task_store.acquire_resource_lock(task, watch)
    if not lock_ok:
        evidence = json.dumps(
            {
                "blocked_task_id": task_id,
                "blocked_title": task.get("title"),
                "resource_key": (lock or {}).get("resource_key"),
                "owner_task_id": owner.get("task_id") if owner else None,
                "owner_title": owner.get("title") if owner else None,
                "owner_status": owner.get("status") if owner else None,
                "lock": lock,
            },
            ensure_ascii=False,
            indent=2,
        )
        task_store.update_task(task_id, status="blocked_by_resource", error="resource is locked by another active task")
        task_store.append_event(task_id, "resource_lock_blocked", {"lock": lock, "owner_task_id": owner.get("task_id") if owner else None})
        spawn_supervision_task(task, "resource_conflict", evidence)
        return
    if lock:
        watch["resource_key"] = lock.get("resource_key")
        watch["lock_file"] = lock.get("lock_file")
        task_store.write_task_json(task_id, "watch.json", watch)

    final_report = watch.get("final_report") or watch.get("final_report_path") or watch.get("report_path")
    if file_nonempty(final_report):
        finalize_watched_task(task, watch)
        return

    pid = pid_from_watch(watch)
    alive = pid_alive(pid)
    paths = [
        watch.get("progress_file"),
        watch.get("heartbeat_file"),
        watch.get("log_file"),
        watch.get("stderr_file"),
        watch.get("stdout_file"),
    ]
    ages = [age for age in (file_age(path) for path in paths) if age is not None]
    freshest_age = min(ages) if ages else None
    stale_after = int(watch.get("stale_after_seconds") or WATCH_STALE_SECONDS)
    fresh = freshest_age is not None and freshest_age <= stale_after

    state = supervision_state(task_id)
    state["last_watch_check_at"] = task_store.utc_now()
    state["pid"] = pid
    state["pid_alive"] = alive
    state["freshest_file_age_seconds"] = round(freshest_age, 2) if freshest_age is not None else None
    write_supervision_state(task_id, state)

    if alive and fresh:
        if age_seconds(task.get("updated_at")) > min(stale_after, 300):
            task_store.update_task(task_id, status="watching", error=None)
        evidence = json.dumps(
            {
                "pid": pid,
                "pid_alive": alive,
                "freshest_file_age_seconds": round(freshest_age, 2) if freshest_age is not None else None,
                "stale_after_seconds": stale_after,
                "final_report_exists": file_nonempty(final_report),
                "watch": watch,
            },
            ensure_ascii=False,
            indent=2,
        )
        spawn_periodic_watch_task(task_store.get_task(task_id) or task, "periodic_codex_watch", evidence)
        return

    evidence = json.dumps(
        {
            "pid": pid,
            "pid_alive": alive,
            "freshest_file_age_seconds": round(freshest_age, 2) if freshest_age is not None else None,
            "stale_after_seconds": stale_after,
            "final_report_exists": file_nonempty(final_report),
            "watch": watch,
        },
        ensure_ascii=False,
        indent=2,
    )
    reason = "watch_pid_dead" if not alive else "watch_stale"
    spawn_supervision_task(task, reason, evidence)


def inspect_active_task(task):
    status = task.get("status")
    task_id = task.get("task_id")
    if status == "queued":
        if not task.get("is_observation") and task_store.active_work_count() >= MAX_ACTIVE_WORK_TASKS:
            return
        ok, output = start_task(task)
        if not ok:
            task_store.update_task(task_id, status="failed", error=("supervisor launch failed: " + output)[-1000:])
        return

    if status == "watching":
        inspect_watch(task)
        return

    if status == "needs_attention":
        watch = task_store.read_task_json(task_id, "watch.json", default={}) or {}
        if isinstance(watch, dict) and watch.get("enabled", False):
            inspect_watch(task)
        return

    if status == "blocked_by_resource":
        return

    if status in {"starting", "running", "finalizing"}:
        active = unit_active(task.get("unit_name"))
        if active is True:
            return
        if age_seconds(task.get("updated_at")) <= TASK_STALE_SECONDS:
            return
        evidence = json.dumps(
            {
                "status": status,
                "unit_name": task.get("unit_name"),
                "unit_active": active,
                "updated_at": task.get("updated_at"),
                "age_seconds": round(age_seconds(task.get("updated_at")), 2),
                "stdout_tail": task_store.read_task_text(task_id, "stdout.log", limit_chars=2000),
                "stderr_tail": task_store.read_task_text(task_id, "stderr.log", limit_chars=2000),
            },
            ensure_ascii=False,
            indent=2,
        )
        spawn_supervision_task(task, "task_unit_inactive_or_stale", evidence)


def supervisor_pass():
    subprocess.run(
        [sys.executable, str(APP_DIR / "task_starter.py")],
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    for task in task_store.list_tasks(limit=1000):
        if task.get("status") in task_store.FINAL_STATUSES:
            task_store.release_resource_locks_for_task(task.get("task_id"), reason=f"task_{task.get('status')}")
        if task.get("status") in task_store.ACTIVE_STATUSES:
            try:
                inspect_active_task(task)
            except Exception as exc:
                task_store.append_event(task.get("task_id"), "supervisor_error", {"error": f"{type(exc).__name__}: {exc}"})


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--once":
        supervisor_pass()
        return
    print("Codex Telegram task supervisor started", flush=True)
    while not STOP:
        supervisor_pass()
        for _ in range(max(1, POLL_SECONDS)):
            if STOP:
                break
            time.sleep(1)
    print("Codex Telegram task supervisor stopped", flush=True)


if __name__ == "__main__":
    main()
