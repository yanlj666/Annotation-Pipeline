# Architecture Notes

## Data Flow

```text
CSV/JSONL -> ingest -> SQLite tasks -> label engine -> review workspace -> export
```

The system is intentionally lightweight: Python standard library, PyYAML, httpx, SQLite, and a single HTML workspace.

## Module Boundaries

- `cli.py`: command entry points and config loading.
- `src/ingest.py`: source row normalization and idempotent task creation.
- `src/engine.py`: batch labeling, rate limiting, retries, failure logging, and schema validation.
- `src/llm_client.py`: OpenAI-compatible OpenClaw calls and local mock behavior.
- `src/store.py`: SQLite persistence for tasks and status summaries.
- `review/server.py`: workspace HTTP API.
- `review/ui.html`: browser workspace for status, review, export, and failure triage.
- `src/export.py`: masking and export artifacts.

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
- `ingest`, `label`, `export`, and `gold-eval`: batch/automation commands.

## Stability Principles

- Keep `task_id`, `turns`, and `status` stable.
- Keep business-specific fields inside `payload`.
- Validate model output against `output_schema` before marking a task `labeled`.
- Keep endpoint, API key, model name, timeout, and engine tuning in config.
- Do not hard-code private OpenClaw endpoints or secrets.
- Reuse `src/export.py` for both CLI and page export.
- Exported case text must pass through masking.

## Failure Handling Philosophy

The project does not try to be a full monitoring platform. The first-line strategy is:

```text
task isolation + retry + JSONL evidence + common error classification + model-assisted troubleshooting
```

The label engine records:

- run settings
- task start/done/retry/failed events
- elapsed time
- raw error text
- standardized `error_type`

The workspace summarizes frequent failure types and can export failure evidence for an engineer or model to inspect.
