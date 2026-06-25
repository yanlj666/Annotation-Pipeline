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
    _validate_mapping(cfg)
    source_format = cfg.get("source_format") or Path(input_path).suffix.lstrip(".")
    rows = list(_read_rows(input_path, source_format))
    tasks = build_tasks(rows, cfg)
    created = skipped = invalid = 0
    skipped_by_status: dict[str, int] = {}
    for task in tasks:
        if task is None:
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


def build_tasks(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any] | None]:
    mode = task_mode(cfg)
    if mode == "turn_only":
        return [_safe_normalize_row(row, cfg, None, mode) for row in rows]

    grouped: dict[str, list[dict[str, Any]]] = {}
    order = 0
    for row in rows:
        row["_ap_input_order"] = order
        order += 1
        fields = cfg["fields"]
        session_id = _field_value(row, fields["session_id"]).strip()
        grouped.setdefault(session_id, []).append(row)

    tasks: list[dict[str, Any] | None] = []
    for session_id in sorted(grouped):
        session_rows = sorted(grouped[session_id], key=lambda row: (_field_value(row, cfg["fields"]["exchange_time"]), row["_ap_input_order"]))
        exchanges: list[dict[str, Any]] = []
        for row in session_rows:
            try:
                exchanges.append(normalize_exchange(row, cfg))
            except ValueError:
                tasks.append(None)
        if mode == "session":
            if not exchanges:
                continue
            tasks.append(build_session_task(session_id, exchanges, cfg))
            continue
        if mode == "turn_with_context" and reference_field_enabled(cfg, "next_user_query"):
            attach_next_user_queries(exchanges)
        context: list[dict[str, str]] = []
        for exchange in exchanges:
            task = build_turn_task(exchange, cfg, context if mode == "turn_with_context" else [])
            tasks.append(task)
            context.extend(exchange["turns"])
    return tasks


def _safe_normalize_row(row: dict[str, Any], cfg: dict[str, Any], context: list[dict[str, str]] | None, mode: str) -> dict[str, Any] | None:
    try:
        exchange = normalize_exchange(row, cfg)
        return build_turn_task(exchange, cfg, context or [])
    except ValueError:
        return None


def normalize_row(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    _validate_mapping(cfg)
    mode = task_mode(cfg)
    if mode == "session":
        exchange = normalize_exchange(row, cfg)
        return build_session_task(exchange["session_id"], [exchange], cfg)
    exchange = normalize_exchange(row, cfg)
    return build_turn_task(exchange, cfg, [])


def normalize_exchange(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    fields = cfg["fields"]
    exchange_id = _field_value(row, fields["exchange_id"]).strip()
    session_id = _field_value(row, fields["session_id"]).strip()
    exchange_time = _field_value(row, fields["exchange_time"]).strip()
    if not exchange_id:
        raise ValueError("empty exchange id")
    if not session_id:
        raise ValueError("empty session id")
    if not exchange_time:
        raise ValueError("empty exchange time")
    turns_value = row.get(fields["turns"], "")
    all_turns = parse_turns(turns_value)
    if not all_turns:
        raise ValueError("empty exchange")
    payload: dict[str, Any] = {}
    for name in cfg.get("passthrough", []):
        if name in row:
            payload[name] = row[name]
    payload["session_id"] = session_id
    payload["exchange_id"] = exchange_id
    payload["exchange_time"] = exchange_time
    return {
        "task_id": make_task_id(exchange_id),
        "exchange_id": exchange_id,
        "session_id": session_id,
        "exchange_time": exchange_time,
        "turns": all_turns,
        "payload": payload,
        "status": "pending",
    }


def build_turn_task(exchange: dict[str, Any], cfg: dict[str, Any], context_turns: list[dict[str, str]]) -> dict[str, Any]:
    payload = dict(exchange["payload"])
    if context_turns:
        payload["context_turns"] = list(context_turns)
    return {"task_id": exchange["task_id"], "turns": exchange["turns"], "payload": payload, "status": "pending"}


def build_session_task(session_id: str, exchanges: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    turns: list[dict[str, str]] = []
    exchange_ids = []
    for exchange in exchanges:
        exchange_ids.append(exchange["exchange_id"])
        turns.extend(exchange["turns"])
    payload: dict[str, Any] = {"session_id": session_id, "exchange_ids": exchange_ids}
    for name in cfg.get("passthrough", []):
        values = [exchange["payload"].get(name) for exchange in exchanges if name in exchange["payload"]]
        if values and all(value == values[0] for value in values):
            payload[name] = values[0]
    return {"task_id": make_task_id(session_id), "turns": turns, "payload": payload, "status": "pending"}


def reference_field_enabled(cfg: dict[str, Any], name: str) -> bool:
    fields = cfg.get("reference_fields", {})
    if isinstance(fields, dict):
        return bool(fields.get(name, False))
    return False


def attach_next_user_queries(exchanges: list[dict[str, Any]]) -> None:
    for index, exchange in enumerate(exchanges):
        next_query = ""
        if index + 1 < len(exchanges):
            next_query = first_user_content(exchanges[index + 1].get("turns", []))
        exchange["payload"]["next_user_query"] = next_query


def first_user_content(turns: list[dict[str, str]]) -> str:
    for turn in turns:
        if turn.get("role") == "user":
            return _clean(str(turn.get("content", ""))).strip()
    return ""


def make_task_id(source_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id).strip("_")
    if safe:
        return safe[:160]
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def task_mode(cfg: dict[str, Any]) -> str:
    mode = str(cfg.get("task_mode", "turn_with_context")).strip().lower()
    if mode not in {"turn_with_context", "turn_only", "session"}:
        raise ValueError(f"unsupported task_mode: {mode}")
    return mode


def _validate_mapping(cfg: dict[str, Any]) -> None:
    if "turn_mode" in cfg:
        raise ValueError(
            "import_mapping.turn_mode has been replaced by import_mapping.task_mode. "
            "Use task_mode: turn_only, turn_with_context, or session."
        )
    fields = cfg.get("fields", {})
    missing = [key for key in ("session_id", "exchange_id", "exchange_time", "turns") if not fields.get(key)]
    if missing:
        raise ValueError(f"missing import_mapping.fields: {', '.join(missing)}")


def _field_value(row: dict[str, Any], field_name: str) -> str:
    return _clean(str(row.get(field_name, "")).strip())


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
