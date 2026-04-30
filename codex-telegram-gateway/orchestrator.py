#!/usr/bin/env python3
import concurrent.futures
import json
import os
import re
import subprocess
import threading
import traceback

import agent_prompts
import facts_store
import job_store
import memory_adapter
import worker_runner


PLANNER_TIMEOUT = int(os.environ.get("ORCHESTRATOR_PLANNER_TIMEOUT_SECONDS", "900"))
DEFAULT_WORKER_TIMEOUT = int(os.environ.get("ORCHESTRATOR_WORKER_TIMEOUT_SECONDS", "1200"))
REPORTER_TIMEOUT = int(os.environ.get("ORCHESTRATOR_REPORTER_TIMEOUT_SECONDS", "900"))
MAX_PARALLEL = int(os.environ.get("ORCHESTRATOR_MAX_PARALLEL", "2"))
APP_DIR = os.environ.get("GATEWAY_APP_DIR", "/opt/codex-telegram-gateway")
PYTHON_BIN = os.environ.get("GATEWAY_PYTHON_BIN", "/usr/bin/python3")
SYSTEMD_RUN_BIN = os.environ.get("SYSTEMD_RUN_BIN", "/usr/bin/systemd-run")
JOB_UNIT_PREFIX = os.environ.get("ORCHESTRATOR_JOB_UNIT_PREFIX", "codex-telegram-job")
DURABLE_JOBS = os.environ.get("ORCHESTRATOR_DURABLE_JOBS", "1").lower() not in {"0", "false", "no"}


def notify(send_callback, chat_id, text):
    try:
        safe_text = memory_adapter.redact_secrets(text)
        if send_callback:
            send_callback(chat_id, safe_text)
        else:
            import telegram_sender

            telegram_sender.send_message(chat_id, safe_text)
    except Exception as exc:
        print(f"telegram notify failed: {exc}", flush=True)


def job_unit_name(job_id, suffix="run"):
    return f"{JOB_UNIT_PREFIX}-{job_store.safe_name(job_id)}-{job_store.safe_name(suffix)}"


