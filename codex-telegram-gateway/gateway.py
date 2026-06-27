#!/usr/bin/env python3
import json
import hashlib
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import facts_store
import job_store
import memory_adapter
import memory_queue
import orchestrator
import router
import task_store


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(item.strip())
    for item in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if item.strip()
}
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/bin/codex")
CODEX_WORKDIR = os.environ.get("CODEX_WORKDIR", "/root")
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "300"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
MEMORY_DIR = STATE_DIR / "memory"
OFFSET_FILE = STATE_DIR / "offset.txt"
RECENT_MESSAGES_FILE = MEMORY_DIR / "recent_messages.jsonl"
SUPERVISOR_RECENT_FILE = MEMORY_DIR / "supervisor_recent.jsonl"
LAST_CONTEXT_FILE = MEMORY_DIR / "last_context.json"
PENDING_MESSAGE_BATCHES_FILE = STATE_DIR / "pending_message_batches.json"
API_BASE = f"https://api.telegram.org/bot{TOKEN}"
MAX_MESSAGE = 3900
MAX_RECENT_TURNS = int(os.environ.get("MAX_RECENT_TURNS", "20"))
MAX_ACTIVE_WORK_TASKS = int(os.environ.get("MAX_ACTIVE_WORK_TASKS", "2"))
MESSAGE_BATCH_DELAY_SECONDS = float(os.environ.get("MESSAGE_BATCH_DELAY_SECONDS", "8"))
DOCUMENT_BATCH_DELAY_SECONDS = float(os.environ.get("DOCUMENT_BATCH_DELAY_SECONDS", "45"))
MAX_TELEGRAM_DOCUMENT_BYTES = int(os.environ.get("MAX_TELEGRAM_DOCUMENT_BYTES", str(50 * 1024 * 1024)))
TELEGRAM_DOCUMENT_RETRIES = int(os.environ.get("TELEGRAM_DOCUMENT_RETRIES", "5"))
TELEGRAM_GETFILE_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_GETFILE_TIMEOUT_SECONDS", "45"))
TELEGRAM_DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS", "300"))
TELEGRAM_DOCUMENT_RETRY_BACKOFF_SECONDS = float(os.environ.get("TELEGRAM_DOCUMENT_RETRY_BACKOFF_SECONDS", "2"))
INCOMING_ATTACHMENTS_DIR = STATE_DIR / "incoming_attachments"
ATTACHMENT_FAILURE_LOG = STATE_DIR / "attachment_download_failures.jsonl"
STOP = False


def handle_signal(_signum, _frame):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def api_call(method, data=None, timeout=35):
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(f"{API_BASE}/{method}", data=payload)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {parsed}")
    return parsed["result"]


def safe_attachment_filename(value, fallback="telegram_document"):
    raw = Path(str(value or fallback)).name.strip()
    if not raw:
        raw = fallback
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    safe = safe.strip("._") or fallback
    return safe[:160]


def attachment_file_id_hash(file_id):
    return hashlib.sha256(str(file_id or "").encode("utf-8")).hexdigest()[:16]


def safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def select_largest_telegram_photo(photos):
    candidates = [item for item in photos or [] if isinstance(item, dict) and item.get("file_id")]
    if not candidates:
        return None

    def score(item):
        width = safe_int(item.get("width"))
        height = safe_int(item.get("height"))
        return (safe_int(item.get("file_size")), width * height, width, height)

    return max(candidates, key=score)


def is_retryable_telegram_download_error(exc):
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, urllib.error.URLError):
        return True
    return "timed out" in str(exc).lower()


