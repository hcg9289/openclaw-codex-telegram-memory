#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
MAX_ACTIVE_WORK_TASKS = int(os.environ.get("MAX_ACTIVE_WORK_TASKS", "2"))
UNIT_PREFIX = os.environ.get("TASK_UNIT_PREFIX", "codex-telegram-task")


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


def start_task(task):
    import task_store

    task_id = task["task_id"]
    unit = f"{UNIT_PREFIX}-{task_store.safe_name(task_id)}"
    cmd = [
        "/usr/bin/systemd-run",
        "--unit",
        unit,
        "--collect",
        "--property",
        "KillMode=control-group",
        "--property",
        "Restart=no",
        "--property",
        "WorkingDirectory=" + str(APP_DIR),
        "--setenv",
        "PYTHONUNBUFFERED=1",
        sys.executable,
        str(APP_DIR / "task_worker.py"),
        task_id,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        task_store.update_task(task_id, status="failed", error=("systemd-run failed: " + result.stdout)[-1000:])
        task_store.append_event(task_id, "launch_failed", {"output": result.stdout[-1000:]})
        return False
    task_store.update_task(task_id, status="starting", unit_name=unit, launch_output=result.stdout.strip())
    task_store.append_event(task_id, "launched", {"unit_name": unit})
    return True


def main():
    load_env_file()
    sys.path.insert(0, str(APP_DIR))
    import task_store

    if len(sys.argv) == 2:
        task = task_store.get_task(sys.argv[1])
        if not task:
            raise SystemExit(f"task not found: {sys.argv[1]}")
        start_task(task)
        return

    active = task_store.active_work_count()
    for task in task_store.queued_work_tasks(limit=20):
        if active >= MAX_ACTIVE_WORK_TASKS:
            break
        if start_task(task):
            active += 1


if __name__ == "__main__":
    main()
