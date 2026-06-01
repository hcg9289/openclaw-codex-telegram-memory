#!/usr/bin/env python3
import asyncio
import contextlib
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


MEMORY_ENV_PATH = Path(os.environ.get("MEMORY_ENV_PATH", "/opt/openclaw-memory/config/memory.env"))
COGNEE_PYTHON = os.environ.get("COGNEE_PYTHON", "/opt/openclaw-memory/venv/bin/python")
DEFAULT_DATASET = "openclaw_memory"
DEFAULT_TIMEOUT = int(os.environ.get("COGNEE_TIMEOUT_SECONDS", "45"))
RECALL_TOP_K = int(os.environ.get("COGNEE_RECALL_TOP_K", "5"))
LOCK_PATH = Path(os.environ.get("COGNEE_WORKER_LOCK_PATH", "/var/lib/codex-telegram-gateway/memory/cognee_worker.lock"))

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.I | re.S),
    re.compile(r"(?i)BEGIN\s+(OPENSSH|RSA|DSA|EC|ED25519)?\s*PRIVATE\s+KEY"),
    re.compile(r"(?i)END\s+(OPENSSH|RSA|DSA|EC|ED25519)?\s*PRIVATE\s+KEY"),
    re.compile(r"(?i)(TELEGRAM_BOT_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|LLM_API_KEY|API_KEY|TOKEN|PASSWORD|PASSWD|SECRET)\s*=\s*[^\s]+"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"(?i)\bpassword\s*[:=]\s*[^\s]+"),
]


