import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .store import Store


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_TURN_CHARS = 12000


def ingest_file(input_path: str, mapping: dict[str, Any], store: Store) -> dict[str, int]:
    store.init()
    cfg = mapping["import_mapping"]
    source_format = cfg.get("source_format") or Path(input_path).suffix.lstrip(".")
    rows = _read_rows(input_path, source_format)
    created = skipped = invalid = 0
    skipped_by_status: dict[str, int] = {}
    for row in rows:
        try:
            task = normalize_row(row, cfg)
        except ValueError:
            invalid += 1
            continue
        if store.upsert_task(task["task_id"], task["turns"], task["payload"]):
            created += 1
        else:
            skipped += 1
            statuses = store.status_counts_for([task["task_id"]])
            status = next((key for key, value in statuses.items() if value), "unknown")
            skipped_by_status[status] = skipped_by_status.get(status, 0) + 1
    return {
        "created": created,
        "skipped_existing": skipped,
        "invalid": invalid,
        "skipped_by_status": skipped_by_status,
    }


def normalize_row(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    fields = cfg["fields"]
    conversation_id = _clean(str(row.get(fields["conversation_id"], "")).strip())
    if not conversation_id:
        raise ValueError("empty conversation id")
    turns_value = row.get(fields["turns"], "")
    all_turns = parse_turns(turns_value)
    if not all_turns:
        raise ValueError("empty conversation")
    payload: dict[str, Any] = {}
    for name in cfg.get("passthrough", []):
        if name in row:
            payload[name] = row[name]
    payload.setdefault("conversation_id", conversation_id)
    payload.setdefault("session_id", derive_session_id(conversation_id))
    turn_mode = str(cfg.get("turn_mode", "single")).strip().lower()
    if turn_mode not in {"single", "conversation"}:
        raise ValueError(f"unsupported turn_mode: {turn_mode}")
    context_turns, turns = split_current_turn(all_turns)
    if turn_mode == "conversation" and context_turns:
        payload["context_turns"] = context_turns
    return {"task_id": make_task_id(conversation_id), "turns": turns, "payload": payload, "status": "pending"}


def make_task_id(conversation_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", conversation_id).strip("_")
    if safe:
        return safe[:160]
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()


def derive_session_id(conversation_id: str) -> str:
    match = re.match(r"^(?P<session>.+)_\d+$", conversation_id)
    return match.group("session") if match else conversation_id


def split_current_turn(turns: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not turns:
        return [], []
    start = len(turns) - 1
    for index in range(len(turns) - 1, -1, -1):
        if turns[index].get("role") == "user":
            start = index
            break
    return turns[:start], turns[start:]


def parse_turns(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        raw_turns = value
    else:
        text = _clean(str(value)).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            raw_turns = parsed if isinstance(parsed, list) else [{"role": "user", "content": text}]
        except json.JSONDecodeError:
            repaired = _try_parse_smart_quote_json(text)
            raw_turns = repaired if repaired is not None else _parse_plain_text(text)
    turns: list[dict[str, str]] = []
    for turn in raw_turns:
        role = str(turn.get("role", "user")).lower() if isinstance(turn, dict) else "user"
        role = role if role in {"user", "assistant"} else "user"
        content = _clean(str(turn.get("content", "") if isinstance(turn, dict) else turn)).strip()
        if content:
            turns.append({"role": role, "content": content[:MAX_TURN_CHARS]})
    return turns


def _parse_plain_text(text: str) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        role, content = _split_role_prefix(stripped)
        turns.append({"role": role or "user", "content": content})
    return turns


def _try_parse_smart_quote_json(text: str) -> list[Any] | None:
    repaired = text.translate(str.maketrans({
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
    }))
    if repaired == text:
        return None
    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else [{"role": "user", "content": text}]


def _split_role_prefix(stripped: str) -> tuple[str | None, str]:
    prefixes = (
        ("user:", "user"),
        ("用户:", "user"),
        ("用户：", "user"),
        ("assistant:", "assistant"),
        ("assistant：", "assistant"),
        ("客服:", "assistant"),
        ("客服：", "assistant"),
        ("助手:", "assistant"),
        ("助手：", "assistant"),
    )
    lowered = stripped.lower()
    for prefix, role in prefixes:
        if lowered.startswith(prefix.lower()):
            return role, stripped[len(prefix):].strip()
    return None, stripped


def _read_rows(input_path: str, source_format: str) -> Iterable[dict[str, Any]]:
    if source_format == "csv":
        with open(input_path, newline="", encoding="utf-8-sig") as fh:
            yield from csv.DictReader(fh)
    elif source_format == "jsonl":
        with open(input_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)
    else:
        raise ValueError(f"unsupported source format: {source_format}")


def _clean(text: str) -> str:
    return CONTROL_CHARS.sub("", text)