def retry_telegram_document_operation(phase, operation):
    attempts = max(1, TELEGRAM_DOCUMENT_RETRIES)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return operation(attempt)
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not is_retryable_telegram_download_error(exc):
                raise
            delay = min(30.0, TELEGRAM_DOCUMENT_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            print(
                f"telegram document {phase} failed: "
                f"attempt={attempt}/{attempts} error={type(exc).__name__}; retrying in {delay:.1f}s",
                flush=True,
            )
            time.sleep(delay)
    raise last_exc


def log_attachment_download_failure(message, document, phase, attempts, exc):
    _jsonl_append(
        ATTACHMENT_FAILURE_LOG,
        {
            "at": task_store.utc_now(),
            "phase": phase,
            "attempts": attempts,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "message_id": message.get("message_id"),
            "chat_id": message.get("chat", {}).get("id"),
            "user_id": message.get("from", {}).get("id"),
            "original_filename": document.get("file_name"),
            "mime_type": document.get("mime_type"),
            "declared_file_size": document.get("file_size"),
            "telegram_file_unique_id": document.get("file_unique_id"),
            "telegram_file_id_hash": attachment_file_id_hash(document.get("file_id")),
        },
    )


def download_telegram_document(message, document, chat_id, user_id):
    file_id = document.get("file_id")
    if not file_id:
        raise ValueError("Telegram document has no file_id")
    declared_size = int(document.get("file_size") or 0)
    if declared_size and declared_size > MAX_TELEGRAM_DOCUMENT_BYTES:
        raise ValueError(f"Telegram document too large: {declared_size} bytes")

    try:
        file_info = retry_telegram_document_operation(
            "getFile",
            lambda _attempt: api_call(
                "getFile",
                {"file_id": file_id},
                timeout=TELEGRAM_GETFILE_TIMEOUT_SECONDS,
            ),
        )
    except Exception as exc:
        log_attachment_download_failure(message, document, "getFile", max(1, TELEGRAM_DOCUMENT_RETRIES), exc)
        raise
    file_path = file_info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram getFile returned no file_path")

    batch_dir = INCOMING_ATTACHMENTS_DIR / task_store.safe_name(_pending_batch_key(chat_id, user_id))
    batch_dir.mkdir(parents=True, exist_ok=True)
    original_name = document.get("file_name") or f"{document.get('file_unique_id') or message.get('message_id')}.bin"
    safe_name = safe_attachment_filename(original_name)
    message_id = message.get("message_id") or int(time.time())
    destination = batch_dir / f"{message_id}_{safe_name}"
    if destination.exists():
        destination = batch_dir / f"{message_id}_{int(time.time())}_{safe_name}"
    tmp = destination.with_suffix(destination.suffix + ".tmp")

    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

    def download_once(_attempt):
        total = 0
        try:
            with urllib.request.urlopen(
                url,
                timeout=TELEGRAM_DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response, tmp.open("wb") as file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_TELEGRAM_DOCUMENT_BYTES:
                        raise ValueError(f"Telegram document too large while downloading: {total} bytes")
                    file.write(chunk)
        except Exception:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        return total

    try:
        total = retry_telegram_document_operation("download", download_once)
        os.replace(tmp, destination)
        os.chmod(destination, 0o600)
    except Exception as exc:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        log_attachment_download_failure(
            message,
            document,
            "download",
            max(1, TELEGRAM_DOCUMENT_RETRIES),
            exc,
        )
        raise

    return {
        "kind": "telegram_document",
        "original_filename": str(original_name),
        "mime_type": document.get("mime_type"),
        "file_size": declared_size or total,
        "telegram_file_unique_id": document.get("file_unique_id"),
        "message_id": message.get("message_id"),
        "downloaded_path": str(destination),
        "received_at": task_store.utc_now(),
    }


def download_telegram_photo(message, photo, chat_id, user_id):
    message_id = message.get("message_id") or int(time.time())
    unique_name = safe_attachment_filename(
        photo.get("file_unique_id") or attachment_file_id_hash(photo.get("file_id")),
        fallback="image",
    )
    document_like = dict(photo)
    document_like["file_name"] = f"telegram_photo_{message_id}_{unique_name}.jpg"
    document_like["mime_type"] = "image/jpeg"

    attachment = download_telegram_document(message, document_like, chat_id, user_id)
    attachment["kind"] = "telegram_photo"
    attachment["width"] = safe_int(photo.get("width"))
    attachment["height"] = safe_int(photo.get("height"))
    return attachment


def is_polling_timeout(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    return "timed out" in str(exc).lower()


def send_message(chat_id, text):
    text = text or "(empty response)"
    chunks = [text[i : i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)]
    for chunk in chunks:
        api_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )


def read_offset():
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_offset(offset):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    os.replace(tmp, OFFSET_FILE)


def atomic_write_json(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        memory_adapter.redact_secrets(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    os.chmod(path, mode)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _jsonl_append(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False) + "\n")


def _read_jsonl(path, limit=None):
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit:
        lines = lines[-limit:]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _trim_recent_messages():
    items = _read_jsonl(RECENT_MESSAGES_FILE)
    keep = items[-MAX_RECENT_TURNS * 2 :]
    tmp = RECENT_MESSAGES_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in keep) + ("\n" if keep else ""), encoding="utf-8")
    os.replace(tmp, RECENT_MESSAGES_FILE)


def kick_memory_writer():
    subprocess.Popen(
        ["/usr/bin/python3", "/opt/codex-telegram-gateway/memory_writer.py", "--once", "--limit", "10"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/opt/codex-telegram-gateway",
    )


def kick_memory_curator():
    subprocess.Popen(
        ["/usr/bin/python3", "/opt/codex-telegram-gateway/memory_curator.py", "--once", "--limit", "1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/opt/codex-telegram-gateway",
    )


def _maybe_create_conversation_batch():
    with memory_queue.recent_state_lock():
        state = memory_queue.read_recent_state()
        last_batched_seq = int(state.get("last_batched_seq") or 0)
        items = [
            item
            for item in _read_jsonl(RECENT_MESSAGES_FILE)
            if isinstance(item.get("seq"), int)
            and item.get("seq", 0) > last_batched_seq
            and item.get("role") in {"user", "assistant"}
        ]
        assistant_seen = 0
        cutoff_seq = None
        for item in items:
            if item.get("role") == "assistant":
                assistant_seen += 1
                if assistant_seen >= MAX_RECENT_TURNS:
                    cutoff_seq = item.get("seq")
                    break
        if cutoff_seq is None:
            return None
        batch_items = [item for item in items if item.get("seq", 0) <= cutoff_seq]
        if not batch_items:
            return None
        related_task_ids = [
            (item.get("metadata") or {}).get("task_id")
            for item in batch_items
            if isinstance(item.get("metadata"), dict)
        ]
        batch = memory_queue.create_conversation_batch(
            batch_items,
            related_task_ids=related_task_ids,
            from_seq=batch_items[0].get("seq"),
            to_seq=cutoff_seq,
        )
    kick_memory_curator()
    return batch


def append_recent(role, text, chat_id, user_id, metadata=None):
    safe_text = memory_adapter.redact_secrets(text)
    metadata = metadata or {}
    if metadata.get("source") in {"supervisor", "legacy_job"}:
        _jsonl_append(
            SUPERVISOR_RECENT_FILE,
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "role": role,
                "chat_id": chat_id,
                "user_id": user_id,
                "text": safe_text,
                "metadata": metadata,
            },
        )
        return None
    seq = memory_queue.next_recent_seq()
    _jsonl_append(
        RECENT_MESSAGES_FILE,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seq": seq,
            "role": role,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": safe_text,
            "metadata": metadata,
        },
    )
    _trim_recent_messages()
    if role == "assistant":
        return _maybe_create_conversation_batch()
    return None


def clear_recent_memory():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    RECENT_MESSAGES_FILE.write_text("", encoding="utf-8")
    LAST_CONTEXT_FILE.write_text("{}", encoding="utf-8")
    memory_queue.write_recent_state({"next_seq": 1, "last_batched_seq": 0})


def format_recent_context(limit_turns=MAX_RECENT_TURNS):
    items = _read_jsonl(RECENT_MESSAGES_FILE, limit_turns * 2)
    if not items:
        return "(no recent Telegram context)"
    lines = []
    for item in items:
        role = item.get("role", "unknown")
        text = (item.get("text") or "").strip()
        if len(text) > 1200:
            text = text[:1200] + "...[truncated]"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def read_agents_excerpt():
    path = Path("/root/AGENTS.md")
    if not path.exists():
        return "(no /root/AGENTS.md found)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = []
    for line in lines:
        if any(keyword in line for keyword in ["本機", "Must", "禁止", "secrets", "Skills", "指令", "驗證", "目標系統", "工程負責人", "長任務", "證據", "接管", "補救", "人話", "heartbeat", "checkpoint"]):
            selected.append(line)
        if len(selected) >= 35:
            break
    return "\n".join(selected) or "\n".join(lines[:40])


def build_recall_query(user_text):
    recent = format_recent_context(limit_turns=3)
    return f"Current user message:\n{user_text}\n\nRecent Telegram context:\n{recent}"


def build_prompt(user_text, recall_text, recent_context):
    return f"""你正在 VPS 的 Codex Telegram Gateway 中執行。

請嚴格遵守 /root/AGENTS.md；以下是重點摘錄：
{read_agents_excerpt()}

[AUTHORITATIVE_CURRENT_FACTS]
以下是 facts.json 的權威當前事實。若這裡與 cognee 歷史記憶衝突，必須以這裡為準：
{facts_store.format_facts_for_prompt()}
[/AUTHORITATIVE_CURRENT_FACTS]

[RECENT_TELEGRAM_CONTEXT]
{recent_context}
[/RECENT_TELEGRAM_CONTEXT]

[HISTORICAL_COGNEE_MEMORY_NON_AUTHORITATIVE]
以下是 cognee recall 找到的歷史/背景記憶，可能相關，也可能過時；不可把它當作 current truth：
{recall_text or "(no relevant cognee memory)"}
[/HISTORICAL_COGNEE_MEMORY_NON_AUTHORITATIVE]

使用者本次訊息：
{user_text}
"""


def should_use_orchestrator(user_text):
    text = str(user_text or "").strip()
    lowered = text.lower()
    heavy_keywords = [
        "檢查",
        "分析",
        "修",
        "修改",
        "實作",
        "實現",
        "部署",
        "安裝",
        "更新",
        "回測",
        "跑一次",
        "執行",
        "debug",
        "bug",
        "error",
        "錯誤",
        "服務",
        "systemd",
        "docker",
        "git",
        "vps",
        "/root/",
        "/opt/",
    ]
    if any(keyword in lowered for keyword in heavy_keywords):
        return True
    if len(text) >= 120 and any(mark in text for mark in ["?", "？", "請", "幫"]):
        return True
    return False


def direct_answer(user_text, chat_id, user_id, recent_context):
    recall_result = memory_adapter.recall_memory(user_text, session_id=str(chat_id), top_k=5, timeout=45)
    recall_text = recall_result.get("text") if recall_result.get("ok") else f"(cognee recall failed: {recall_result.get('error')})"
    save_last_context(chat_id, user_id, recall_result)
    answer = memory_adapter.redact_secrets(run_codex(build_prompt(user_text, recall_text, recent_context)))
    send_message(chat_id, answer)
    append_recent("user", user_text, chat_id, user_id, metadata={"source": "telegram_direct"})
    append_recent("assistant", answer, chat_id, user_id, metadata={"source": "telegram_direct"})
    return answer


def run_codex(prompt):
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = out_file.name
    try:
        cmd = [
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "--output-last-message",
            out_path,
            prompt,
        ]
        completed = subprocess.run(
            cmd,
            cwd=CODEX_WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=CODEX_TIMEOUT,
            check=False,
        )
        answer = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        if answer:
            return answer
        tail = (completed.stdout or "").strip()[-3000:]
        if completed.returncode == 0:
            return tail or "(Codex finished without text output)"
        return f"Codex failed with exit code {completed.returncode}.\n\n{tail}"
    except subprocess.TimeoutExpired:
        return f"Codex timed out after {CODEX_TIMEOUT} seconds."
    finally:
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass


def should_auto_remember(user_text, answer):
    combined = f"{user_text}\n{answer}".lower()
    keywords = [
        "已完成",
        "完成",
        "已更新",
        "已安裝",
        "部署",
        "systemd",
        ".service",
        "/opt/",
        "/etc/",
        "/var/lib/",
        "passwordauthentication",
        "authorized_keys",
        "vps_狀態",
        "fingerprint",
        "active",
        "health",
    ]
    return any(keyword.lower() in combined for keyword in keywords) and not memory_adapter.contains_secret(combined)


def remember_async(text, metadata):
    result = memory_queue.enqueue_memory(text, metadata=metadata, source=(metadata or {}).get("source") or "telegram")
    if result.get("ok"):
        kick_memory_writer()
    else:
        print(f"memory enqueue skipped/failed: {result}", flush=True)
    return result


def save_last_context(chat_id, user_id, recall_result):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chat_id": chat_id,
        "user_id": user_id,
        "recall_ok": recall_result.get("ok"),
        "recall_error": recall_result.get("error"),
        "recall_items": recall_result.get("items", [])[:5],
        "recent": _read_jsonl(RECENT_MESSAGES_FILE, 10),
    }
    tmp = LAST_CONTEXT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LAST_CONTEXT_FILE)


