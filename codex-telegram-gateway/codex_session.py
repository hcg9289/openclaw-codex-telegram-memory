#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import memory_adapter
import task_store


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
CHAT_SESSIONS_DIR = STATE_DIR / "sessions"


def _now():
    return task_store.utc_now()


def _safe_chat_id(chat_id):
    return task_store.safe_name(f"chat_{chat_id}")


def _chat_session_path(chat_id):
    return CHAT_SESSIONS_DIR / (_safe_chat_id(chat_id) + ".json")


def _read_json(path, default=None):
    return task_store.read_json(path, default=default)


def _write_json(path, data):
    task_store.write_json(path, data)


def _append_jsonl(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False) + "\n")


def _parse_json_events(stdout_text):
    events = []
    thread_id = None
    fallback_answer = ""
    for raw_line in (stdout_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        if event.get("thread_id"):
            thread_id = event.get("thread_id")
        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = event.get("thread_id")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            fallback_answer = item.get("text") or fallback_answer
    return events, thread_id, fallback_answer


def _base_cmd():
    return [
        os.environ.get("CODEX_BIN", "/usr/bin/codex"),
        "exec",
    ]


def _codex_flags():
    return [
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
    ]


def _run_codex(command_kind, prompt, session_id=None, timeout=None):
    timeout = int(timeout or os.environ.get("CODEX_TIMEOUT_SECONDS", "18000"))
    out_path = None
    started = time.time()
    try:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
            out_path = out_file.name
        cmd = _base_cmd()
        if command_kind == "resume":
            if not session_id:
                raise ValueError("session_id is required for codex exec resume")
            cmd.extend(["resume"])
        cmd.extend(_codex_flags())
        cmd.extend(["--output-last-message", out_path])
        if command_kind == "resume":
            cmd.extend([session_id, prompt])
        else:
            cmd.append(prompt)

        completed = subprocess.run(
            cmd,
            cwd=os.environ.get("CODEX_WORKDIR", "/root"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout_text = memory_adapter.redact_secrets(completed.stdout or "")
        events, parsed_thread_id, fallback_answer = _parse_json_events(stdout_text)
        try:
            answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            answer = ""
        answer = memory_adapter.redact_secrets(answer or fallback_answer)
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": stdout_text,
            "answer": answer,
            "session_id": parsed_thread_id or session_id,
            "events": events,
            "duration_seconds": round(time.time() - started, 2),
            "command_kind": command_kind,
        }
    except subprocess.TimeoutExpired as exc:
        stdout_text = memory_adapter.redact_secrets((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        return {
            "ok": False,
            "returncode": None,
            "stdout": stdout_text,
            "answer": f"Codex timed out after {timeout} seconds.",
            "session_id": session_id,
            "events": [],
            "duration_seconds": round(time.time() - started, 2),
            "command_kind": command_kind,
            "timeout": True,
        }
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except FileNotFoundError:
                pass


def task_session(task_id):
    return task_store.read_task_json(task_id, "session.json", default={}) or {}


def task_session_id(task_id):
    session = task_session(task_id)
    return session.get("session_id")


def mark_task_session_finished(task_id, status, reason="task_finished"):
    session = task_session(task_id)
    if not session:
        return None
    session["task_status"] = status
    session["finished_at"] = session.get("finished_at") or _now()
    session["updated_at"] = _now()
    session["finish_reason"] = reason
    task_store.write_task_json(task_id, "session.json", session)
    task_store.append_event(
        task_id,
        "codex_session_finished",
        {
            "session_id": session.get("session_id"),
            "status": status,
            "reason": reason,
        },
    )
    return session


def _write_task_session(task_id, session_id, scope, reason, result, chat_id=None):
    existing = task_session(task_id)
    data = {
        **existing,
        "session_id": session_id,
        "scope": scope,
        "chat_id": chat_id,
        "created_at": existing.get("created_at") or _now(),
        "updated_at": _now(),
        "task_status": existing.get("task_status") or "active",
        "last_reason": reason,
        "last_command": result.get("command_kind"),
        "last_returncode": result.get("returncode"),
        "last_duration_seconds": result.get("duration_seconds"),
    }
    task_store.write_task_json(task_id, "session.json", data)
    task_store.update_task(task_id, session_id=session_id, session_scope=scope)
    return data


def _update_chat_session(chat_id, session_id, reason, result):
    if not chat_id or not session_id:
        return None
    CHAT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _chat_session_path(chat_id)
    existing = _read_json(path, default={}) or {}
    data = {
        **existing,
        "session_id": session_id,
        "scope": "chat",
        "chat_id": chat_id,
        "created_at": existing.get("created_at") or _now(),
        "updated_at": _now(),
        "last_reason": reason,
        "last_command": result.get("command_kind"),
        "last_returncode": result.get("returncode"),
        "last_duration_seconds": result.get("duration_seconds"),
    }
    _write_json(path, data)
    return data


def _chat_session_id(chat_id):
    if not chat_id:
        return None
    return (_read_json(_chat_session_path(chat_id), default={}) or {}).get("session_id")


def _record_resume_event(task_id, reason, result, scope):
    item = {
        "ts": _now(),
        "reason": reason,
        "scope": scope,
        "command_kind": result.get("command_kind"),
        "session_id": result.get("session_id"),
        "ok": result.get("ok"),
        "returncode": result.get("returncode"),
        "duration_seconds": result.get("duration_seconds"),
        "answer_preview": memory_adapter.redact_secrets((result.get("answer") or "")[:1000]),
    }
    _append_jsonl(task_store.task_dir(task_id) / "resume_events.jsonl", item)


def run_for_task(task_id, prompt, reason="task_worker", use_chat_session=False, chat_id=None, timeout=None):
    task_store.write_task_text(task_id, "last_codex_prompt.md", prompt)
    scope = "chat" if use_chat_session else "task"
    existing_session_id = _chat_session_id(chat_id) if use_chat_session else task_session_id(task_id)
    if existing_session_id:
        result = _run_codex("resume", prompt, session_id=existing_session_id, timeout=timeout)
    else:
        result = _run_codex("exec", prompt, timeout=timeout)

    session_id = result.get("session_id") or existing_session_id
    if session_id:
        _write_task_session(task_id, session_id, scope, reason, result, chat_id=chat_id)
        if use_chat_session:
            _update_chat_session(chat_id, session_id, reason, result)
    task_store.write_task_text(task_id, "stdout.log", result.get("stdout") or "")
    _record_resume_event(task_id, reason, result, scope)
    task_store.append_event(
        task_id,
        "codex_session_run",
        {
            "scope": scope,
            "reason": reason,
            "command_kind": result.get("command_kind"),
            "session_id": session_id,
            "ok": result.get("ok"),
            "returncode": result.get("returncode"),
            "duration_seconds": result.get("duration_seconds"),
        },
    )
    return result


def resume_session(task_id, prompt, reason="watch_tick", timeout=None):
    session_id = task_session_id(task_id)
    if not session_id:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "answer": "No session_id is recorded for this task.",
            "session_id": None,
            "events": [],
            "duration_seconds": 0,
            "command_kind": "resume",
            "missing_session": True,
        }
    task_store.write_task_text(task_id, f"resume_prompt_{task_store.safe_name(reason)}.md", prompt)
    result = _run_codex("resume", prompt, session_id=session_id, timeout=timeout)
    if result.get("session_id"):
        _write_task_session(task_id, result.get("session_id"), "task", reason, result, chat_id=(task_store.get_task(task_id) or {}).get("chat_id"))
    task_store.append_task_text(task_id, "stdout.log", "\n\n[resume_stdout]\n" + (result.get("stdout") or "") + "\n[/resume_stdout]\n")
    _record_resume_event(task_id, reason, result, "task")
    task_store.append_event(
        task_id,
        "codex_session_resume",
        {
            "reason": reason,
            "session_id": result.get("session_id") or session_id,
            "ok": result.get("ok"),
            "returncode": result.get("returncode"),
            "duration_seconds": result.get("duration_seconds"),
        },
    )
    return result
