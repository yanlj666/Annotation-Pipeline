import asyncio
import json
import time
from typing import Any

from .llm_client import LLMClient
from .store import Store


class RateLimiter:
    def __init__(self, interval_ms: int, rate_limit_per_min: int):
        self.interval = max(0, interval_ms) / 1000
        self.rate_limit_per_min = max(1, rate_limit_per_min)
        self._lock = asyncio.Lock()
        self._last_at = 0.0
        self._window: list[float] = []

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._window = [t for t in self._window if now - t < 60]
            if len(self._window) >= self.rate_limit_per_min:
                await asyncio.sleep(60 - (now - self._window[0]))
            since_last = time.monotonic() - self._last_at
            if since_last < self.interval:
                await asyncio.sleep(self.interval - since_last)
            self._last_at = time.monotonic()
            self._window.append(self._last_at)


async def run_labeling(config: dict[str, Any], task_config: dict[str, Any], store: Store) -> dict[str, int]:
    store.init()
    engine_cfg = config.get("engine", {})
    sample_size = engine_cfg.get("sample_size")
    tasks = store.list_tasks("pending", sample_size)
    client = LLMClient(config)
    limiter = RateLimiter(engine_cfg.get("interval_ms", 0), engine_cfg.get("rate_limit_per_min", 60))
    sem = asyncio.Semaphore(max(1, int(engine_cfg.get("concurrency", 1))))
    max_retries = max(0, int(engine_cfg.get("max_retries", 0)))
    counts = {"labeled": 0, "failed": 0, "skipped": 0}

    async def one(task: dict[str, Any]) -> None:
        async with sem:
            if task["status"] != "pending":
                counts["skipped"] += 1
                return
            error = ""
            for attempt in range(max_retries + 1):
                try:
                    await limiter.wait()
                    annotation = await asyncio.to_thread(_label_once, client, task_config, task)
                    validate_output(annotation, task_config["output_schema"])
                    store.mark_labeled(task["task_id"], annotation)
                    counts["labeled"] += 1
                    return
                except Exception as exc:  # keep failures isolated per task
                    error = f"attempt {attempt + 1}: {exc}"
                    await asyncio.sleep(min(2 ** attempt, 8))
            store.mark_failed(task["task_id"], error)
            counts["failed"] += 1

    await asyncio.gather(*(one(task) for task in tasks))
    return counts


def _label_once(client: LLMClient, task_config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    prompt = task_config["prompt"]
    turns = json.dumps(task["turns"], ensure_ascii=False, indent=2)
    payload = json.dumps(task["payload"], ensure_ascii=False, indent=2)
    schema = json.dumps(task_config["output_schema"], ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": prompt["user"].format(turns=turns, payload=payload, schema=schema)},
    ]
    return client.complete_json(messages, task_config["output_schema"])


def validate_output(annotation: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(annotation, dict):
        raise ValueError("annotation must be a JSON object")
    extra = set(annotation) - set(schema)
    if extra:
        raise ValueError(f"unexpected fields: {', '.join(sorted(extra))}")
    for key, spec in schema.items():
        if key not in annotation:
            raise ValueError(f"missing field: {key}")
        typ = spec.get("type", "string")
        value = annotation[key]
        if typ == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be string")
        if typ == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean")
        if typ == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
            raise ValueError(f"{key} must be integer")
        if typ == "number" and not (isinstance(value, int | float) and not isinstance(value, bool)):
            raise ValueError(f"{key} must be number")
        if typ == "array" and not isinstance(value, list):
            raise ValueError(f"{key} must be array")
        if typ == "object" and not isinstance(value, dict):
            raise ValueError(f"{key} must be object")