def format_memory_status():
    status = memory_adapter.memory_status()
    status.update(memory_queue.queue_status())
    status["facts_file"] = str(facts_store.FACTS_FILE)
    status["facts_events_file"] = str(facts_store.EVENTS_FILE)
    status["current_facts_count"] = len(facts_store.get_current_facts())
    safe = {key: value for key, value in status.items() if "token" not in key.lower() and "key" not in key.lower()}
    return json.dumps(safe, ensure_ascii=False, indent=2)


def parse_fact_assignment(text, command):
    payload = text[len(command) :].strip()
    if "=" not in payload:
        raise ValueError(f"用法：{command} <key> = <value>")
    key, value = payload.split("=", 1)
    return key.strip(), value.strip()


def _job_id_from_command(text, command):
    return text[len(command) :].strip().split()[0] if text[len(command) :].strip() else ""


def format_jobs():
    jobs = job_store.list_jobs(limit=10)
    if not jobs:
        return "(no jobs)"
    lines = []
    for job in jobs:
        lines.append(
            f"{job.get('title') or job_store.make_title(job.get('user_text'))}\n"
            f"  job_id：{job.get('job_id')}\n"
            f"  狀態：{job_store.status_label(job.get('status'))}｜更新：{job.get('updated_at')}"
        )
    return "\n\n".join(lines)


