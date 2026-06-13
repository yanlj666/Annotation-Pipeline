import json
from typing import Any

import httpx


ERROR_TYPES = {
    "timeout",
    "rate_limited",
    "server_error",
    "network_error",
    "invalid_json",
    "schema_error",
    "data_error",
    "unknown",
}


def classify_error(error: BaseException | str) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code == 429:
            return "rate_limited"
        if 500 <= code <= 599:
            return "server_error"
        if 400 <= code <= 499:
            return "data_error"
    if isinstance(error, httpx.RequestError):
        return "network_error"
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    text = str(error).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    if "500" in text or "502" in text or "503" in text or "504" in text:
        return "server_error"
    if "json" in text:
        return "invalid_json"
    if (
        "missing field" in text
        or "unexpected fields" in text
        or "must be string" in text
        or "must be boolean" in text
        or "must be integer" in text
        or "must be number" in text
        or "must be array" in text
        or "must be object" in text
        or "annotation must be" in text
    ):
        return "schema_error"
    if "empty conversation" in text or "empty conversation id" in text or "unsupported source format" in text:
        return "data_error"
    if "connect" in text or "network" in text or "connection" in text or "dns" in text:
        return "network_error"
    return "unknown"


def error_summary(tasks: list[dict[str, Any]], limit: int = 5) -> dict[str, Any]:
    counts = {kind: 0 for kind in sorted(ERROR_TYPES)}
    recent = []
    for task in tasks:
        error = task.get("error") or ""
        kind = classify_error(error)
        counts[kind] = counts.get(kind, 0) + 1
        if len(recent) < limit:
            recent.append(
                {
                    "task_id": task.get("task_id"),
                    "error_type": kind,
                    "error": error,
                    "updated_at": task.get("updated_at"),
                }
            )
    return {
        "total": len(tasks),
        "counts": {key: value for key, value in counts.items() if value},
        "recent": recent,
    }
