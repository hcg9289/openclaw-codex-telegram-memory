#!/usr/bin/env python3
import json
import calendar
import os
import re
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
import codex_session  # noqa: E402
import task_store  # noqa: E402
import telegram_sender  # noqa: E402


POLL_SECONDS = int(os.environ.get("TASK_SUPERVISOR_INTERVAL_SECONDS", "60"))
TASK_STALE_SECONDS = int(os.environ.get("TASK_STALE_SECONDS", "900"))
WATCH_STALE_SECONDS = int(os.environ.get("WATCH_STALE_SECONDS", "900"))
SUPERVISION_COOLDOWN_SECONDS = int(os.environ.get("SUPERVISION_COOLDOWN_SECONDS", "300"))
CODEX_WATCHER_INTERVAL_SECONDS = int(os.environ.get("CODEX_WATCHER_INTERVAL_SECONDS", "600"))
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
        return float(calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")))
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


def file_snapshot(path):
    if not path:
        return None
    try:
        stat = Path(path).stat()
        return {
            "path": str(path),
            "mtime_ns": int(stat.st_mtime_ns),
            "size": int(stat.st_size),
        }
    except Exception:
        return None


def read_text_tail(path, limit=3000):
    if not path:
        return ""
    try:
        return memory_adapter.redact_secrets(Path(path).read_text(encoding="utf-8", errors="replace")[-limit:])
    except Exception:
        return ""


def parse_final_report_status(text):
    report = text or ""
    status = None
    failure_reason = None
    for raw_line in report.splitlines()[:80]:
        line = raw_line.strip()
        match = re.match(r"[-*]?\s*status\s*:\s*([A-Za-z0-9_.-]+)\s*$", line, re.IGNORECASE)
        if match:
            status = match.group(1).strip().lower()
            continue
        match = re.match(r"[-*]?\s*failure_reason\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            failure_reason = match.group(1).strip()
    if status in {"failed", "failure", "error", "cancelled", "canceled"}:
        return "failed", failure_reason or status
    if failure_reason and failure_reason.lower() not in {"none", "null", "n/a", "-"}:
        return "failed", failure_reason
    if status in {"succeeded", "success", "completed", "complete", "done"}:
        return "succeeded", None
    return "unknown", failure_reason


def read_json_file(path):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def number_from(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def format_duration(seconds):
    if seconds is None:
        return None
    try:
        seconds = max(0, int(float(seconds)))
    except Exception:
        return None
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def progress_metrics(watch, state):
    path = watch.get("progress_file") if isinstance(watch, dict) else None
    data = read_json_file(path)
    if not isinstance(data, dict):
        return {"available": False, "path": path}

    completed = number_from(data, "completed", "done", "processed", "current", "finished")
    total = number_from(data, "total", "total_items", "total_tasks", "target")
    elapsed = number_from(data, "elapsed_sec", "elapsed_seconds", "runtime_seconds")
    now = time.time()
    percent = None
    eta_seconds = None
    eta_source = None
    if completed is not None and total and total > 0:
        percent = min(100.0, max(0.0, completed / total * 100.0))
        remaining = max(0.0, total - completed)
        previous = state.get("progress_snapshot") if isinstance(state, dict) else None
        if isinstance(previous, dict):
            prev_completed = number_from(previous, "completed")
            prev_ts = number_from(previous, "ts")
            if prev_completed is not None and prev_ts is not None:
                delta_completed = completed - prev_completed
                delta_seconds = now - prev_ts
                if delta_completed > 0 and delta_seconds > 0:
                    eta_seconds = remaining / (delta_completed / delta_seconds)
                    eta_source = "recent_rate"
        if eta_seconds is None and elapsed and elapsed > 0 and completed > 0:
            eta_seconds = remaining / (completed / elapsed)
            eta_source = "average_rate"

    summary = None
    if completed is not None and total:
        summary = f"{int(completed)}/{int(total)}"
        if percent is not None:
            summary += f" ({percent:.1f}%)"
        if eta_seconds is not None:
            summary += f", ETA about {format_duration(eta_seconds)}"

    metrics = {
        "available": True,
        "path": path,
        "stage": data.get("stage"),
        "status": data.get("status"),
        "updated_at": data.get("updated_at"),
        "completed": int(completed) if completed is not None else None,
        "total": int(total) if total is not None else None,
        "percent": round(percent, 2) if percent is not None else None,
        "elapsed_sec": round(elapsed, 2) if elapsed is not None else None,
        "eta_seconds": round(eta_seconds, 2) if eta_seconds is not None else None,
        "eta_human": format_duration(eta_seconds) if eta_seconds is not None else None,
        "eta_source": eta_source,
        "summary": summary,
    }
    if completed is not None:
        state["progress_snapshot"] = {"completed": completed, "total": total, "ts": now}
    return metrics


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


def unit_control_group(unit_name):
    if not unit_name:
        return ""
    unit = str(unit_name)
    if not unit.endswith(".service"):
        unit = unit + ".service"
    result = subprocess.run(
        ["systemctl", "show", unit, "-p", "ControlGroup", "--value", "--no-pager"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    return (result.stdout or "").strip()


def unit_process_snapshot(unit_name):
    control_group = unit_control_group(unit_name)
    if not control_group:
        return []
    procs_file = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"
    try:
        pids = [int(raw.strip()) for raw in procs_file.read_text(encoding="utf-8", errors="replace").splitlines() if raw.strip()]
    except Exception:
        return []
    if not pids:
        return []
    result = subprocess.run(
        ["ps", "-o", "pid=,ppid=,comm=,args=", "-p", ",".join(str(pid) for pid in pids)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": ppid,
                "comm": parts[2],
                "cmd": parts[3] if len(parts) >= 4 else "",
            }
        )
    return rows


def process_is_codex_or_worker(row, task_id):
    cmd = row.get("cmd", "")
    comm = row.get("comm", "")
    needle = str(task_id or "")
    if "task_worker.py" in cmd and (not needle or needle in cmd):
        return True
    if comm in {"codex", "node"}:
        if " codex exec " in f" {cmd} " or "/codex exec " in cmd or comm == "codex":
            return True
    return False


def task_has_codex_process(task):
    if isinstance(task, dict):
        task_id = task.get("task_id")
        unit_name = task.get("unit_name")
        unit_rows = unit_process_snapshot(unit_name)
        if any(process_is_codex_or_worker(row, task_id) for row in unit_rows):
            return True
    else:
        task_id = task
        unit_rows = []
    if not task_id:
        return False
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm=,cmd="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    rows = {}
    children = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        comm = parts[2]
        cmd = parts[3]
        rows[pid] = {"ppid": ppid, "comm": comm, "cmd": cmd}
        children.setdefault(ppid, []).append(pid)

    needle = str(task_id)
    roots = [
        pid
        for pid, row in rows.items()
        if "task_worker.py" in row["cmd"] and needle in row["cmd"]
    ]
    if not roots:
        return False

    stack = list(roots)
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = rows.get(pid) or {}
        cmd = row.get("cmd", "")
        comm = row.get("comm", "")
        if process_is_codex_or_worker({"cmd": cmd, "comm": comm}, task_id):
            return True
        stack.extend(children.get(pid, []))
    return False


def stop_task_unit(task):
    unit_name = task.get("unit_name") if isinstance(task, dict) else None
    if not unit_name:
        return False
    unit = str(unit_name)
    if not unit.endswith(".service"):
        unit = unit + ".service"
    subprocess.run(
        ["systemctl", "stop", unit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    return True


def candidate_output_dirs(task_id, task):
    texts = [
        task.get("user_text") if isinstance(task, dict) else "",
        task_store.read_task_text(task_id, "prompt.md", limit_chars=60000),
        task_store.read_task_text(task_id, "stdout.log", limit_chars=20000),
    ]
    dirs = []
    seen = set()
    for text in texts:
        for match in re.finditer(r"--output-dir\s+([^\s\"']+)", text or ""):
            value = match.group(1).strip()
            if value and value not in seen:
                dirs.append(Path(value))
                seen.add(value)
        for match in re.finditer(r"(/[^\s\"']+/output[^\s\"']*)", text or ""):
            value = match.group(1).rstrip(".,)")
            if value and value not in seen:
                dirs.append(Path(value))
                seen.add(value)
    return dirs


def infer_final_report(task):
    task_id = task.get("task_id")
    watch = task_store.read_task_json(task_id, "watch.json", default={}) or {}
    if isinstance(watch, dict):
        explicit = watch.get("final_report") or watch.get("final_report_path") or watch.get("report_path")
        if file_nonempty(explicit):
            return str(explicit), {"source": "watch.json"}

    for out_dir in candidate_output_dirs(task_id, task):
        if not out_dir.exists() or not out_dir.is_dir():
            continue
        summary = out_dir / "final_falsification_summary.json"
        data = read_json_file(summary)
        if isinstance(data, dict):
            paths = data.get("paths") or {}
            for key in (
                "final_grid_falsification_report_md",
                "final_grid_falsification_report_html",
                "report_md",
                "diagnostic_report_md",
                "final_combination_summary_md",
            ):
                path = paths.get(key)
                if file_nonempty(path):
                    return str(path), {"source": str(summary), "key": key}
        for name in (
            "final_grid_falsification_report_zh.md",
            "diagnostic_report_zh.md",
            "final_combination_summary_zh.md",
            "ablation_summary_zh.md",
        ):
            path = out_dir / name
            if file_nonempty(path):
                return str(path), {"source": "output_dir", "output_dir": str(out_dir), "name": name}
    return None, {}


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


def _has_task_session(task):
    return bool(codex_session.task_session_id(task.get("task_id")))


def build_resume_tick_prompt(task, reason, evidence, final_report=None, final_status=None, failure_reason=None):
    task_id = task.get("task_id")
    task_dir_path = task_store.task_dir(task_id)
    goal_state = task_store.read_task_json(task_id, "goal_state.json", default={}) or {}
    session_json = task_store.read_task_json(task_id, "session.json", default={}) or {}
    final_block = ""
    if final_report:
        final_block = f"""
[FINAL_REPORT_DETECTED]
final_report={final_report}
parsed_final_status={final_status or "unknown"}
failure_reason={failure_reason or ""}
[/FINAL_REPORT_DETECTED]
"""
    return f"""你正在同一個 Codex task session 內繼續監護任務，不是新任務。

父 task：
task_id={task_id}
title={task.get('title')}
status={task.get('status')}
reason={reason}

[SESSION]
{json.dumps(session_json, ensure_ascii=False, indent=2)}
[/SESSION]

[GOAL_STATE]
{json.dumps(goal_state, ensure_ascii=False, indent=2)}
[/GOAL_STATE]

[SCHEDULER_TICK]
{evidence}
[/SCHEDULER_TICK]
{final_block}
[TASK_CONTEXT]
{task_store.task_context(task_id, limit_chars=14000)}
[/TASK_CONTEXT]

請只處理這個 task 的 goal loop tick：
1. 你自己讀取 task folder、watch.json、progress/heartbeat、runner log、stderr/stdout、final report；不要依賴 Python 的機械判斷。
2. 判斷是否完成、仍正常推進、卡住、或明確失敗。
3. 如果只是進度巡檢，必須給完成百分比；資料足夠才估 ETA，否則直接說暫時不能估。
4. 如果 final report 已存在，必須讀取並用自然繁體中文總結結果，不要只說檔案存在。
5. 如果卡住或失敗，先給 root cause 證據，再說下一步補救；不可無腦重跑同一 runner。
6. 不要啟動第二個相同 runner；必須尊重 resource lock、watch.json、progress/heartbeat。

如果你判斷任務狀態需要更新，請寫入：
{task_dir_path}/watch_decision.json

JSON schema:
{{
  "status": "watching | needs_attention | succeeded | failed",
  "reason": "short reason",
  "progress_percent": 0.0,
  "eta": "optional human ETA or null",
  "final_report": "optional absolute path or null",
  "completion_evidence": "short evidence summary",
  "needs_user_input": false
}}

規則：
- 只有你已讀到 final report 或有明確成功證據，才可寫 status=succeeded。
- 只有你有明確失敗證據，才可寫 status=failed。
- 仍在跑但未完成，寫 status=watching。
- 卡住但可補救或需要人手，寫 status=needs_attention。
- 不要直接修改 task.json；由 gateway 根據 watch_decision.json 套用狀態。

回覆要適合手機閱讀：
- 第一句直接講人話結論。
- 最多 3 個短項目。
- 不要貼 JSON、半截 log、大段 code block、或「使用 Skill」欄位。
- 不要擴大範圍；這是同一任務的 tick，不是新任務。
"""


def resume_parent_session(parent, reason, evidence, final_report=None, final_status=None, failure_reason=None):
    parent_id = parent.get("task_id")
    if not _has_task_session(parent):
        return False
    if task_has_codex_process(parent):
        task_store.append_event(parent_id, "session_resume_suppressed_codex_running", {"reason": reason})
        return True
    prompt = build_resume_tick_prompt(parent, reason, evidence, final_report=final_report, final_status=final_status, failure_reason=failure_reason)
    result = codex_session.resume_session(
        parent_id,
        prompt,
        reason=reason,
        timeout=int(os.environ.get("CODEX_WATCH_RESUME_TIMEOUT_SECONDS", "900")),
    )
    answer = memory_adapter.redact_secrets(result.get("answer") or (result.get("stdout") or "")[-6000:] or "")
    if not answer:
        answer = f"Codex resume finished without text output. ok={result.get('ok')} returncode={result.get('returncode')}"
    task_store.write_task_text(parent_id, "latest_watch_report.md", answer)
    task_store.append_thread_message(
        parent_id,
        "supervisor_thread.jsonl",
        "assistant",
        answer,
        {"source": "session_resume", "reason": reason, "ok": result.get("ok"), "returncode": result.get("returncode")},
    )
    task_store.update_goal_state(
        parent_id,
        last_progress=answer[:1500],
        next_action="Continue 10-minute Codex scheduler ticks until final status is confirmed.",
        needs_user_input=False,
    )
    if final_report:
        safe = answer
        task_store.write_task_text(parent_id, "result.md", safe)
        task_store.update_task(
            parent_id,
            status=final_status or "succeeded",
            error=failure_reason if final_status == "failed" else None,
            supervisor_completed_at=task_store.utc_now(),
        )
        codex_session.mark_task_session_finished(parent_id, final_status or "succeeded", reason="session_resume_finalized")
        task_store.update_goal_state(
            parent_id,
            status=final_status or "succeeded",
            completion_evidence=safe[:2000],
            needs_user_input=False,
        )
        task_store.append_thread_message(parent_id, "thread.jsonl", "assistant", safe, {"source": "session_resume_finalized"})
        task_store.append_event(
            parent_id,
            "session_resume_finalized",
            {"final_report": final_report, "final_status": final_status, "failure_reason": failure_reason},
        )
        released = task_store.release_resource_locks_for_task(parent_id, reason="session_resume_finalized")
        if released:
            task_store.append_event(parent_id, "resource_locks_released", {"resource_keys": released})
        if parent.get("unit_name") and unit_active(parent.get("unit_name")):
            if stop_task_unit(parent):
                task_store.append_event(parent_id, "task_unit_stopped_after_session_resume_finalized", {"unit_name": parent.get("unit_name")})
        if final_status == "failed":
            append_main_recent(parent, f"任務已失敗：{parent.get('title')}\ntask_id：{parent_id}\nfinal_report：{final_report}\nfailure_reason：{failure_reason}")
        else:
            append_main_recent(parent, f"任務已完成：{parent.get('title')}\ntask_id：{parent_id}\nfinal_report：{final_report}")
    else:
        task_store.append_event(parent_id, "session_resume_watch_report", {"reason": reason, "ok": result.get("ok")})
        apply_codex_watch_decision(parent, answer, reason)
    if parent.get("chat_id"):
        telegram_sender.send_message(parent.get("chat_id"), answer)
    return True


def apply_codex_watch_decision(parent, answer, reason):
    parent_id = parent.get("task_id")
    decision = task_store.read_task_json(parent_id, "watch_decision.json", default={}) or {}
    if not isinstance(decision, dict) or not decision.get("status"):
        return None
    status = str(decision.get("status") or "").strip().lower()
    if status not in {"watching", "needs_attention", "succeeded", "failed"}:
        task_store.append_event(parent_id, "codex_watch_decision_ignored", {"reason": reason, "decision": decision})
        return None

    safe_answer = memory_adapter.redact_secrets(answer or "")
    task_store.append_event(parent_id, "codex_watch_decision_applied", {"reason": reason, "decision": decision})
    task_store.update_goal_state(
        parent_id,
        status=status,
        last_progress=decision.get("completion_evidence") or safe_answer[:1500],
        next_action=decision.get("reason"),
        needs_user_input=bool(decision.get("needs_user_input")),
        completion_evidence=(decision.get("completion_evidence") or safe_answer[:2000]) if status in {"succeeded", "failed"} else None,
    )

    if status in {"watching", "needs_attention"}:
        task_store.update_task(
            parent_id,
            status=status,
            error=decision.get("reason") if status == "needs_attention" else None,
            last_codex_watch_decision_at=task_store.utc_now(),
        )
        return decision

    task_store.write_task_text(parent_id, "result.md", safe_answer)
    task_store.update_task(
        parent_id,
        status=status,
        error=decision.get("reason") if status == "failed" else None,
        supervisor_completed_at=task_store.utc_now(),
        final_report=decision.get("final_report"),
    )
    codex_session.mark_task_session_finished(parent_id, status, reason="codex_watch_decision")
    task_store.append_thread_message(parent_id, "thread.jsonl", "assistant", safe_answer, {"source": "codex_watch_decision", "status": status})
    released = task_store.release_resource_locks_for_task(parent_id, reason="codex_watch_decision")
    if released:
        task_store.append_event(parent_id, "resource_locks_released", {"resource_keys": released})
    if parent.get("unit_name") and unit_active(parent.get("unit_name")):
        if stop_task_unit(parent):
            task_store.append_event(parent_id, "task_unit_stopped_after_codex_watch_decision", {"unit_name": parent.get("unit_name")})
    if status == "failed":
        append_main_recent(parent, f"任務已失敗：{parent.get('title')}\ntask_id：{parent_id}\nreason：{decision.get('reason')}")
    else:
        append_main_recent(parent, f"任務已完成：{parent.get('title')}\ntask_id：{parent_id}\nfinal_report：{decision.get('final_report')}")
    return decision


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
    if _has_task_session(parent):
        allowed, state = can_spawn_supervision(parent_id, reason)
        if not allowed:
            task_store.append_event(parent_id, "session_resume_supervision_suppressed", {"reason": reason})
            write_supervision_state(parent_id, state)
            return None
        write_supervision_state(parent_id, state)
        task_store.update_task(parent_id, status="needs_attention" if reason != "periodic_codex_watch" else parent.get("status"))
        if resume_parent_session(parent, reason, evidence):
            return parent_id
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
    if _has_task_session(parent):
        allowed, state = can_spawn_periodic_watch(parent_id)
        if not allowed:
            return None
        write_supervision_state(parent_id, state)
        if resume_parent_session(parent, reason, evidence):
            return parent_id
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

    user_text = f"""你是 Codex Telegram Gateway 的定期 watcher。Python supervisor 只負責每 10 分鐘喚醒你；真正的工程判斷由你做。

父 task 未完成，需要你巡檢：
task_id={parent_id}
title={parent.get('title')}
status={parent.get('status')}
reason={reason}

排程資訊：
{evidence}

請只巡檢這個父 task，不要做無關工作。

你必須：
1. 讀取父 task folder、watch.json、progress file、heartbeat、runner log、stderr/stdout、final report path。
2. 判斷現在是正常推進、假 running、stale、已完成、還是明確失敗。
3. 若仍正常推進，必須用 progress 估算完成百分比；若有足夠資料，給 ETA；若不足，直接說無法估 ETA。
4. 若 final report 已存在，讀取並總結結果；不要只說檔案存在。
5. 若卡住或失敗，說清楚 root cause 證據與下一步；只有在 resource lock 不衝突且 resume_command 明確安全時才可補救。
6. 不要啟動第二個相同 runner；先查 resource lock 和既有 PID。

回覆格式必須適合手機閱讀：
- 第一句就是人話結論，不要以「最終目標：」開頭。
- 第一段必須包含：目前是否正常、完成百分比、ETA 或「暫時不能估算」。
- 後面最多 3 個短項目：進度、證據、下一次要看什麼。
- 不要貼大段 JSON、不要長 code block、不要像審計報告。
- 任務完成/失敗的正文必須按第一性原理寫：先回答「是否成功/失敗/仍需觀察」，再說「這個判定的適用範圍」，最後最多 3 條關鍵依據。
- 不要使用「人話結論」「證據」「使用 Skill」這類標籤；不要貼半截 log、內部事件名長串或 final_report tail。

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

        gateway.append_recent(
            "assistant",
            text,
            task.get("chat_id"),
            task.get("user_id"),
            metadata={"source": "supervisor", "task_id": task.get("task_id")},
        )
    except Exception as exc:
        task_store.append_event(task.get("task_id"), "main_recent_append_failed", {"error": f"{type(exc).__name__}: {exc}"})


def extract_report_field(text, field):
    if not text:
        return None
    pattern = re.compile(rf"[-*]?\s*{re.escape(field)}\s*:\s*(.+?)\s*$", re.IGNORECASE)
    for raw_line in text.splitlines()[:120]:
        match = pattern.match(raw_line.strip())
        if match:
            value = match.group(1).strip()
            if value and value.lower() not in {"none", "null", "n/a", "-"}:
                return value
    return None


def build_watched_completion_message(task, report_path, report_tail, final_status, failure_reason):
    title = task.get("title") or "background task"
    duration = extract_report_field(report_tail, "duration_seconds") or extract_report_field(report_tail, "elapsed_seconds")
    scope = "這個判定只代表這個已登記的 background runner 路徑，不代表其他未驗證長任務也一定穩定。"
    if final_status == "failed":
        reason = failure_reason or extract_report_field(report_tail, "failure_reason") or "final report 標記 failed"
        first = f"這次任務失敗；supervisor 已讀到 final report，失敗原因是：{reason}。"
    else:
        if duration:
            first = f"這次任務成功；supervisor 已讀到 final report，runner 實跑 {duration} 秒後完成。"
        else:
            first = "這次任務成功；supervisor 已讀到 final report 並完成收尾。"

    points = [
        f"任務：{title}",
        f"final report：{report_path}",
        f"supervisor 判定：{final_status}",
    ]
    if final_status == "failed" and failure_reason:
        points[2] = f"失敗原因：{failure_reason}"

    return first + "\n" + scope + "\n\n" + "\n".join(f"- {point}" for point in points)


def build_watch_progress_message(task, watch, progress, pid, freshest_age, final_report):
    title = task.get("title") or "background task"
    if isinstance(progress, dict) and progress.get("available"):
        summary = progress.get("summary")
        if not summary:
            completed = progress.get("completed")
            total = progress.get("total")
            percent = progress.get("percent")
            if completed is not None and total is not None:
                summary = f"{completed}/{total}"
                if percent is not None:
                    summary += f" ({float(percent):.1f}%)"
        stage = progress.get("stage") or "unknown"
        updated_at = progress.get("updated_at") or "unknown"
        first = f"巡檢報告：任務正在正常推進，進度 {summary or '可讀進度不足'}。"
        points = [
            f"階段：{stage}｜progress 更新：{updated_at}",
            f"runner：PID {pid} 存活｜最新 heartbeat/log 約 {format_duration(freshest_age) or '0m'} 前更新",
            f"final report：{'已產生' if file_nonempty(final_report) else '尚未產生'}",
        ]
    else:
        first = "巡檢報告：runner 存活且 heartbeat/log 新鮮，但 progress 檔目前沒有可讀百分比。"
        points = [
            f"任務：{title}",
            f"runner：PID {pid} 存活｜最新 heartbeat/log 約 {format_duration(freshest_age) or '0m'} 前更新",
            f"final report：{'已產生' if file_nonempty(final_report) else '尚未產生'}",
        ]
    return first + "\n\n" + "\n".join(f"- {point}" for point in points)


def publish_watch_progress_report(task, watch, progress, pid, freshest_age, final_report):
    task_id = task.get("task_id")
    text = build_watch_progress_message(task, watch, progress, pid, freshest_age, final_report)
    safe = memory_adapter.redact_secrets(text)
    task_store.write_task_text(task_id, "latest_watch_report.md", safe)
    task_store.append_thread_message(task_id, "supervisor_thread.jsonl", "supervisor", safe, {"source": "watch_progress_report"})
    if task.get("chat_id"):
        telegram_sender.send_message(task.get("chat_id"), safe)
    task_store.append_event(task_id, "watch_progress_report_sent", {"progress": progress})


def finalize_watched_task(task, watch):
    task_id = task["task_id"]
    report_path = watch.get("final_report") or watch.get("final_report_path") or watch.get("report_path")
    report_tail = read_text_tail(report_path, limit=5000)
    report_status, failure_reason = parse_final_report_status(report_tail)
    final_status = "failed" if report_status == "failed" else "succeeded"
    if _has_task_session(task):
        evidence = json.dumps(
            {
                "final_report": report_path,
                "report_status": report_status,
                "final_status": final_status,
                "failure_reason": failure_reason,
                "report_tail": report_tail[-3000:],
            },
            ensure_ascii=False,
            indent=2,
        )
        if resume_parent_session(
            task,
            "final_report_detected",
            evidence,
            final_report=report_path,
            final_status=final_status,
            failure_reason=failure_reason,
        ):
            return
    text = build_watched_completion_message(task, report_path, report_tail, final_status, failure_reason)
    safe = memory_adapter.redact_secrets(text)
    task_store.write_task_text(task_id, "result.md", safe)
    task_store.update_task(task_id, status=final_status, error=failure_reason if final_status == "failed" else None, supervisor_completed_at=task_store.utc_now())
    if _has_task_session(task):
        codex_session.mark_task_session_finished(task_id, final_status, reason="supervisor_finalized")
    task_store.append_thread_message(task_id, "thread.jsonl", "assistant", safe, {"source": "supervisor_finalized"})
    task_store.append_event(task_id, "supervisor_finalized", {"final_report": report_path, "final_status": final_status, "failure_reason": failure_reason})
    released = task_store.release_resource_locks_for_task(task_id, reason="supervisor_finalized")
    if released:
        task_store.append_event(task_id, "resource_locks_released", {"resource_keys": released})
    if task.get("unit_name") and unit_active(task.get("unit_name")):
        if stop_task_unit(task):
            task_store.append_event(task_id, "task_unit_stopped_after_supervisor_finalized", {"unit_name": task.get("unit_name")})
    if final_status == "failed":
        append_main_recent(task, f"任務已失敗：{task.get('title')}\ntask_id：{task_id}\nfinal_report：{report_path}\nfailure_reason：{failure_reason}")
    else:
        append_main_recent(task, f"任務已完成：{task.get('title')}\ntask_id：{task_id}\nfinal_report：{report_path}")
    if task.get("chat_id"):
        telegram_sender.send_message(task.get("chat_id"), safe)


def finalize_inferred_task(task, report_path, detector=None):
    watch = {
        "final_report": report_path,
        "detector": detector or {},
    }
    finalize_watched_task(task, watch)


def inspect_watch(task):
    task_id = task["task_id"]
    watch = task_store.read_task_json(task_id, "watch.json", default={}) or {}
    if not isinstance(watch, dict) or not watch.get("enabled", False):
        return

    state = supervision_state(task_id)
    state["last_watch_check_at"] = task_store.utc_now()
    state["mode"] = "codex_scheduler_only"
    write_supervision_state(task_id, state)

    evidence = json.dumps(
        {
            "scheduler": "codex_only",
            "interval_seconds": CODEX_WATCHER_INTERVAL_SECONDS,
            "instruction": "Python did not inspect PID, heartbeat, progress, final_report, or resource lock. Codex must read task files and decide.",
            "task_id": task_id,
            "title": task.get("title"),
            "status": task.get("status"),
            "watch": watch,
        },
        ensure_ascii=False,
        indent=2,
    )
    task_store.append_event(task_id, "codex_watch_scheduler_tick", {"interval_seconds": CODEX_WATCHER_INTERVAL_SECONDS})
    spawn_periodic_watch_task(task, "periodic_codex_watch", evidence)


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
        inferred_report, detector = infer_final_report(task)
        if inferred_report:
            task_store.append_event(task_id, "inferred_final_report_detected", {"final_report": inferred_report, "detector": detector})
            finalize_inferred_task(task, inferred_report, detector)
            return
        active = unit_active(task.get("unit_name"))
        if active is True:
            codex_running = task_has_codex_process(task)
            state = supervision_state(task_id)
            state["last_active_unit_check_at"] = task_store.utc_now()
            state["unit_active"] = True
            state["codex_running"] = codex_running
            if codex_running:
                state.pop("no_codex_since_ts", None)
                state.pop("no_codex_since_at", None)
                state.pop("no_codex_elapsed_seconds", None)
                state.pop("watch_json_present", None)
                write_supervision_state(task_id, state)
                return

            watch = task_store.read_task_json(task_id, "watch.json", default={}) or {}
            if isinstance(watch, dict) and watch.get("enabled", False):
                state.pop("no_codex_since_ts", None)
                state.pop("no_codex_since_at", None)
                state.pop("no_codex_elapsed_seconds", None)
                state["watch_json_present"] = True
                write_supervision_state(task_id, state)
                inspect_watch(task)
                return

            now = time.time()
            no_codex_since = float(state.get("no_codex_since_ts") or 0)
            if no_codex_since <= 0:
                state["no_codex_since_ts"] = now
                state["no_codex_since_at"] = task_store.utc_now()
                state["watch_json_present"] = False
                write_supervision_state(task_id, state)
                task_store.append_event(
                    task_id,
                    "codex_absent_without_watch_timer_started",
                    {"interval_seconds": CODEX_WATCHER_INTERVAL_SECONDS},
                )
                return
            no_codex_elapsed = max(0.0, now - no_codex_since)
            state["no_codex_elapsed_seconds"] = round(no_codex_elapsed, 2)
            state["watch_json_present"] = False
            write_supervision_state(task_id, state)
            if no_codex_elapsed < CODEX_WATCHER_INTERVAL_SECONDS:
                return

            if task_has_codex_process(task):
                state = supervision_state(task_id)
                state["codex_running"] = True
                state.pop("no_codex_since_ts", None)
                state.pop("no_codex_since_at", None)
                state.pop("no_codex_elapsed_seconds", None)
                write_supervision_state(task_id, state)
                task_store.append_event(task_id, "codex_watch_suppressed_codex_reappeared", {})
                return

            evidence = json.dumps(
                {
                    "status": status,
                    "unit_name": task.get("unit_name"),
                    "unit_active": active,
                    "codex_running": codex_running,
                    "updated_at": task.get("updated_at"),
                    "age_seconds": round(age_seconds(task.get("updated_at")), 2),
                    "no_codex_since_at": state.get("no_codex_since_at"),
                    "no_codex_elapsed_seconds": round(no_codex_elapsed, 2),
                    "watch_json_present": False,
                    "stdout_tail": task_store.read_task_text(task_id, "stdout.log", limit_chars=2000),
                    "stderr_tail": task_store.read_task_text(task_id, "stderr.log", limit_chars=2000),
                },
                ensure_ascii=False,
                indent=2,
            )
            spawn_supervision_task(task, "codex_absent_without_watch", evidence)
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
