#!/usr/bin/env python3
import json
import os
import time
import uuid
from pathlib import Path

import memory_adapter


STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
JOBS_DIR = STATE_DIR / "jobs"
PHASE_LABELS = {
    "_planner": "主代理規劃",
    "explorer": "資料探索",
    "auditor": "風險審查",
    "runner": "執行回測",
    "verifier": "結果驗證",
    "reporter": "報告整理",
    "_final_reporter": "最終報告",
}
STATUS_LABELS = {
    "pending": "等待中",
    "queued": "排隊中",
    "planning": "主代理規劃中",
    "running": "執行中",
    "succeeded": "已完成",
    "failed": "已中斷",
    "cancelled": "已取消",
}


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_job_id():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def job_dir(job_id):
    return JOBS_DIR / job_id


def phase_dir(job_id, phase_id):
    return job_dir(job_id) / "phases" / safe_name(phase_id)


def safe_name(value):
    value = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe[:80] or "phase"


def make_title(user_text, max_len=36):
    text = " ".join(str(user_text or "").strip().split())
    if not text:
        return "未命名任務"
    if "回測" in text and ("偷看未來" in text or "訓練" in text or "交易品種" in text or "真實性" in text):
        return "20品種回測真實性審查"
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


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


def create_job(user_text, chat_id, user_id):
    job_id = new_job_id()
    root = job_dir(job_id)
    (root / "phases").mkdir(parents=True, exist_ok=True)
    job = {
        "job_id": job_id,
        "status": "pending",
        "chat_id": chat_id,
        "user_id": user_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "title": make_title(user_text),
        "user_text": memory_adapter.redact_secrets(user_text),
        "current_phase": None,
        "error": None,
    }
    write_json(root / "job.json", job)
    return job


def get_job(job_id):
    return read_json(job_dir(job_id) / "job.json")


def update_job(job_id, **fields):
    job = get_job(job_id) or {"job_id": job_id, "created_at": utc_now()}
    job.update(fields)
    job["updated_at"] = utc_now()
    write_json(job_dir(job_id) / "job.json", job)
    return job


def mark_cancelled(job_id, reason="cancelled by user"):
    return update_job(job_id, status="cancelled", error=reason)


def is_cancelled(job_id):
    job = get_job(job_id)
    return bool(job and job.get("status") == "cancelled")


def list_jobs(limit=10):
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for path in JOBS_DIR.iterdir():
        if not path.is_dir():
            continue
        job = read_json(path / "job.json")
        if job:
            jobs.append(job)
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jobs[:limit]


def mark_stale_active_jobs(reason="stale after gateway restart"):
    if not JOBS_DIR.exists():
        return []
    changed = []
    active = {"pending", "queued", "planning", "running"}
    for job in list_jobs(limit=1000):
        if job.get("status") not in active:
            continue
        job_id = job.get("job_id")
        update_job(job_id, status="failed", current_phase=None, error=reason)
        root = job_dir(job_id) / "phases"
        if root.exists():
            for phase_json in root.glob("*/phase.json"):
                phase = read_json(phase_json, {})
                if phase.get("status") in active:
                    update_phase(job_id, phase.get("id") or phase_json.parent.name, status="failed", error=reason)
        changed.append(job_id)
    return changed


def write_memory_context(job_id, context):
    write_json(job_dir(job_id) / "memory_context.json", context)


def read_memory_context(job_id):
    return read_json(job_dir(job_id) / "memory_context.json", {})


def write_plan(job_id, plan):
    write_json(job_dir(job_id) / "plan.json", plan)


def read_plan(job_id):
    return read_json(job_dir(job_id) / "plan.json", {})


def init_phase(job_id, phase):
    phase_id = safe_name(phase.get("id") or phase.get("role") or "phase")
    root = phase_dir(job_id, phase_id)
    root.mkdir(parents=True, exist_ok=True)
    state = {
        "id": phase_id,
        "role": phase.get("role", "worker"),
        "status": "pending",
        "objective": phase.get("objective", ""),
        "depends_on": phase.get("depends_on", []),
        "timeout_seconds": int(phase.get("timeout_seconds", 900)),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "error": None,
    }
    write_json(root / "phase.json", state)
    return state


def get_phase(job_id, phase_id):
    return read_json(phase_dir(job_id, phase_id) / "phase.json")


