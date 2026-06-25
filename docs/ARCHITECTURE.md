# Architecture Notes

## Data Flow

```text
CSV/JSONL exchange rows
  -> ingest and validate import mapping
  -> group by session_id and sort by exchange_time
  -> generate idempotent SQLite tasks
  -> optional preflight against the real model
  -> model labeling with schema and enum validation
  -> browser review
  -> masked export
```

The project is intentionally lightweight: Python standard library, PyYAML, httpx, SQLite, and a single HTML workspace.

## Task Model

AP is an exchange-level annotation pipeline.

- One source row represents one exchange.
- `fields.session_id` identifies a complete multi-turn session.
- `fields.exchange_id` identifies the current exchange and is used for exchange task idempotency.
- `fields.exchange_time` sorts exchanges inside the same session.
- `fields.turns` contains the exchange messages, usually user question plus assistant answer.

Stable internal fields:

- `task_id`: stable task key.
- `turns`: current labeling object.
- `status`: task state.
- `payload`: passthrough business fields plus AP reference fields such as `session_id`, `exchange_id`, `exchange_time`, optional `context_turns`, and opt-in derived fields such as `next_user_query`.

Business-specific imported fields must stay in `payload`. Python code should not hard-code business column names.

## Task Modes

`config/import_mapping.yaml` controls the task mode:

- `turn_with_context`: default. Label the current exchange. Earlier exchanges in the same session are added to `payload.context_turns`.
- `turn_only`: label the current exchange without previous context.
- `session`: label the full session. All exchanges in the session are sorted and merged into `turns`.

`turn_mode`, `turn_mode: single`, `turn_mode: conversation`, and `conversation_id` are historical configuration names. Import rejects `turn_mode` and tells the user to migrate to `task_mode`.

Task configs may define `prompt_vars` to lift selected task values, such as `payload.next_user_query`, into explicit prompt placeholders. When `hide_from_payload` is true for a payload field, prompt rendering removes that field from the regular `{payload}` block while keeping the explicit placeholder available.

## Module Boundaries

- `cli.py`: command entry points and config loading.
- `src/ingest.py`: import mapping validation, exchange normalization, session grouping, sorting, and idempotent task creation.
- `src/engine.py`: batch labeling, preflight, rate limiting, retries, failure logging, prompt rendering, output schema validation, and enum validation.
- `src/llm_client.py`: OpenAI-compatible requests, model thinking mode payloads, usage capture, and local mock behavior.
- `src/store.py`: SQLite persistence for tasks and status summaries.
- `review/server.py`: workspace HTTP API.
- `review/ui.html`: browser workspace for status, review, annotation edits, export, and failure triage.
- `src/export.py`: masking and export artifacts.

## Model Request Rules

Model endpoint, API key, model name, timeout, sampling, and thinking mode come from config.

When `model.thinking.enabled: true`, AP sends:

```json
{"thinking": {"type": "enabled"}}
```

and omits `temperature`, `top_p`, and `seed` from the request body.

`reasoning_content` is not stored in annotation, review output, or export.

## Preflight

`preflight` and `validate` are aliases for the same small-sample validation flow.

Preflight:

- sends one or a few real model requests
- validates JSON parsing
- validates `output_schema`
- validates exact enum matches
- classifies failures
- reports cache usage fields when the model service returns them
- does not batch-write labels

Prompt cache warmup is treated as a best-effort side effect, not a guaranteed cost control.

## Status Machine

Normal flow:

```text
pending -> labeled -> reviewed -> exported
```

Failure flow:

```text
pending -> failed
failed -> labeled
```

Failed tasks can be retried by CLI:

```bash
python cli.py label --status failed
```

or from the workspace.

## CLI Semantics

- `serve`: recommended entry point for the full workspace.
- `review`: compatible entry point for review-oriented usage.
- `ingest`: converts source rows into idempotent tasks.
- `preflight` / `validate`: runs small-sample real model validation before the full batch.
- `label`: labels pending or selected-status tasks.
- `export`: writes masked export artifacts.
- `gold-eval`: evaluates against a gold JSONL file.
- `eval-batch`: creates, archives, tracks, and merges staged evaluation batches.

## Stability Principles

- Keep `task_id`, `turns`, and `status` stable.
- Keep business-specific fields inside `payload`.
- Validate model output against `output_schema` before marking a task `labeled`.
- Validate configured enum values by exact match; do not auto-correct near misses.
- Keep endpoint, API key, model name, timeout, thinking, and engine tuning in config.
- Do not hard-code private OpenClaw endpoints or secrets.
- Reuse `src/export.py` for both CLI and page export.
- Exported case text must pass through masking.

## Failure Handling Philosophy

The project does not try to be a full monitoring platform. The first-line strategy is:

```text
task isolation + preflight + retry + JSONL evidence + common error classification + model-assisted troubleshooting
```

The label engine records:

- run settings
- task start/done/retry/failed events
- elapsed time
- raw error text
- standardized `error_type`
- usage information when available

The workspace summarizes frequent failure types and can export failure evidence for an engineer or model to inspect.
