#!/usr/bin/env python3
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import job_store
import memory_adapter


ROUTER_TIMEOUT = int(os.environ.get("ORCHESTRATOR_ROUTER_TIMEOUT_SECONDS", "90"))
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/bin/codex")
CODEX_WORKDIR = os.environ.get("CODEX_WORKDIR", "/root")
CONTROL_INTENTS = {
    "job_status",
    "job_plan",
    "job_log",
    "job_result",
    "cancel_job",
    "rerun_phase",
}
PHASE_ALIASES = {
    "planner": "_planner",
    "主代理": "_planner",
    "計劃": "_planner",
    "explorer": "explorer",
    "探索": "explorer",
    "auditor": "auditor",
    "審查": "auditor",
    "runner": "runner",
    "回測": "runner",
    "verifier": "verifier",
    "驗證": "verifier",
    "reporter": "_final_reporter",
    "報告": "_final_reporter",
}


def jobs_summary(limit=8):
    jobs = [job for job in job_store.list_jobs(limit=limit * 3) if is_user_job(job)][:limit]
    rows = []
    for job in jobs:
        rows.append(
            {
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "current_phase": job.get("current_phase"),
                "user_text": (job.get("user_text") or "")[:220],
            }
        )
    return rows


def latest_job(prefer_active=True):
    jobs = [job for job in job_store.list_jobs(limit=50) if is_user_job(job)]
    if prefer_active:
        for job in jobs:
            if job.get("status") in {"queued", "planning", "running", "pending"}:
                return job
    return jobs[0] if jobs else None


def is_user_job(job):
    if not job:
        return False
    if str(job.get("chat_id")) in {"0", "None"}:
        return False
    text = (job.get("user_text") or "").lower()
    title = (job.get("title") or "").lower()
    if "smoke" in text or "smoke" in title:
        return False
    return True


def extract_json_object(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("router output did not contain JSON object")


def router_prompt(user_text, recent_context):
    return f"""你是 Telegram Codex Router。你的唯一任務是判斷本次自然語言是否在控制既有 job，或是否應建立新 job。

只輸出 JSON object，不要 markdown，不要解釋。

可用 intents:
- new_job: 使用者提出新任務或普通問題
- job_status: 查最近/指定 job 狀態或進度
- job_plan: 查 plan
- job_log: 查某 phase log
- job_result: 查 final report 或 phase result
- cancel_job: 取消最近/指定 job
- rerun_phase: 重跑某 phase
- unknown: 無法判斷，應 fallback new_job

target:
- "latest_active" 用於「剛才/上一個/最近/那個任務」
- 或具體 job_id
- 或 null

phase:
- "_planner", "explorer", "auditor", "runner", "verifier", "_final_reporter" 或 null

[RECENT_JOBS]
{json.dumps(jobs_summary(), ensure_ascii=False, indent=2)}
[/RECENT_JOBS]

[RECENT_TELEGRAM_CONTEXT]
{recent_context}
[/RECENT_TELEGRAM_CONTEXT]

[USER_TEXT]
{user_text}
[/USER_TEXT]

輸出 schema:
{{
  "intent": "new_job|job_status|job_plan|job_log|job_result|cancel_job|rerun_phase|unknown",
  "target": "latest_active|null|job_id",
  "phase": null,
  "confidence": "high|medium|low",
  "reason": "short reason"
}}
"""


def normalize_phase(value):
    if not value:
        return None
    raw = str(value).strip()
    return PHASE_ALIASES.get(raw, raw)


def fallback_route(user_text):
    lowered = user_text.lower()
    phase = None
    for key, value in PHASE_ALIASES.items():
        if key.lower() in lowered:
            phase = value
            break
    recent_words = ["剛才", "上一個", "最近", "那個", "這個", "current", "latest", "last"]
    has_recent = any(word in lowered for word in recent_words)
    if any(word in lowered for word in ["取消", "cancel"]):
        return {"intent": "cancel_job", "target": "latest_active" if has_recent else None, "phase": None, "confidence": "medium", "reason": "rule fallback cancel"}
    if any(word in lowered for word in ["重跑", "rerun", "再跑", "重做", "中斷的部份", "中斷的部分"]):
        return {"intent": "rerun_phase", "target": "latest_active" if has_recent else None, "phase": phase, "confidence": "medium", "reason": "rule fallback rerun"}
    if any(word in lowered for word in ["做到哪", "進度", "狀態", "完成未", "完成了嗎", "status", "progress"]):
        return {"intent": "job_status", "target": "latest_active" if has_recent else None, "phase": None, "confidence": "medium", "reason": "rule fallback status"}
    if any(word in lowered for word in ["log", "日誌", "stdout"]):
        return {"intent": "job_log", "target": "latest_active" if has_recent else None, "phase": phase, "confidence": "medium", "reason": "rule fallback log"}
    if any(word in lowered for word in ["結果", "報告", "總結", "result", "report"]):
        return {"intent": "job_result", "target": "latest_active" if has_recent else None, "phase": phase, "confidence": "medium", "reason": "rule fallback result"}
    return {"intent": "new_job", "target": None, "phase": None, "confidence": "low", "reason": "fallback new job"}


def route(user_text, recent_context):
    safe_text = memory_adapter.redact_secrets(user_text)
    if not job_store.list_jobs(limit=1):
        return {"intent": "new_job", "target": None, "phase": None, "confidence": "high", "reason": "no existing jobs"}
    prompt = router_prompt(safe_text, recent_context)
    try:
        routed = extract_json_object(run_router_codex(prompt))
    except Exception:
        routed = fallback_route(safe_text)
    intent = routed.get("intent") or "new_job"
    if intent == "unknown":
        intent = "new_job"
    if intent not in CONTROL_INTENTS and intent != "new_job":
        intent = "new_job"
    routed["intent"] = intent
    routed["phase"] = normalize_phase(routed.get("phase"))
    return routed


def resolve_job(route_result):
    target = route_result.get("target")
    if target == "latest_active" or not target:
        return latest_job(prefer_active=True)
    return job_store.get_job(str(target))


def run_router_codex(prompt):
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = out_file.name
    try:
        completed = subprocess.run(
            [
                CODEX_BIN,
                "exec",
                "--skip-git-repo-check",
                "--output-last-message",
                out_path,
                prompt,
            ],
            cwd=CODEX_WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=ROUTER_TIMEOUT,
            check=False,
        )
        answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        if answer:
            return memory_adapter.redact_secrets(answer)
        if completed.returncode != 0:
            raise RuntimeError((completed.stdout or "")[-1000:])
        return memory_adapter.redact_secrets((completed.stdout or "").strip())
    finally:
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass
