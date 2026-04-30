#!/usr/bin/env python3
import os
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"


def load_env_file(path):
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


def main():
    load_env_file(ENV_FILE)
    sys.path.insert(0, str(APP_DIR))

    import orchestrator

    if len(sys.argv) < 3:
        raise SystemExit("usage: job_worker.py <run|rerun> <job_id> [phase_id]")
    mode = sys.argv[1]
    job_id = sys.argv[2]
    if mode == "run":
        orchestrator.run_job(job_id, send_callback=None, finish_callback=None)
        return
    if mode == "rerun":
        if len(sys.argv) < 4:
            raise SystemExit("usage: job_worker.py rerun <job_id> <phase_id>")
        orchestrator.run_rerun_phase(job_id, sys.argv[3], send_callback=None)
        return
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
