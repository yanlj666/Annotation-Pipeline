import asyncio
import json
import time
from typing import Any

from .llm_client import LLMClient
from .store import Store


class RateLimiter:
    def __init__(self, interval_ms: int, rate_limit_per_min: int, burst: int = 1):
        self.interval = max(0, interval_ms) / 1000
        self.rate_limit_per_min = max(1, rate_limit_per_min)
        self.capacity = max(1, burst)
        self._tokens = float(self.capacity)
        self._refill_per_second = self.rate_limit_per_min / 60
        self._lock = asyncio.Lock()
        self._last_at = 0.0
        self._last_refill_at = time.monotonic()

    async def wait(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill_at)
                self._tokens = min(self.capacity, self._tokens + elapsed * self._refill_per_second)
                self._last_refill_at = now

                token_delay = 0.0 if self._tokens >= 1 else (1 - self._tokens) / self._refill_per_second
                interval_delay = max(0.0, self.interval - (now - self._last_at)) if self._last_at else 0.0
                delay = max(token_delay, interval_delay)
                if delay <= 0:
                    self._tokens -= 1
                    self._last_at = now
                    return
            await asyncio.sleep(delay)


async def run_labeling(config: dict[str, Any], task_config: dict[str, Any], store: Store) -> dict[str, int]:
    store.init()
    engine_cfg = config.get("engine", {})
    sample_size = engine_cfg.get("sample_size")
    tasks = store.list_tasks("pending", sample_size)
    client = LLMClient(config)
    concurrency = max(1, int(engine_cfg.get("concurrency", 1)))
    limiter = RateLimiter(
        engine_cfg.get("interval_ms", 0),
        engine_cfg.get("rate_limit_per_min", 60),
        int(engine_cfg.get("burst", concurrency)),
    )
    sem = asyncio.Semaphore(concurrency)
    max_retries = max(0, int(engine_cfg.get("max_retries", 0)))
    progress = bool(engine_cfg.get("progress", True))
    counts = {"labeled": 0, "failed": 0, "skipped": 0}
    total = len(tasks)
    counter = 0
    counter_lock = asyncio.Lock()

    def log(message: str) -> None:
        if progress:
            print(f"[label] {message}", flush=True)

    log(
        "start "
        f"total={total} concurrency={concurrency} "
        f"rate_limit_per_min={limiter.rate_limit_per_min} burst={limiter.capacity} timeout={client.timeout}s"
    )

    async def one(task: dict[str, Any]) -> None:
        nonlocal counter
        async with sem:
            async with counter_lock:
                counter += 1
                index = counter
            task_id = task["task_id"]
            started = time.monotonic()
            log(f"start {index}/{total} task_id={task_id}")
            if task["status"] != "pending":
                counts["skipped"] += 1
                log(f"skip {index}/{total} task_id={task_id} status={task['status']}")
                return
            error = ""
            for attempt in range(max_retries + 1):
                try:
                    await limiter.wait()
                    annotation = await _label_once(client, task_config, task)
                    validate_output(annotation, task_config["output_schema"])
                    store.mark_labeled(task_id, annotation)
                    counts["labeled"] += 1
                    log(
                        f"done {index}/{total} task_id={task_id} "
                        f"elapsed={time.monotonic() - started:.2f}s counts={counts}"
                    )
                    return
                except Exception as exc:  # keep failures isolated per task
                    error = f"attempt {attempt + 1}: {exc}"
                    log(f"retry {index}/{total} task_id={task_id} {error}")
                    if attempt < max_retries:
                        await asyncio.sleep(min(2 ** attempt, 8))
            store.mark_failed(task_id, error)
            counts["failed"] += 1
            log(f"failed {index}/{total} task_id={task_id} elapsed={time.monotonic() - started:.2f}s error={error}")

    await asyncio.gather(*(one(task) for task in tasks))
    log(f"finished counts={counts}")
    return counts


async def _label_once(client: LLMClient, task_config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    prompt = task_config["prompt"]
    turns = json.dumps(task["turns"], ensure_ascii=False, indent=2)
    payload = json.dumps(task["payload"], ensure_ascii=False, indent=2)
    schema = json.dumps(task_config["output_schema"], ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": render_prompt_template(prompt["user"], turns=turns, payload=payload, schema=schema)},
    ]
    return await client.complete_json_async(messages, task_config["output_schema"])


def render_prompt_template(template: str, **values: str) -> str:
    """Replace only supported prompt placeholders, leaving JSON braces intact."""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


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
