#!/usr/bin/env python3
import json
import os
import re
import time
from pathlib import Path

import memory_adapter


STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/codex-telegram-gateway"))
MEMORY_DIR = STATE_DIR / "memory"
FACTS_FILE = MEMORY_DIR / "facts.json"
EVENTS_FILE = MEMORY_DIR / "facts_events.jsonl"

ALLOWED_CONFIDENCE = {"verified", "declared", "hypothesis"}
ALLOWED_SOURCE = {"telegram_manual", "verified_vps_command", "system_migration", "smoke_test"}

DEFAULT_KEYS = [
    "trading_system.current_entrypoint",
    "trading_system.active_strategy",
    "trading_system.production_service",
    "trading_system.live_data_source",
    "trading_system.risk_limits",
    "trading_system.capital_mode",
    "trading_system.kill_switch_status",
    "codex_gateway.program_path",
    "codex_gateway.memory_adapter_path",
    "openclaw.gateway_service",
    "openclaw.data_dir",
    "openclaw.workspace_dir",
]

KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,120}$")


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_key(key):
    key = (key or "").strip()
    if not KEY_PATTERN.match(key):
        raise ValueError("fact key must be 3-120 chars and contain only letters, numbers, dot, underscore, or hyphen")
    return key


def validate_value(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("fact value cannot be empty")
    if memory_adapter.contains_secret(value):
        raise ValueError("secret-like content cannot be written to facts")
    return memory_adapter.redact_secrets(value)


def normalize_source(source):
    source = (source or "telegram_manual").strip()
    return source if source in ALLOWED_SOURCE else "telegram_manual"


def normalize_confidence(confidence):
    confidence = (confidence or "declared").strip()
    return confidence if confidence in ALLOWED_CONFIDENCE else "declared"


def _ensure_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _read_facts_all():
    if not FACTS_FILE.exists():
        return {}
    try:
        data = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_facts_all(data):
    _ensure_dirs()
    tmp = FACTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, FACTS_FILE)
    os.chmod(FACTS_FILE, 0o600)


def _append_event(event):
    _ensure_dirs()
    with EVENTS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(EVENTS_FILE, 0o600)


def set_fact(key, value, source="telegram_manual", confidence="declared", note=None):
    key = validate_key(key)
    value = validate_value(value)
    source = normalize_source(source)
    confidence = normalize_confidence(confidence)
    now = utc_now()
    facts = _read_facts_all()
    previous = facts.get(key)
    version = int(previous.get("version", 0)) + 1 if isinstance(previous, dict) else 1
    fact = {
        "key": key,
        "value": value,
        "version": version,
        "status": "current",
        "updated_at": now,
        "source": source,
        "confidence": confidence,
    }
    if note:
        fact["note"] = memory_adapter.redact_secrets(str(note))[:1000]
    if previous:
        fact["supersedes_version"] = previous.get("version")
        fact["previous_updated_at"] = previous.get("updated_at")
    facts[key] = fact
    _write_facts_all(facts)
    event = {
        "event": "set_fact",
        "key": key,
        "version": version,
        "event_at": now,
        "old_value": previous.get("value") if isinstance(previous, dict) else None,
        "new_value": value,
        "source": source,
        "confidence": confidence,
        "note": fact.get("note"),
    }
    _append_event(event)
    return fact


def archive_fact(key, source="telegram_manual", note=None):
    key = validate_key(key)
    now = utc_now()
    facts = _read_facts_all()
    previous = facts.get(key)
    if not previous:
        raise KeyError(f"fact not found: {key}")
    version = int(previous.get("version", 0)) + 1
    archived = dict(previous)
    archived.update(
        {
            "version": version,
            "status": "archived",
            "updated_at": now,
            "source": normalize_source(source),
        }
    )
    if note:
        archived["note"] = memory_adapter.redact_secrets(str(note))[:1000]
    facts[key] = archived
    _write_facts_all(facts)
    _append_event(
        {
            "event": "archive_fact",
            "key": key,
            "version": version,
            "event_at": now,
            "old_value": previous.get("value"),
            "new_value": previous.get("value"),
            "source": archived["source"],
            "confidence": archived.get("confidence"),
            "note": archived.get("note"),
        }
    )
    return archived


def get_fact(key):
    return _read_facts_all().get(validate_key(key))


def get_current_facts():
    return {key: value for key, value in _read_facts_all().items() if isinstance(value, dict) and value.get("status") == "current"}


def list_facts(prefix=None):
    facts = get_current_facts()
    if prefix:
        facts = {key: value for key, value in facts.items() if key.startswith(prefix)}
    return dict(sorted(facts.items()))


def fact_events(key=None, limit=20):
    if not EVENTS_FILE.exists():
        return []
    events = []
    for line in EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if key and item.get("key") != key:
            continue
        events.append(item)
    return events[-limit:]


def format_facts_for_prompt(limit=80):
    facts = list_facts()
    if not facts:
        return "(no authoritative current facts)"
    lines = []
    for key, fact in list(facts.items())[:limit]:
        lines.append(
            f"- {key}: {fact.get('value')} "
            f"(version={fact.get('version')}, updated_at={fact.get('updated_at')}, "
            f"source={fact.get('source')}, confidence={fact.get('confidence')})"
        )
    return "\n".join(lines)


def format_fact(fact):
    if not fact:
        return "fact not found"
    return json.dumps(fact, ensure_ascii=False, indent=2, sort_keys=True)


def format_facts(prefix=None):
    facts = list_facts(prefix=prefix)
    if not facts:
        return "(no current facts)"
    return "\n\n".join(format_fact(fact) for fact in facts.values())