def redact_secrets(text):
    if not text:
        return ""
    redacted = str(text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def contains_secret(text):
    if not text:
        return False
    return redact_secrets(text) != str(text)


def load_env_file(path=MEMORY_ENV_PATH):
    data = {}
    if not Path(path).exists():
        return data
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        data[key] = value
    return data


def env_value(key, default=None):
    return os.environ.get(key) or load_env_file().get(key) or default


def write_dataset():
    return env_value("COGNEE_WRITE_DATASET", env_value("COGNEE_DATASET", DEFAULT_DATASET))


def recall_datasets():
    raw = env_value("COGNEE_RECALL_DATASETS", write_dataset())
    values = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    return values or [write_dataset()]


@contextlib.contextmanager
def cognee_worker_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_worker(action, payload, timeout=DEFAULT_TIMEOUT):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as request_file:
        json.dump(payload, request_file, ensure_ascii=False)
        request_path = request_file.name
    try:
        cmd = [COGNEE_PYTHON, __file__, "__worker__", action, request_path]
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout.strip()
        if completed.returncode != 0:
            return {
                "ok": False,
                "error": f"worker exit={completed.returncode}",
                "stderr": completed.stderr.strip()[-3000:],
                "stdout": stdout[-1000:],
            }
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "worker returned non-json", "stdout": stdout[-3000:], "stderr": completed.stderr.strip()[-1000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"worker timed out after {timeout}s"}
    finally:
        try:
            os.unlink(request_path)
        except FileNotFoundError:
            pass


def recall_memory(query, session_id=None, top_k=RECALL_TOP_K, timeout=DEFAULT_TIMEOUT):
    safe_query = redact_secrets(query)
    if not safe_query.strip():
        return {"ok": True, "items": [], "text": ""}
    payload = {"query": safe_query, "top_k": top_k, "datasets": recall_datasets()}
    return _run_worker("recall", payload, timeout=timeout)


def remember_fact(text, metadata=None, session_id=None, timeout=120):
    if contains_secret(text):
        return {"ok": False, "skipped": True, "error": "secret-like content was not written to cognee"}
    payload = {
        "text": redact_secrets(text),
        "metadata": metadata or {},
        "dataset": write_dataset(),
    }
    return _run_worker("remember", payload, timeout=timeout)


def memory_status():
    env = load_env_file()
    status = {
        "memory_env": str(MEMORY_ENV_PATH),
        "memory_env_exists": MEMORY_ENV_PATH.exists(),
        "cognee_python": COGNEE_PYTHON,
        "cognee_python_exists": Path(COGNEE_PYTHON).exists(),
        "dataset": write_dataset(),
        "recall_datasets": recall_datasets(),
        "llm_model": env.get("LLM_MODEL"),
        "llm_endpoint": env.get("LLM_ENDPOINT"),
        "embedding_model": env.get("EMBEDDING_MODEL"),
        "vector_db": env.get("VECTOR_DB_PROVIDER"),
        "graph_db": env.get("GRAPH_DATABASE_PROVIDER"),
        "db_host": env.get("DB_HOST"),
        "db_port": env.get("DB_PORT"),
    }
    endpoint = env.get("LLM_ENDPOINT")
    if endpoint:
        try:
            with urllib.request.urlopen(endpoint.rstrip("/") + "/models", timeout=5) as response:
                status["llm_endpoint_ok"] = response.status < 500
        except Exception as exc:
            status["llm_endpoint_ok"] = False
            status["llm_endpoint_error"] = str(exc)[:200]
    try:
        host = env.get("DB_HOST", "127.0.0.1")
        port = int(env.get("DB_PORT", "15432"))
        with socket.create_connection((host, port), timeout=5):
            status["db_port_ok"] = True
    except Exception as exc:
        status["db_port_ok"] = False
        status["db_port_error"] = str(exc)[:200]
    for service in [
        "openclaw-memory-minimax-proxy.service",
        "openclaw-memory-kimi-proxy.service",
        "codex-telegram-gateway.service",
    ]:
        result = subprocess.run(["systemctl", "is-active", service], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        status[service] = result.stdout.strip() or "unknown"
    worker = _run_worker("version", {}, timeout=20)
    status["cognee_worker_ok"] = bool(worker.get("ok"))
    status["cognee_version"] = worker.get("version")
    status["cognee_worker_error"] = worker.get("error")
    return status


def _load_memory_env_into_process():
    for key, value in load_env_file().items():
        os.environ.setdefault(key, value)


async def _worker_recall(payload):
    _load_memory_env_into_process()
    import cognee
    from cognee.modules.search.types import SearchType

    results = await cognee.search(
        query_text=payload["query"],
        query_type=SearchType.CHUNKS,
        datasets=payload.get("datasets") or [payload.get("dataset") or write_dataset()],
        top_k=int(payload.get("top_k") or RECALL_TOP_K),
    )
    items = [str(item) for item in results]
    return {"ok": True, "items": items, "text": "\n\n".join(items)}


async def _worker_remember(payload):
    _load_memory_env_into_process()
    import cognee

    metadata = payload.get("metadata") or {}
    header = "\n".join(f"{key}: {value}" for key, value in sorted(metadata.items()) if value is not None)
    data = f"{header}\n\n{payload['text']}".strip()
    result = await cognee.remember(
        data,
        dataset_name=payload.get("dataset") or write_dataset(),
        run_in_background=False,
    )
    return {"ok": True, "result": str(result)}


async def _worker_version():
    _load_memory_env_into_process()
    import importlib.metadata as md
    import cognee  # noqa: F401

    return {"ok": True, "version": md.version("cognee")}


def _worker_main():
    if len(sys.argv) != 4:
        print(json.dumps({"ok": False, "error": "usage: memory_adapter.py __worker__ <action> <request.json>"}))
        return 2
    action = sys.argv[2]
    payload = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    try:
        with cognee_worker_lock():
            if action == "recall":
                result = asyncio.run(_worker_recall(payload))
            elif action == "remember":
                result = asyncio.run(_worker_remember(payload))
            elif action == "version":
                result = asyncio.run(_worker_version())
            else:
                result = {"ok": False, "error": f"unknown action: {action}"}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "__worker__":
    raise SystemExit(_worker_main())
