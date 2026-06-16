import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import classify_error
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


class LabelRunLogger:
    def __init__(self, log_dir: str | Path):
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = directory / f"label_{stamp}.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")
        self._lock = asyncio.Lock()

    async def write(self, event: str, **fields: Any) -> None:
        item = {"time": _utc_now_ms(), "event": event, **fields}
        async with self._lock:
            self._fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


async def run_labeling(
    config: dict[str, Any],
    task_config: dict[str, Any],
    store: Store,
    statuses: list[str] | tuple[str, ...] | str = "pending",
    strict: bool = False,
) -> dict[str, Any]:
    store.init()
    engine_cfg = config.get("engine", {})
    sample_size = engine_cfg.get("sample_size")
    selected_statuses = _parse_statuses(statuses)
    tasks = store.list_tasks(selected_statuses, sample_size)
    client = LLMClient(config)
    mock_reason = client.mock_reason()
    if mock_reason:
        if strict:
            raise RuntimeError(f"mock mode refused by --strict: {mock_reason}")
        print("=" * 60, flush=True)
        print(f"[MOCK MODE] {mock_reason}", flush=True)
        print("[MOCK MODE] Returning deterministic mock annotations for testing only.", flush=True)
        print("[MOCK MODE] Set OPENCLAW_ENDPOINT and OPENCLAW_API_KEY for production labeling.", flush=True)
        print("=" * 60, flush=True)
    concurrency = max(1, int(engine_cfg.get("concurrency", 1)))
    limiter = RateLimiter(
        engine_cfg.get("interval_ms", 0),
        engine_cfg.get("rate_limit_per_min", 60),
        int(engine_cfg.get("burst", concurrency)),
    )
    sem = asyncio.Semaphore(concurrency)
    max_retries = max(0, int(engine_cfg.get("max_retries", 0)))
    progress = bool(engine_cfg.get("progress", True))
    alert_failure_rate = float(engine_cfg.get("alert_failure_rate", 0.3))
    alert_consecutive_failures = max(1, int(engine_cfg.get("alert_consecutive_failures", 5)))
    run_logger = LabelRunLogger(engine_cfg.get("log_dir", "logs"))
    counts = {"labeled": 0, "failed": 0, "skipped": 0}
    error_counts: Counter[str] = Counter()
    total = len(tasks)
    counter = 0
    consecutive_failures = 0
    consecutive_alerted = False
    counter_lock = asyncio.Lock()
    counts_lock = asyncio.Lock()

    def log(message: str) -> None:
        if progress:
            print(f"[label] {message}", flush=True)

    log(
        "start "
        f"total={total} concurrency={concurrency} "
        f"statuses={','.join(selected_statuses)} "
        f"rate_limit_per_min={limiter.rate_limit_per_min} burst={limiter.capacity} timeout={client.timeout}s "
        f"log={run_logger.path}"
    )
    await run_logger.write(
        "run_start",
        total=total,
        statuses=selected_statuses,
        concurrency=concurrency,
        rate_limit_per_min=limiter.rate_limit_per_min,
        burst=limiter.capacity,
        timeout=client.timeout,
        max_retries=max_retries,
        model=client.model,
        endpoint=client.endpoint,
        **resolve_sampling_config(config, task_config),
        mock=bool(mock_reason),
        mock_reason=mock_reason,
    )

    async def one(task: dict[str, Any]) -> None:
        nonlocal consecutive_failures, consecutive_alerted, counter
        async with sem:
            async with counter_lock:
                counter += 1
                index = counter
            task_id = task["task_id"]
            started = time.monotonic()
            log(f"start {index}/{total} task_id={task_id}")
            await run_logger.write("task_start", task_id=task_id, index=index, total=total, status=task["status"])
            if task["status"] not in selected_statuses:
                counts["skipped"] += 1
                log(f"skip {index}/{total} task_id={task_id} status={task['status']}")
                await run_logger.write("task_skip", task_id=task_id, index=index, total=total, status=task["status"])
                return
            error = ""
            error_type = "unknown"
            for attempt in range(max_retries + 1):
                try:
                    await limiter.wait()
                    annotation = await _label_once(client, task_config, task)
                    validate_output(annotation, task_config["output_schema"])
                    store.mark_labeled(task_id, annotation)
                    elapsed = time.monotonic() - started
                    async with counts_lock:
                        counts["labeled"] += 1
                        consecutive_failures = 0
                    log(
                        f"done {index}/{total} task_id={task_id} "
                        f"elapsed={elapsed:.2f}s counts={counts}"
                    )
                    await run_logger.write(
                        "task_done",
                        task_id=task_id,
                        index=index,
                        total=total,
                        attempt=attempt + 1,
                        elapsed=round(elapsed, 3),
                    )
                    return
                except Exception as exc:  # keep failures isolated per task
                    error = f"attempt {attempt + 1}: {exc}"
                    error_type = classify_error(exc)
                    log(f"retry {index}/{total} task_id={task_id} type={error_type} {error}")
                    await run_logger.write(
                        "task_retry",
                        task_id=task_id,
                        index=index,
                        total=total,
                        attempt=attempt + 1,
                        error_type=error_type,
                        error=str(exc),
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(min(2 ** attempt, 8))
            store.mark_failed(task_id, error)
            elapsed = time.monotonic() - started
            async with counts_lock:
                counts["failed"] += 1
                consecutive_failures += 1
                error_counts[error_type] += 1
                if not consecutive_alerted and consecutive_failures >= alert_consecutive_failures:
                    consecutive_alerted = True
                    log(
                        "[alert] consecutive failures reached "
                        f"{consecutive_failures}; consider checking OpenClaw endpoint, rate limits, or prompt format"
                    )
            log(f"failed {index}/{total} task_id={task_id} elapsed={elapsed:.2f}s type={error_type} error={error}")
            await run_logger.write(
                "task_failed",
                task_id=task_id,
                index=index,
                total=total,
                elapsed=round(elapsed, 3),
                error_type=error_type,
                error=error,
            )

    try:
        await asyncio.gather(*(one(task) for task in tasks))
        failure_rate = (counts["failed"] / total) if total else 0.0
        if total and failure_rate >= alert_failure_rate and counts["failed"]:
            log(
                "[alert] failure rate is "
                f"{failure_rate:.1%}; common types={dict(error_counts)}; manual investigation is recommended"
            )
        result = {
            **counts,
            "log_path": str(run_logger.path),
            "error_types": dict(error_counts),
            "failure_rate": round(failure_rate, 4),
            "mock": bool(mock_reason),
        }
        await run_logger.write("run_finished", **result)
        log(f"finished counts={counts} error_types={dict(error_counts)} log={run_logger.path}")
        return result
    finally:
        run_logger.close()


async def _label_once(client: LLMClient, task_config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    prompt = task_config["prompt"]
    turns = json.dumps(task["turns"], ensure_ascii=False, indent=2)
    payload = json.dumps(task["payload"], ensure_ascii=False, indent=2)
    schema = json.dumps(task_config["output_schema"], ensure_ascii=False, indent=2)
    values = {
        "turns": turns,
        "payload": payload,
        "schema": schema,
        "task_name": str(task_config.get("name", "")),
        "task_description": str(task_config.get("description", "")),
    }
    messages = [
        {"role": "system", "content": render_prompt_template(prompt["system"], **values)},
        {"role": "user", "content": render_prompt_template(prompt["user"], **values)},
    ]
    return await client.complete_json_async(
        messages,
        task_config["output_schema"],
        resolve_sampling_config(client.config, task_config),
    )


def render_prompt_template(template: str, **values: str) -> str:
    """Replace only supported prompt placeholders, leaving JSON braces intact."""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def resolve_sampling_config(config: dict[str, Any], task_config: dict[str, Any]) -> dict[str, Any]:
    model_cfg = config.get("model", {})
    return {
        "temperature": _first_configured(task_config, model_cfg, "temperature", 0),
        "top_p": _first_configured(task_config, model_cfg, "top_p", 1),
        "seed": _first_configured(task_config, model_cfg, "seed", None),
    }


def _first_configured(task_config: dict[str, Any], model_cfg: dict[str, Any], key: str, default: Any) -> Any:
    if task_config.get(key) is not None:
        return task_config[key]
    if model_cfg.get(key) is not None:
        return model_cfg[key]
    return default


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


def _parse_statuses(statuses: list[str] | tuple[str, ...] | str) -> list[str]:
    if isinstance(statuses, str):
        items = [item.strip() for item in statuses.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in statuses if str(item).strip()]
    return items or ["pending"]


def _utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
