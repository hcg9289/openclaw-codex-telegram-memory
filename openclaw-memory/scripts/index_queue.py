#!/opt/openclaw-memory/venv/bin/python
import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


def _load_memory_env():
    env_path = Path("/opt/openclaw-memory/config/memory.env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_memory_env()


ROOT = Path(os.environ.get("OPENCLAW_MEMORY_ROOT", "/opt/openclaw-memory"))
QUEUE = ROOT / "queue"
PROCESSED = QUEUE / "processed"
REJECTED = QUEUE / "rejected"
LOGS = ROOT / "logs"
DATASET = "openclaw_memory"
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-:.]{12,})"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{25,}\b"),
]

def now():
    return datetime.now(timezone.utc).isoformat()

def reject(path: Path, reason: str):
    REJECTED.mkdir(parents=True, exist_ok=True)
    target = REJECTED / f"{path.stem}.{int(datetime.now().timestamp())}{path.suffix}"
    shutil.move(str(path), str(target))
    print(json.dumps({"ts": now(), "event": "rejected", "file": str(path), "reason": reason}, ensure_ascii=False))

def redact_and_validate(text: str, path: Path) -> str | None:
    if path.name in {".env", "id_ed25519"}:
        return None
    if "PRIVATE KEY" in text:
        return None
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            redacted = pattern.sub(lambda m: m.group(0).split("=", 1)[0] + "=[REDACTED]" if "=" in m.group(0) else "[REDACTED_SECRET]", redacted)
    return redacted

def normalize_payload(path: Path) -> str | None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    safe = redact_and_validate(raw, path)
    if safe is None:
        return None
    if path.suffix.lower() == ".json":
        try:
            obj = json.loads(safe)
            safe = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return f"Source file: {path.name}\nIndexed at: {now()}\n\n{safe}"

async def main():
    import cognee
    PROCESSED.mkdir(parents=True, exist_ok=True)
    REJECTED.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    files = sorted([p for p in QUEUE.iterdir() if p.is_file() and p.suffix.lower() in {".md", ".json", ".txt"}])
    if not files:
        print(json.dumps({"ts": now(), "event": "empty_queue"}, ensure_ascii=False))
        return
    for path in files:
        payload = normalize_payload(path)
        if payload is None:
            reject(path, "secret_or_blocked_filename")
            continue
        print(json.dumps({"ts": now(), "event": "adding", "file": str(path)}, ensure_ascii=False))
        await cognee.add(payload, dataset_name=DATASET)
        await cognee.cognify(datasets=[DATASET], data_per_batch=1)
        target = PROCESSED / f"{path.stem}.{int(datetime.now().timestamp())}{path.suffix}"
        shutil.move(str(path), str(target))
        print(json.dumps({"ts": now(), "event": "processed", "file": str(path), "moved_to": str(target)}, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
