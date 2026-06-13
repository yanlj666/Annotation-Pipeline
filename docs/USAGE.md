# Usage Guide

This guide covers the normal workflow for local use and OpenClaw Linux VM use.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, use:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Configure OpenClaw

Edit `config/config.yaml` or provide environment variables:

```bash
export OPENCLAW_ENDPOINT="http://your-openclaw-endpoint"
export OPENCLAW_API_KEY="..."
```

The model endpoint must be OpenAI-compatible and accept:

```text
POST /v1/chat/completions
```

Important engine settings:

```yaml
engine:
  concurrency: 5
  interval_ms: 200
  rate_limit_per_min: 120
  burst: 5
  max_retries: 3
  log_dir: "logs"
  alert_failure_rate: 0.3
  alert_consecutive_failures: 5
```

Lower `concurrency` and `rate_limit_per_min` if OpenClaw returns rate limits or timeouts.

## 3. Start the Workspace

Local desktop:

```bash
python cli.py serve --open
```

OpenClaw Linux VM:

```bash
python cli.py serve --host 0.0.0.0 --port 8000
```

One-step scripts:

```bash
# Windows
start_review.bat

# Linux / OpenClaw VM
HOST=0.0.0.0 PORT=8000 bash start_review.sh
```

## 4. Import Data

Map source fields in `config/import_mapping.yaml`, then run:

```bash
python cli.py init-db
python cli.py ingest path/to/input.csv
```

The import contract is:

- `conversation_id`: idempotency key source
- `turns`: conversation text or JSON turns
- `passthrough`: optional business fields copied into `payload`

Business fields should not be hard-coded in Python code.

## 5. Label

Label pending tasks:

```bash
python cli.py label
```

Retry failed tasks:

```bash
python cli.py label --status failed
```

Process pending and failed tasks together:

```bash
python cli.py label --status pending,failed
```

Each label run writes a JSONL log under `logs/`. The log records run settings, task starts, retries, failures, elapsed time, raw error text, and `error_type`.

## 6. Review and Export

Use the workspace page to:

- view total task status
- inspect labeled conversations
- edit annotation JSON
- write review reasons
- export reviewed cases
- see failure summaries
- retry failed tasks
- export failure reports

CLI export remains available for automation:

```bash
python cli.py export --out exports
```

## 7. Common Failures

- `timeout`: OpenClaw response is too slow. Reduce concurrency or increase model timeout.
- `rate_limited`: OpenClaw returned 429 or rate-limit text. Lower `rate_limit_per_min` and `burst`.
- `server_error`: OpenClaw returned 5xx. Check OpenClaw service health.
- `network_error`: endpoint is unreachable, DNS failed, or connection was refused.
- `invalid_json`: model response was not valid JSON. Tighten the prompt and JSON-only instruction.
- `schema_error`: JSON was valid but did not match `output_schema`.
- `data_error`: input rows are empty or malformed.
- `unknown`: keep the raw log and ask a model or engineer to inspect it.

## 8. What to Give a Model for Troubleshooting

Provide:

- command used
- `config/config.yaml` model and engine summary
- the latest `logs/label_*.jsonl`
- failure summary from the workspace
- 1-3 failed `task_id` examples
- task schema from `config/tasks/*.yaml`

Do not paste API keys or private endpoints into shared reports.
