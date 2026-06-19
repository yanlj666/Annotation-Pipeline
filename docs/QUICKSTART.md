# Quick Start

This page covers the shortest path for one real annotation run.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Configure

Set model credentials in the environment or edit `config/config.yaml`.

```bash
export OPENCLAW_ENDPOINT="https://openclaw-endpoint.example.com"
export OPENCLAW_API_KEY="..."
```

For DeepSeek thinking mode:

```yaml
model:
  thinking:
    enabled: true
```

## 3. Map Input Fields

Edit `config/import_mapping.yaml`.

```yaml
import_mapping:
  source_format: csv
  task_mode: turn_with_context
  fields:
    session_id: "session_id"
    exchange_id: "exchange_id"
    exchange_time: "exchange_time"
    turns: "turns"
  passthrough:
    - "user_id"
    - "channel"
```

`turns` is one exchange, usually one user message plus one assistant message.

## 4. Import

```bash
python cli.py init-db
python cli.py ingest path/to/input.csv
python cli.py status
```

## 5. Preflight

Run a real model validation before the full batch.

```bash
python cli.py preflight --task intent_v1 --sample 1
```

This checks connectivity, JSON parsing, schema validation, enum validation, and basic model behavior without batch-writing labels.

## 6. Label

```bash
python cli.py label --task intent_v1 --strict
```

Retry failed tasks:

```bash
python cli.py label --status failed --strict
```

## 7. Review

```bash
python cli.py serve --open
```

Use the browser workspace to inspect labels, click annotation fields to add field-specific review notes, and submit reviewed results.

## 8. Export

```bash
python cli.py export --out exports
```

Exports pass through masking. For detailed behavior and troubleshooting, see `docs/GUIDE.md`.
