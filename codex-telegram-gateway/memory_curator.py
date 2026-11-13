#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
LOCK_PATH = Path(os.environ.get("MEMORY_CURATOR_LOCK_PATH", "/var/lib/codex-telegram-gateway/memory/memory_curator.lock"))


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


@contextlib.contextmanager
def curator_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def extract_json(text):
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def compact_messages(messages):
    lines = []
    for item in messages:
        role = item.get("role", "unknown")
        ts = item.get("ts", "")
        text = str(item.get("text") or "").strip()
        if len(text) > 1800:
            text = text[:1800] + "...[truncated]"
        lines.append(f"{ts} {role}: {text}")
    return "\n".join(lines)


def build_prompt(batch, facts_text, recall_text):
    return f"""You are Codex Memory Curator for OpenClaw Telegram Gateway.

Goal: compress exactly this conversation batch into durable long-term memory candidates.

Rules:
- Output JSON only. No Markdown, no commentary.
- Traditional Chinese for prose values.
- Do not store secrets, tokens, private keys, passwords, or full .env contents.
- Do not overwrite current truth. Put current-state updates only in facts_candidates.
- Cognee memory is historical context, not current truth.
- Prefer stable decisions, user preferences, project state, verified results, paths, service names, and unresolved risks.
- Ignore greetings, repeated acknowledgements, transient wording, and unverified speculation.

Required JSON shape:
{{
  "memory_summary": "",
  "decisions": [],
  "stable_preferences": [],
  "project_state_updates": [],
  "facts_candidates": [],
  "unresolved_questions": [],
  "do_not_store": []
}}

[AUTHORITATIVE_CURRENT_FACTS]
{facts_text}
[/AUTHORITATIVE_CURRENT_FACTS]

[HISTORICAL_COGNEE_RECALL_NON_AUTHORITATIVE]
{recall_text or "(no recall)"}
[/HISTORICAL_COGNEE_RECALL_NON_AUTHORITATIVE]

[CONVERSATION_BATCH]
batch_id: {batch.get("batch_id")}
started_at: {batch.get("started_at")}
ended_at: {batch.get("ended_at")}
turn_count: {batch.get("turn_count")}

{compact_messages(batch.get("messages") or [])}
[/CONVERSATION_BATCH]
"""


def run_codex_curator(prompt):
    codex_bin = os.environ.get("CODEX_BIN", "/usr/bin/codex")
    workdir = os.environ.get("CODEX_WORKDIR", "/root")
    timeout = int(os.environ.get("MEMORY_CURATOR_CODEX_TIMEOUT_SECONDS", "1800"))
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = out_file.name
    try:
        cmd = [
            codex_bin,
            "exec",
            "--model",
            os.environ.get("MEMORY_CURATOR_CODEX_MODEL", "gpt-5.5"),
            "-c",
            'model_reasoning_effort="xhigh"',
            "--sandbox",
            "read-only",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            workdir,
            "--output-last-message",
            out_path,
            prompt,
        ]
        completed = subprocess.run(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        if completed.returncode != 0:
            raise RuntimeError(f"codex exit={completed.returncode}: {(completed.stdout or '')[-2000:]}")
        return answer or (completed.stdout or "").strip()
    finally:
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass


def render_memory_text(batch, result):
    sections = [
        f"Conversation memory batch: {batch.get('batch_id')}",
        f"UTC range: {batch.get('started_at')} -> {batch.get('ended_at')}",
        f"Turn count: {batch.get('turn_count')}",
        "",
        "Summary:",
        str(result.get("memory_summary") or "").strip(),
    ]
    for key, title in [
        ("decisions", "Decisions"),
        ("stable_preferences", "Stable preferences"),
        ("project_state_updates", "Project state updates"),
        ("unresolved_questions", "Unresolved questions"),
    ]:
        values = result.get(key) or []
        if values:
            sections.extend(["", f"{title}:"])
            sections.extend(f"- {value}" for value in values)
    return "\n".join(sections).strip()


def process_batch(path, batch):
    import facts_store
    import memory_adapter
    import memory_queue

    query = compact_messages(batch.get("messages") or [])[-4000:]
    recall_result = memory_adapter.recall_memory(query, top_k=5, timeout=30) if query.strip() else {"ok": True, "text": ""}
    recall_text = recall_result.get("text") if recall_result.get("ok") else f"(recall unavailable: {recall_result.get('error')})"
    prompt = build_prompt(batch, facts_store.format_facts_for_prompt(), recall_text)
    memory_queue.update_batch(path, status="curating", curator_started_at=memory_queue.utc_now())
    raw = run_codex_curator(prompt)
    result = extract_json(raw)
    for key in [
        "memory_summary",
        "decisions",
        "stable_preferences",
        "project_state_updates",
        "facts_candidates",
        "unresolved_questions",
        "do_not_store",
    ]:
        result.setdefault(key, [] if key != "memory_summary" else "")
    memory_text = render_memory_text(batch, result)
    enqueue_result = memory_queue.enqueue_memory(
        memory_text,
        metadata={
            "source": "codex_memory_curator",
            "batch_id": batch.get("batch_id"),
            "started_at": batch.get("started_at"),
            "ended_at": batch.get("ended_at"),
        },
        source="codex_memory_curator",
    )
    memory_queue.update_batch(
        path,
        status="curated" if enqueue_result.get("ok") else "failed",
        curator_finished_at=memory_queue.utc_now(),
        curator_result=result,
        memory_queue_result=enqueue_result,
        error=None if enqueue_result.get("ok") else enqueue_result.get("error"),
    )
    subprocess.Popen(
        [sys.executable, str(APP_DIR / "memory_writer.py"), "--once", "--limit", "5"],
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": bool(enqueue_result.get("ok")), "batch_id": batch.get("batch_id"), "enqueue": enqueue_result}


def process_once(limit=1):
    load_env_file()
    sys.path.insert(0, str(APP_DIR))
    import memory_queue

    results = []
    with curator_lock():
        for path, batch in memory_queue.pending_batches(limit=limit):
            try:
                results.append(process_batch(path, batch))
            except Exception as exc:
                memory_queue.update_batch(
                    path,
                    status="failed",
                    curator_finished_at=memory_queue.utc_now(),
                    error=str(exc)[:1000],
                )
                results.append({"ok": False, "batch_id": batch.get("batch_id"), "error": str(exc)})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()
    results = process_once(limit=args.limit)
    print(json.dumps({"results": results}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
