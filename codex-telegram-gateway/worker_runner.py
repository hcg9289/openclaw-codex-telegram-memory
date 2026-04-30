#!/usr/bin/env python3
import os
import subprocess
import tempfile
import time
from pathlib import Path

import job_store
import memory_adapter


CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/bin/codex")
CODEX_WORKDIR = os.environ.get("CODEX_WORKDIR", "/root")


def _to_text(value):
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_codex_worker(job_id, phase_id, prompt, timeout_seconds=900):
    job_store.write_phase_text(job_id, phase_id, "prompt.md", prompt)
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = out_file.name

    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--output-last-message",
        out_path,
        prompt,
    ]
    if job_store.is_cancelled(job_id):
        result = "Phase cancelled before start."
        job_store.write_phase_text(job_id, phase_id, "stdout.log", "")
        job_store.write_phase_text(job_id, phase_id, "stderr.log", "")
        job_store.write_phase_text(job_id, phase_id, "result.md", result)
        return {"status": "cancelled", "result": result, "returncode": None, "duration_seconds": 0}

    start = time.time()
    stdout_text = ""
    answer = ""
    status = "failed"
    returncode = None
    try:
        completed = subprocess.run(
            cmd,
            cwd=CODEX_WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout_text = memory_adapter.redact_secrets(_to_text(completed.stdout))
        try:
            answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            answer = ""
        answer = memory_adapter.redact_secrets(answer)
        if completed.returncode == 0:
            status = "succeeded"
            result = answer or stdout_text[-6000:] or "(Codex finished without text output)"
        else:
            result = answer or stdout_text[-6000:] or f"Codex failed with exit code {completed.returncode}."
    except subprocess.TimeoutExpired:
        status = "failed"
        result = f"Phase timed out after {timeout_seconds} seconds."
    finally:
        if not answer:
            try:
                answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
            except FileNotFoundError:
                answer = ""
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass

    job_store.write_phase_text(job_id, phase_id, "stdout.log", stdout_text)
    job_store.write_phase_text(job_id, phase_id, "stderr.log", "")

    job_store.write_phase_text(job_id, phase_id, "result.md", result)
    return {
        "status": status,
        "result": result,
        "returncode": returncode,
        "duration_seconds": round(time.time() - start, 2),
    }