def is_unit_active(unit_name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"{unit_name}.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


def is_job_unit_active(job_id):
    prefix = f"{JOB_UNIT_PREFIX}-{job_store.safe_name(job_id)}"
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active", "--no-legend", f"{prefix}*.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _start_systemd_worker(job_id, mode="run", phase_id=None):
    unit = job_unit_name(job_id, mode if not phase_id else f"{mode}-{phase_id}")
    cmd = [
        SYSTEMD_RUN_BIN,
        "--unit",
        unit,
        "--collect",
        "--property",
        "KillMode=control-group",
        "--property",
        "Restart=no",
        "--property",
        "WorkingDirectory=" + APP_DIR,
        "--setenv",
        "PYTHONUNBUFFERED=1",
        PYTHON_BIN,
        os.path.join(APP_DIR, "job_worker.py"),
        mode,
        job_id,
    ]
    if phase_id:
        cmd.append(phase_id)
    result = subprocess.run(
        cmd,
        cwd=APP_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"systemd-run failed: {result.stdout.strip()}")
    return unit, result.stdout.strip()


def job_title(job):
    return (job or {}).get("title") or job_store.make_title((job or {}).get("user_text"))


def build_memory_context(user_text, recent_context, chat_id):
    recall_query = f"Current user task:\n{user_text}\n\nRecent Telegram context:\n{recent_context}"
    recall_result = memory_adapter.recall_memory(recall_query, session_id=str(chat_id), top_k=5, timeout=45)
    recall_text = recall_result.get("text") if recall_result.get("ok") else f"(cognee recall failed: {recall_result.get('error')})"
    return {
        "facts": facts_store.format_facts_for_prompt(),
        "recent_context": recent_context,
        "cognee_recall_ok": recall_result.get("ok"),
        "cognee_recall_error": recall_result.get("error"),
        "cognee_recall_text": recall_text,
        "cognee_recall_items": recall_result.get("items", [])[:5],
        "memory_priority": [
            "facts.json current truth",
            "recent Telegram context",
            "cognee historical recall",
            "job-local context",
            "user task",
        ],
    }


def extract_json_object(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty planner output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("planner output did not contain JSON object")


def fallback_plan(user_text, reason):
    return {
        "job_type": "quick_answer",
        "summary": f"fallback plan because planner failed: {reason}",
        "phases": [
            {
                "id": "reporter",
                "role": "reporter",
                "objective": "Answer the user directly using the available facts, recent context, and cognee recall.",
                "depends_on": [],
                "timeout_seconds": DEFAULT_WORKER_TIMEOUT,
                "prompt": f"Planner fallback. Original task: {user_text}",
            }
        ],
    }


def normalize_plan(plan):
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(phases, list) or not phases:
        raise ValueError("plan must contain non-empty phases list")
    normalized = []
    seen = set()
    for index, phase in enumerate(phases, start=1):
        if not isinstance(phase, dict):
            continue
        phase_id = job_store.safe_name(phase.get("id") or f"phase_{index}")
        if phase_id in seen:
            phase_id = f"{phase_id}_{index}"
        seen.add(phase_id)
        depends_on = phase.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        normalized.append(
            {
                "id": phase_id,
                "role": str(phase.get("role") or "worker"),
                "objective": str(phase.get("objective") or phase.get("prompt") or ""),
                "depends_on": [job_store.safe_name(item) for item in depends_on],
                "timeout_seconds": int(phase.get("timeout_seconds") or DEFAULT_WORKER_TIMEOUT),
                "prompt": str(phase.get("prompt") or ""),
            }
        )
    if not normalized:
        raise ValueError("plan had no usable phases")
    plan["phases"] = normalized
    return plan


def run_planner(job_id, user_text, memory_context):
    phase = {"id": "_planner", "role": "planner", "objective": "Create job plan", "timeout_seconds": PLANNER_TIMEOUT}
    job_store.init_phase(job_id, phase)
    job_store.update_job(job_id, status="planning", current_phase="_planner")
    prompt = agent_prompts.planner_prompt(user_text, memory_context)
    job_store.write_phase_text(job_id, "_planner", "prompt.md", prompt)
    result = worker_runner.run_codex_worker(job_id, "_planner", prompt, timeout_seconds=PLANNER_TIMEOUT)
    job_store.update_phase(
        job_id,
        "_planner",
        status=result["status"],
        duration_seconds=result["duration_seconds"],
        error=None if result["status"] == "succeeded" else result["result"][:1000],
    )
    if result["status"] != "succeeded":
        raise RuntimeError(result["result"])
    return extract_json_object(result["result"])


def phase_ready(phase, completed):
    return all(dep in completed for dep in phase.get("depends_on", []))


def run_phase(job_id, phase, user_text, memory_context, phase_results):
    phase_id = phase["id"]
    if job_store.is_cancelled(job_id):
        job_store.update_phase(job_id, phase_id, status="cancelled", error="job cancelled")
        return {"status": "cancelled", "result": "Job cancelled."}
    job_store.update_phase(job_id, phase_id, status="running")
    job_store.update_job(job_id, status="running", current_phase=phase_id)
    prompt = agent_prompts.worker_prompt(
        phase.get("role", "worker"),
        phase.get("objective", ""),
        user_text,
        memory_context,
        phase_results,
        phase.get("prompt", ""),
    )
    result = worker_runner.run_codex_worker(
        job_id,
        phase_id,
        prompt,
        timeout_seconds=int(phase.get("timeout_seconds") or DEFAULT_WORKER_TIMEOUT),
    )
    job_store.update_phase(
        job_id,
        phase_id,
        status=result["status"],
        duration_seconds=result["duration_seconds"],
        error=None if result["status"] == "succeeded" else result["result"][:1000],
    )
    return result


def run_phases(job_id, plan, user_text, memory_context):
    phases = plan["phases"]
    for phase in phases:
        job_store.init_phase(job_id, phase)

    pending = {phase["id"]: phase for phase in phases}
    completed = set()
    failed = {}
    phase_results = {}

    while pending:
        if job_store.is_cancelled(job_id):
            for phase_id in list(pending):
                job_store.update_phase(job_id, phase_id, status="cancelled", error="job cancelled")
            raise RuntimeError("job cancelled")

        ready = [phase for phase in pending.values() if phase_ready(phase, completed)]
        if not ready:
            raise RuntimeError(f"phase dependency deadlock: {sorted(pending)}")

        batch = ready[: max(1, MAX_PARALLEL)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            future_map = {
                executor.submit(run_phase, job_id, phase, user_text, memory_context, phase_results): phase
                for phase in batch
            }
            for future in concurrent.futures.as_completed(future_map):
                phase = future_map[future]
                phase_id = phase["id"]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"status": "failed", "result": f"{type(exc).__name__}: {exc}"}
                    job_store.update_phase(job_id, phase_id, status="failed", error=result["result"][:1000])
                phase_results[phase_id] = {
                    "role": phase.get("role"),
                    "status": result.get("status"),
                    "result": result.get("result"),
                }
                pending.pop(phase_id, None)
                if result.get("status") == "succeeded":
                    completed.add(phase_id)
                else:
                    failed[phase_id] = result.get("result")

        if failed:
            raise RuntimeError(f"phase failed: {failed}")

    return phase_results


def run_reporter(job_id, user_text, memory_context, phase_results):
    prompt = agent_prompts.reporter_prompt(user_text, memory_context, phase_results)
    phase = {"id": "_final_reporter", "role": "reporter", "objective": "Create final report", "timeout_seconds": REPORTER_TIMEOUT}
    job_store.init_phase(job_id, phase)
    result = worker_runner.run_codex_worker(job_id, "_final_reporter", prompt, timeout_seconds=REPORTER_TIMEOUT)
    job_store.update_phase(
        job_id,
        "_final_reporter",
        status=result["status"],
        duration_seconds=result["duration_seconds"],
        error=None if result["status"] == "succeeded" else result["result"][:1000],
    )
    final_report = result["result"]
    job_store.write_final_report(job_id, final_report)
    return final_report


def remember_job_async(job, final_report):
    def _worker():
        text = (
            f"Codex Orchestrator job_id: {job.get('job_id')}\n"
            f"User task:\n{job.get('user_text')}\n\n"
            f"Final report:\n{final_report}"
        )
        result = memory_adapter.remember_fact(
            text,
            metadata={"source": "telegram_job", "job_id": job.get("job_id"), "chat_id": job.get("chat_id"), "user_id": job.get("user_id")},
            session_id=str(job.get("chat_id")),
            timeout=180,
        )
        if not result.get("ok"):
            print(f"job memory remember skipped/failed: {result}", flush=True)

    threading.Thread(target=_worker, daemon=True).start()


def run_job(job_id, send_callback=None, finish_callback=None):
    job = job_store.get_job(job_id)
    if not job:
        return
    try:
        user_text = job["user_text"]
        chat_id = job["chat_id"]
        recent_context = job.get("recent_context", "")
        memory_context = build_memory_context(user_text, recent_context, chat_id)
        job_store.write_memory_context(job_id, memory_context)

        notify(send_callback, chat_id, f"任務「{job_title(job)}」開始由主代理規劃。\njob_id：{job_id}")

        try:
            plan = normalize_plan(run_planner(job_id, user_text, memory_context))
        except Exception as exc:
            plan = normalize_plan(fallback_plan(user_text, str(exc)))
        job_store.write_plan(job_id, plan)

        phase_names = "、".join(f"{job_store.phase_label(phase['id'])}({phase['id']})" for phase in plan["phases"])
        notify(send_callback, chat_id, f"任務「{job_title(job)}」規劃完成。\njob_id：{job_id}\n階段：{phase_names}")

        phase_results = run_phases(job_id, plan, user_text, memory_context)
        final_report = run_reporter(job_id, user_text, memory_context, phase_results)
        job_store.update_job(job_id, status="succeeded", current_phase=None, error=None)
        remember_job_async(job_store.get_job(job_id), final_report)
        notify(send_callback, chat_id, f"任務「{job_title(job_store.get_job(job_id))}」已完成。\njob_id：{job_id}\n\n{final_report}")
        if finish_callback:
            finish_callback(job_store.get_job(job_id), final_report)
    except Exception as exc:
        tb = traceback.format_exc()
        job_store.update_job(job_id, status="failed", current_phase=None, error=f"{type(exc).__name__}: {exc}")
        job_store.write_final_report(job_id, f"Job failed: {type(exc).__name__}: {exc}\n\n{tb[-4000:]}")
        if job:
            failed_job = job_store.get_job(job_id) or job
            notify(
                send_callback,
                job.get("chat_id"),
                f"任務「{job_title(failed_job)}」已中斷。\n"
                f"job_id：{job_id}\n"
                f"原因：{type(exc).__name__}: {exc}\n"
                f"你可以問「剛才任務做到哪？」查看已完成階段。",
            )


def start_job(user_text, chat_id, user_id, recent_context, send_callback=None, finish_callback=None):
    safe_text = memory_adapter.redact_secrets(user_text)
    job = job_store.create_job(safe_text, chat_id, user_id)
    job_store.update_job(job["job_id"], status="queued", recent_context=memory_adapter.redact_secrets(recent_context))
    if DURABLE_JOBS:
        try:
            unit, output = _start_systemd_worker(job["job_id"], mode="run")
            job_store.update_job(job["job_id"], worker_unit=unit, worker_launch=output)
            return job_store.get_job(job["job_id"]) or job
        except Exception as exc:
            job_store.update_job(job["job_id"], worker_launch_error=str(exc)[:1000])
            notify(send_callback, chat_id, f"durable worker 啟動失敗，改用 Gateway 內部 thread 執行。\njob_id：{job['job_id']}\n原因：{exc}")
    thread = threading.Thread(target=run_job, args=(job["job_id"], send_callback, finish_callback), daemon=True)
    thread.start()
    return job


def run_rerun_phase(job_id, phase_id, send_callback=None):
    job = job_store.get_job(job_id)
    plan = job_store.read_plan(job_id)
    memory_context = job_store.read_memory_context(job_id)
    if not job or not plan:
        if job:
            notify(send_callback, job.get("chat_id"), f"找不到任務「{job_title(job)}」的 plan。\njob_id：{job_id}")
        return
    phase = None
    for item in plan.get("phases", []):
        if item.get("id") == phase_id:
            phase = item
            break
    if not phase:
        notify(send_callback, job.get("chat_id"), f"找不到任務「{job_title(job)}」的階段：{phase_id}\njob_id：{job_id}")
        return
    previous_results = {}
    for dep in phase.get("depends_on", []):
        previous_results[dep] = {
            "status": (job_store.get_phase(job_id, dep) or {}).get("status"),
            "result": job_store.read_phase_text(job_id, dep, "result.md"),
        }
    job_store.update_job(job_id, status="running", current_phase=phase_id, error=None)
    result = run_phase(job_id, phase, job.get("user_text", ""), memory_context, previous_results)
    if result.get("status") == "succeeded":
        remaining_failed = []
        phase_results = {}
        for item in plan.get("phases", []):
            item_id = item.get("id")
            state = job_store.get_phase(job_id, item_id) or {}
            item_result = job_store.read_phase_text(job_id, item_id, "result.md")
            item_status = state.get("status")
            phase_results[item_id] = {
                "role": item.get("role"),
                "status": item_status,
                "result": item_result,
            }
            if item_status in {"failed", "cancelled"}:
                remaining_failed.append(item_id)
        if remaining_failed:
            job_store.update_job(
                job_id,
                status="failed",
                current_phase=None,
                error="remaining failed phases: " + ", ".join(remaining_failed),
            )
        else:
            final_report = run_reporter(job_id, job.get("user_text", ""), memory_context, phase_results)
            job_store.update_job(job_id, status="succeeded", current_phase=None, error=None)
            remember_job_async(job_store.get_job(job_id), final_report)
            notify(
                send_callback,
                job.get("chat_id"),
                f"任務「{job_title(job_store.get_job(job_id))}」已完成。\njob_id：{job_id}\n\n{final_report}",
            )
    else:
        job_store.update_job(job_id, status="failed", current_phase=None, error=result.get("result", "")[:1000])
    notify(
        send_callback,
        job.get("chat_id"),
        f"任務「{job_title(job)}」的 {job_store.phase_label(phase_id)} 重跑結果：{job_store.status_label(result.get('status'))}\n"
        f"job_id：{job_id}\n\n{result.get('result')}",
    )


def start_rerun_phase(job_id, phase_id, send_callback=None):
    if DURABLE_JOBS:
        job = job_store.get_job(job_id)
        try:
            unit, output = _start_systemd_worker(job_id, mode="rerun", phase_id=phase_id)
            job_store.update_job(job_id, status="queued", current_phase=phase_id, worker_unit=unit, worker_launch=output, error=None)
            return
        except Exception as exc:
            if job:
                notify(send_callback, job.get("chat_id"), f"durable rerun 啟動失敗，改用 Gateway 內部 thread 執行。\njob_id：{job_id}\n原因：{exc}")

    def _worker():
        run_rerun_phase(job_id, phase_id, send_callback=send_callback)

    threading.Thread(target=_worker, daemon=True).start()


def cancel_job(job_id):
    job_store.mark_cancelled(job_id)
    prefix = f"{JOB_UNIT_PREFIX}-{job_store.safe_name(job_id)}"
    try:
        subprocess.run(
            ["systemctl", "stop", f"{prefix}*.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        pass


def recover_active_jobs():
    recovered = []
    failed = []
    for job in job_store.list_jobs(limit=1000):
        if job.get("status") not in {"pending", "queued", "planning", "running"}:
            continue
        job_id = job.get("job_id")
        if job_id and is_job_unit_active(job_id):
            recovered.append(job_id)
            continue
        if job_id:
            job_store.update_job(job_id, status="failed", current_phase=None, error="worker not active after gateway restart")
            failed.append(job_id)
    return {"recovered": recovered, "failed": failed}