def update_phase(job_id, phase_id, **fields):
    phase = get_phase(job_id, phase_id) or {"id": safe_name(phase_id), "created_at": utc_now()}
    phase.update(fields)
    phase["updated_at"] = utc_now()
    write_json(phase_dir(job_id, phase_id) / "phase.json", phase)
    return phase


def write_phase_text(job_id, phase_id, filename, text):
    atomic_write(phase_dir(job_id, phase_id) / filename, text)


def read_phase_text(job_id, phase_id, filename, limit_chars=12000):
    path = phase_dir(job_id, phase_id) / filename
    if not path.exists():
        return ""
    return memory_adapter.redact_secrets(path.read_text(encoding="utf-8", errors="replace")[-limit_chars:])


def write_final_report(job_id, text):
    atomic_write(job_dir(job_id) / "final_report.md", text)


def read_final_report(job_id, limit_chars=16000):
    path = job_dir(job_id) / "final_report.md"
    if not path.exists():
        return ""
    return memory_adapter.redact_secrets(path.read_text(encoding="utf-8", errors="replace")[-limit_chars:])


def fallback_report(job_id, limit_chars=16000):
    job = get_job(job_id)
    if not job:
        return ""
    plan = read_plan(job_id) or {}
    lines = [
        f"任務：{job.get('title') or make_title(job.get('user_text'))}",
        f"job_id：{job_id}",
        "",
        "這個任務沒有生成 final_report.md；以下是已完成 phase 的結果摘要。",
    ]
    for phase in plan.get("phases", []):
        phase_id = phase.get("id")
        state = get_phase(job_id, phase_id) or {}
        result = read_phase_text(job_id, phase_id, "result.md", limit_chars=max(2000, limit_chars // 4))
        lines.append("")
        lines.append(f"## {phase_label(phase_id)} ({phase_id})：{status_label(state.get('status'))}")
        if state.get("error"):
            lines.append(f"error: {state.get('error')}")
        lines.append(result or "(empty)")
    return memory_adapter.redact_secrets("\n".join(lines)[-limit_chars:])


def summarize_job(job):
    if not job:
        return "job not found"
    return (
        f"job_id={job.get('job_id')}\n"
        f"status={job.get('status')}\n"
        f"created_at={job.get('created_at')}\n"
        f"updated_at={job.get('updated_at')}\n"
        f"current_phase={job.get('current_phase')}\n"
        f"error={job.get('error')}\n"
        f"user_text={job.get('user_text')}"
    )


def phase_label(phase_id):
    return PHASE_LABELS.get(phase_id, phase_id)


def status_label(status):
    return STATUS_LABELS.get(status, status or "未知")


def natural_job_status(job):
    if not job:
        return "找不到這個任務。"
    job_id = job.get("job_id")
    status = job.get("status")
    current_phase = job.get("current_phase")
    lines = [
        f"任務：{job.get('title') or make_title(job.get('user_text'))}",
        f"job_id：{job_id}",
        f"狀態：{status_label(status)}",
        f"建立時間：{job.get('created_at')}",
        f"更新時間：{job.get('updated_at')}",
    ]
    if current_phase:
        lines.append(f"目前階段：{phase_label(current_phase)} ({current_phase})")
    if job.get("error"):
        lines.append(f"原因：{job.get('error')}")

    phases_root = job_dir(job_id) / "phases"
    if phases_root.exists():
        lines.append("")
        lines.append("階段進度：")
        for phase_json in sorted(phases_root.glob("*/phase.json")):
            phase = read_json(phase_json, {})
            phase_id = phase.get("id") or phase_json.parent.name
            lines.append(f"- {phase_label(phase_id)} ({phase_id})：{status_label(phase.get('status'))}")

    if status == "failed" and job.get("error") == "stale after gateway restart":
        lines.append("")
        lines.append("這個任務因 Gateway 重啟而中斷；已完成的 phase 仍可用 `/job-result <job_id> explorer` 查看。建議重新發一次任務，或指定重跑某個 phase。")
    elif status in {"queued", "planning", "running", "pending"}:
        lines.append("")
        lines.append("任務仍在處理中。你可以問「剛才任務做到哪？」或用 `/job-log <job_id> <phase>` 查看 log。")
    elif status == "succeeded":
        lines.append("")
        lines.append("任務已完成。可用自然語言問「看剛才報告」，或用 `/job-result <job_id>` 查看完整報告。")
    return memory_adapter.redact_secrets("\n".join(lines))