def launch_task(task):
    if task.get("is_observation") or task_store.active_work_count() < MAX_ACTIVE_WORK_TASKS:
        result = subprocess.run(
            ["/usr/bin/python3", "/opt/codex-telegram-gateway/task_starter.py", task["task_id"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            task_store.update_task(task["task_id"], status="failed", error=("task_starter failed: " + (result.stdout or ""))[-1000:])
            return False, result.stdout or ""
        return True, result.stdout or ""
    return False, "queued because max active work tasks reached"


def finalize_task_attachments(task_id, attachments):
    finalized = []
    target_dir = task_store.task_dir(task_id) / "attachments"
    target_dir.mkdir(parents=True, exist_ok=True)
    for index, attachment in enumerate(attachments or [], start=1):
        item = dict(attachment)
        source = Path(str(item.get("downloaded_path") or ""))
        safe_name = safe_attachment_filename(item.get("original_filename") or source.name or f"attachment_{index}.bin")
        destination = target_dir / f"{index:02d}_{safe_name}"
        if destination.exists():
            destination = target_dir / f"{index:02d}_{int(time.time())}_{safe_name}"
        if source.exists():
            os.replace(source, destination)
            os.chmod(destination, 0o600)
            item["path"] = str(destination)
            item["stored"] = True
        else:
            item["path"] = str(destination)
            item["stored"] = False
            item["error"] = "downloaded source file missing"
        item.pop("downloaded_path", None)
        finalized.append(item)
    task_store.write_task_json(task_id, "attachments.json", finalized)
    task_store.append_event(task_id, "attachments_saved", {"count": len(finalized)})
    return finalized


def format_attachment_summary(attachments):
    if not attachments:
        return ""
    lines = ["Telegram attachments saved for this task:"]
    for index, item in enumerate(attachments, start=1):
        size = item.get("file_size") or 0
        lines.append(
            f"{index}. filename={item.get('original_filename')} path={item.get('path')} "
            f"mime_type={item.get('mime_type')} size_bytes={size}"
        )
    return "\n".join(lines)


def create_and_launch_task_from_text(user_text, chat_id, user_id, batch_count=1, attachments=None):
    attachments = attachments or []
    if attachments and not str(user_text or "").strip():
        user_text = "收到 Telegram 附件，請按最近對話目的處理。"
    task = task_store.create_task(user_text, chat_id, user_id)
    final_attachments = finalize_task_attachments(task["task_id"], attachments) if attachments else []
    attachment_summary = format_attachment_summary(final_attachments)
    user_text_for_task = user_text
    if attachment_summary:
        user_text_for_task = f"{str(user_text or '').strip()}\n\n{attachment_summary}".strip()
        task = task_store.update_task(task["task_id"], user_text=user_text_for_task, title=task_store.make_title(user_text_for_task))
        task_store.append_thread_message(
            task["task_id"],
            "thread.jsonl",
            "user",
            user_text_for_task,
            {"source": "telegram_task_attachments"},
        )
    append_recent("user", user_text_for_task, chat_id, user_id, metadata={"source": "telegram_task", "task_id": task["task_id"]})
    launch_task(task)
    status = (task_store.get_task(task["task_id"]) or task).get("status")
    batch_note = f"\n合併段數：{batch_count}" if batch_count > 1 else ""
    attachment_note = f"\n附件數：{len(final_attachments)}" if final_attachments else ""
    send_message(
        chat_id,
        f"已建立 background Codex task：{task.get('title')}\n"
        f"task_id：{task.get('task_id')}\n"
        f"狀態：{status}{batch_note}{attachment_note}\n"
        f"你可以繼續對話；要查進度可直接問，或用 /task <task_id>。",
    )
    return task


def _pending_batch_key(chat_id, user_id):
    return f"{chat_id}:{user_id}"


def load_pending_message_batches():
    data = read_json(PENDING_MESSAGE_BATCHES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_pending_message_batches(batches):
    if batches:
        atomic_write_json(PENDING_MESSAGE_BATCHES_FILE, batches)
        return
    if PENDING_MESSAGE_BATCHES_FILE.exists():
        PENDING_MESSAGE_BATCHES_FILE.unlink()


def enqueue_user_task_message(safe_text, chat_id, user_id, attachments=None, delay_seconds=None):
    attachments = attachments or []
    safe_text = str(safe_text or "").strip()
    if not safe_text and not attachments:
        return
    now = time.time()
    delay = float(delay_seconds if delay_seconds is not None else MESSAGE_BATCH_DELAY_SECONDS)
    key = _pending_batch_key(chat_id, user_id)
    batches = load_pending_message_batches()
    batch = batches.get(key) or {
        "chat_id": chat_id,
        "user_id": user_id,
        "segments": [],
        "attachments": [],
        "created_at": now,
        "last_update_at": now,
        "deadline": now + delay,
    }
    batch.setdefault("segments", [])
    batch.setdefault("attachments", [])
    if safe_text:
        batch["segments"].append(safe_text)
    if attachments:
        batch["attachments"].extend(attachments)
    batch["last_update_at"] = now
    batch["deadline"] = now + delay
    batches[key] = batch
    save_pending_message_batches(batches)

def flush_due_pending_message_batches(force=False):
    now = time.time()
    batches = load_pending_message_batches()
    if not batches:
        return
    remaining = {}
    for key, batch in batches.items():
        deadline = float(batch.get("deadline") or 0)
        if not force and deadline > now:
            remaining[key] = batch
            continue
        segments = [str(item).strip() for item in batch.get("segments") or [] if str(item).strip()]
        attachments = [item for item in batch.get("attachments") or [] if isinstance(item, dict)]
        if not segments and not attachments:
            continue
        user_text = "\n\n".join(segments) if segments else "收到 Telegram 附件，請按最近對話目的處理。"
        create_and_launch_task_from_text(
            user_text,
            batch.get("chat_id"),
            batch.get("user_id"),
            batch_count=max(len(segments), 1),
            attachments=attachments,
        )
    save_pending_message_batches(remaining)


def next_pending_message_deadline():
    deadlines = []
    for batch in load_pending_message_batches().values():
        try:
            deadlines.append(float(batch.get("deadline")))
        except Exception:
            continue
    return min(deadlines) if deadlines else None


def format_tasks():
    tasks = task_store.list_tasks(limit=10)
    if not tasks:
        return "(no tasks)"
    lines = []
    for task in tasks:
        lines.append(
            f"{task.get('title')}\n"
            f"  task_id：{task.get('task_id')}\n"
            f"  狀態：{task.get('status')}｜更新：{task.get('updated_at')}"
        )
    return "\n\n".join(lines)


def _task_id_from_command(text, command):
    return text[len(command) :].strip().split()[0] if text[len(command) :].strip() else None


def finish_job_callback(job, final_report):
    append_recent(
        "assistant",
        memory_adapter.redact_secrets(final_report),
        job.get("chat_id"),
        job.get("user_id"),
        metadata={"source": "legacy_job", "job_id": job.get("job_id")},
    )


def handle_job_route(chat_id, route_result):
    job = router.resolve_job(route_result)
    if not job:
        send_message(chat_id, "找不到可控制的 job。")
        return "找不到可控制的 job。"
    job_id = job.get("job_id")
    intent = route_result.get("intent")
    phase = route_result.get("phase")
    if intent == "job_status":
        reply = job_store.natural_job_status(job)
        send_message(chat_id, reply)
        return reply
    if intent == "job_plan":
        reply = memory_adapter.redact_secrets(json.dumps(job_store.read_plan(job_id), ensure_ascii=False, indent=2))
        send_message(chat_id, reply)
        return reply
    if intent == "job_log":
        phase_id = phase or job.get("current_phase") or "_planner"
        reply = job_store.read_phase_text(job_id, phase_id, "stdout.log") or f"(no stdout.log for {phase_id})"
        send_message(chat_id, reply)
        return reply
    if intent == "job_result":
        if phase:
            result = job_store.read_phase_text(job_id, phase, "result.md") or f"(no result for {phase})"
        else:
            result = job_store.read_final_report(job_id) or "(no final report)"
        send_message(chat_id, result)
        return result
    if intent == "cancel_job":
        orchestrator.cancel_job(job_id)
        reply = f"已要求取消任務：{job.get('title') or job_store.make_title(job.get('user_text'))}\njob_id：{job_id}"
        send_message(chat_id, reply)
        return reply
    if intent == "rerun_phase":
        if not phase:
            plan = job_store.read_plan(job_id) or {}
            failed = []
            for item in plan.get("phases", []):
                phase_id = item.get("id")
                phase_state = job_store.get_phase(job_id, phase_id) or {}
                if phase_state.get("status") in {"failed", "cancelled"}:
                    failed.append(phase_id)
            if failed:
                phase = failed[0]
            else:
                reply = "找不到已中斷的 phase；請指定 explorer / auditor / runner / verifier / reporter。"
                send_message(chat_id, reply)
                return reply
        orchestrator.start_rerun_phase(job_id, phase, send_callback=send_message)
        reply = f"已開始重跑任務「{job.get('title') or job_store.make_title(job.get('user_text'))}」的 {job_store.phase_label(phase)}。\njob_id：{job_id}"
        send_message(chat_id, reply)
        return reply
    return None


def handle_message(message):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    document = message.get("document")
    photo = select_largest_telegram_photo(message.get("photo") or [])
    text = (message.get("text") or message.get("caption") or "").strip()

    if not chat_id or (not text and not document and not photo):
        return
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    if document or photo:
        safe_text = memory_adapter.redact_secrets(text)
        if memory_adapter.contains_secret(text):
            send_message(chat_id, "偵測到疑似 secret，已遮蔽後處理，且不會寫入長期記憶。")
        try:
            if document:
                attachment = download_telegram_document(message, document, chat_id, user_id)
                attachment_label = "文件"
            else:
                attachment = download_telegram_photo(message, photo, chat_id, user_id)
                attachment_label = "圖片"
        except Exception as exc:
            send_message(chat_id, f"附件接收失敗：{type(exc).__name__}: {exc}")
            return
        if not safe_text:
            safe_text = f"收到 Telegram {attachment_label}：{attachment.get('original_filename')}"
        enqueue_user_task_message(
            safe_text,
            chat_id,
            user_id,
            attachments=[attachment],
            delay_seconds=DOCUMENT_BATCH_DELAY_SECONDS,
        )
        send_message(
            chat_id,
            f"已收到{attachment_label}：{attachment.get('original_filename')}\n"
            f"會等約 {int(DOCUMENT_BATCH_DELAY_SECONDS)} 秒合併同一批附件，再建立 Codex task。",
        )
        return

    if text in {"/start", "/help"}:
        send_message(
            chat_id,
            "Codex Telegram Gateway online.\n\n"
            "普通訊息會建立 background Codex task，並帶入 facts/recent/cognee 記憶。\n"
            "支援 Telegram document/photo 附件，會下載到 task folder 後交給 Codex 使用。\n"
            "不使用 Orchestrator/子代理；/job-* 只保留用於查詢舊任務紀錄。\n"
            "Commands: /status, /ping, /whoami, /context, /remember <text>, /reset, /memory-status, "
            "/facts, /fact <key>, /setfact <key> = <value>, /setfact-verified <key> = <value>, "
            "/archivefact <key>, /fact-events <key>, /tasks, /task <id>, /task-log <id>, /task-result <id>, /cancel-task <id>, "
            "/jobs, /job <id>, /job-plan <id>, "
            "/job-log <id> [phase], /job-result <id> [phase], /cancel-job <id>, /rerun-phase <id> <phase>, /approve <id> <phase>",
        )
        return
    if text == "/ping":
        send_message(chat_id, "pong")
        return
    if text == "/whoami":
        send_message(chat_id, f"chat_id={chat_id}\nuser_id={user_id}")
        return
    if text == "/status":
        status = subprocess.run(
            [CODEX_BIN, "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        send_message(chat_id, status.stdout.strip() or f"status exit={status.returncode}")
        return
    if text == "/memory-status":
        send_message(chat_id, format_memory_status())
        return
    if text == "/tasks":
        send_message(chat_id, format_tasks())
        return
    if text.startswith("/task-result"):
        task_id = _task_id_from_command(text, "/task-result")
        if not task_id:
            send_message(chat_id, "用法：/task-result <task_id>")
            return
        send_message(chat_id, task_store.read_task_text(task_id, "result.md") or "(no task result)")
        return
    if text.startswith("/task-log"):
        task_id = _task_id_from_command(text, "/task-log")
        if not task_id:
            send_message(chat_id, "用法：/task-log <task_id>")
            return
        send_message(chat_id, task_store.read_task_text(task_id, "stdout.log") or "(no task stdout.log)")
        return
    if text.startswith("/cancel-task"):
        task_id = _task_id_from_command(text, "/cancel-task")
        if not task_id:
            send_message(chat_id, "用法：/cancel-task <task_id>")
            return
        task = task_store.get_task(task_id)
        if not task:
            send_message(chat_id, "找不到這個 task。")
            return
        task_store.update_task(task_id, status="cancelled", error="cancelled by user")
        unit = task.get("unit_name")
        if unit:
            subprocess.run(["systemctl", "stop", f"{unit}.service"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        send_message(chat_id, f"已要求取消 task。\ntask_id：{task_id}")
        return
    if text.startswith("/task "):
        task_id = _task_id_from_command(text, "/task")
        send_message(chat_id, task_store.natural_task_status(task_store.get_task(task_id)))
        return
    if text == "/jobs":
        send_message(chat_id, format_jobs())
        return
    if text.startswith("/job-plan"):
        job_id = _job_id_from_command(text, "/job-plan")
        if not job_id:
            send_message(chat_id, "用法：/job-plan <job_id>")
            return
        send_message(chat_id, memory_adapter.redact_secrets(json.dumps(job_store.read_plan(job_id), ensure_ascii=False, indent=2)))
        return
    if text.startswith("/job-log"):
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "用法：/job-log <job_id> [phase]")
            return
        job_id = parts[1]
        phase_id = parts[2] if len(parts) >= 3 else "_planner"
        log = job_store.read_phase_text(job_id, phase_id, "stdout.log") or "(no stdout.log)"
        send_message(chat_id, log)
        return
    if text.startswith("/job-result"):
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "用法：/job-result <job_id> [phase]")
            return
        job_id = parts[1]
        if len(parts) >= 3:
            result = job_store.read_phase_text(job_id, parts[2], "result.md") or "(no phase result)"
        else:
            result = job_store.read_final_report(job_id) or job_store.fallback_report(job_id) or "(no final report)"
        send_message(chat_id, result)
        return
    if text.startswith("/cancel-job"):
        job_id = _job_id_from_command(text, "/cancel-job")
        if not job_id:
            send_message(chat_id, "用法：/cancel-job <job_id>")
            return
        orchestrator.cancel_job(job_id)
        send_message(chat_id, f"已要求取消 job {job_id}。running worker 會在下一次檢查點停止。")
        return
    if text.startswith("/rerun-phase"):
        parts = text.split()
        if len(parts) < 3:
            send_message(chat_id, "用法：/rerun-phase <job_id> <phase>")
            return
        orchestrator.start_rerun_phase(parts[1], parts[2], send_callback=send_message)
        send_message(chat_id, f"已開始重跑 job {parts[1]} phase {parts[2]}。")
        return
    if text.startswith("/approve"):
        send_message(chat_id, "目前是授權自動模式，沒有 pending approval gate。")
        return
    if text.startswith("/job "):
        job_id = _job_id_from_command(text, "/job")
        send_message(chat_id, memory_adapter.redact_secrets(job_store.natural_job_status(job_store.get_job(job_id))))
        return
    if text == "/facts":
        send_message(chat_id, memory_adapter.redact_secrets(facts_store.format_facts()))
        return
    if text.startswith("/fact-events"):
        key = text[len("/fact-events") :].strip()
        if not key:
            send_message(chat_id, "用法：/fact-events <key>")
            return
        events = facts_store.fact_events(key=key, limit=10)
        send_message(chat_id, memory_adapter.redact_secrets(json.dumps(events, ensure_ascii=False, indent=2)))
        return
    if text.startswith("/fact "):
        key = text[len("/fact") :].strip()
        fact = facts_store.get_fact(key)
        send_message(chat_id, memory_adapter.redact_secrets(facts_store.format_fact(fact)))
        return
    if text.startswith("/setfact-verified"):
        try:
            key, value = parse_fact_assignment(text, "/setfact-verified")
            fact = facts_store.set_fact(
                key,
                value,
                source="verified_vps_command",
                confidence="verified",
                note=f"set by telegram user {user_id}",
            )
            send_message(chat_id, "已寫入 verified current fact：\n" + facts_store.format_fact(fact))
        except Exception as exc:
            send_message(chat_id, f"未能寫入 fact：{exc}")
        return
    if text.startswith("/setfact"):
        try:
            key, value = parse_fact_assignment(text, "/setfact")
            fact = facts_store.set_fact(
                key,
                value,
                source="telegram_manual",
                confidence="declared",
                note=f"set by telegram user {user_id}",
            )
            send_message(chat_id, "已寫入 declared current fact：\n" + facts_store.format_fact(fact))
        except Exception as exc:
            send_message(chat_id, f"未能寫入 fact：{exc}")
        return
    if text.startswith("/archivefact"):
        key = text[len("/archivefact") :].strip()
        if not key:
            send_message(chat_id, "用法：/archivefact <key>")
            return
        try:
            fact = facts_store.archive_fact(key, source="telegram_manual", note=f"archived by telegram user {user_id}")
            send_message(chat_id, "已封存 fact：\n" + facts_store.format_fact(fact))
        except Exception as exc:
            send_message(chat_id, f"未能封存 fact：{exc}")
        return
    if text == "/context":
        if LAST_CONTEXT_FILE.exists():
            context = LAST_CONTEXT_FILE.read_text(encoding="utf-8", errors="replace")
        else:
            context = json.dumps({"recent": _read_jsonl(RECENT_MESSAGES_FILE, 10)}, ensure_ascii=False, indent=2)
        send_message(chat_id, memory_adapter.redact_secrets(context))
        return
    if text == "/reset":
        clear_recent_memory()
        send_message(chat_id, "短期 Telegram 記憶已清空；cognee 長期記憶未清除。")
        return
    if text.startswith("/remember"):
        fact = text[len("/remember") :].strip()
        if not fact:
            send_message(chat_id, "用法：/remember <要寫入長期記憶的內容>")
            return
        result = remember_async(
            fact,
            {"source": "telegram_manual", "chat_id": chat_id, "user_id": user_id},
        )
        if result.get("ok"):
            send_message(chat_id, f"已加入長期記憶寫入隊列。\nmemory_id：{result.get('memory_id')}")
        else:
            send_message(chat_id, f"未加入長期記憶隊列：{result.get('error') or result}")
        return

    safe_text = memory_adapter.redact_secrets(text)
    if memory_adapter.contains_secret(text):
        send_message(chat_id, "偵測到疑似 secret，已遮蔽後處理，且不會寫入長期記憶。")

    enqueue_user_task_message(safe_text, chat_id, user_id)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/python3", "/opt/codex-telegram-gateway/task_starter.py"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    api_call("deleteWebhook", {"drop_pending_updates": "false"}, timeout=20)
    me = api_call("getMe", timeout=20)
    print(f"Codex Telegram Gateway started as @{me.get('username')}", flush=True)

    offset = read_offset()
    polling_timeout_count = 0
    while not STOP:
        try:
            flush_due_pending_message_batches()
            poll_timeout = 30
            deadline = next_pending_message_deadline()
            if deadline is not None:
                poll_timeout = max(1, min(30, int(deadline - time.time()) + 1))
            params = {"timeout": poll_timeout, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            updates = api_call("getUpdates", params, timeout=poll_timeout + 10)
            polling_timeout_count = 0
            for update in updates:
                update_id = update["update_id"]
                offset = update_id + 1
                write_offset(offset)
                if "message" in update:
                    handle_message(update["message"])
            flush_due_pending_message_batches()
        except Exception as exc:
            if is_polling_timeout(exc):
                polling_timeout_count += 1
                if polling_timeout_count == 1 or polling_timeout_count % 20 == 0:
                    print(f"gateway polling timeout: retrying count={polling_timeout_count}", flush=True)
                flush_due_pending_message_batches()
                time.sleep(1)
                continue
            print(f"gateway error: {exc}", flush=True)
            time.sleep(5)

    flush_due_pending_message_batches(force=True)
    print("Codex Telegram Gateway stopped", flush=True)


if __name__ == "__main__":
    main()
