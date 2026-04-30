#!/opt/openclaw-memory/venv/bin/python
import asyncio
import json
import os
import sys
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


async def main():
    import cognee
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        raise SystemExit("usage: search_memory.py <query>")
    results = await cognee.search(query_text=query, datasets=["openclaw_memory"], top_k=5)
    out = []
    for item in results:
        out.append(str(item))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
