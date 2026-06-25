# Annotation Pipeline

A local LLM exchange-level annotation workspace:

1. ingest CSV/JSONL into SQLite
2. label rows with an OpenAI-compatible endpoint such as OpenClaw
3. review and correct labels in a browser
4. export masked prompt packs, reviewed cases, and failure evidence

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python cli.py serve --host 127.0.0.1 --port 8800
```

Windows users can run `start_review.bat`. Linux/OpenClaw VM users can run:

```bash
HOST=0.0.0.0 PORT=8800 bash start_review.sh
```

OpenClaw sandbox users can use the managed helper:

```bash
bash scripts/ap_openclaw.sh serve start
bash scripts/ap_openclaw.sh serve status
```

See `docs/OPENCLAW_SANDBOX.md` for watchdog, logs, and label recovery commands.

Open `http://127.0.0.1:8800` for the workspace.

## Core Commands

```bash
python cli.py init-db
python cli.py status
python cli.py ingest path/to/input.csv
python cli.py preflight --task intent_v1 --sample 1 --strict
python cli.py label --task intent_v1 --strict
python cli.py label --status failed
python cli.py serve --host 127.0.0.1 --port 8800
python cli.py export --out exports
python cli.py gold-eval path/to/gold.jsonl
python cli.py eval-batch status
```

## Documentation

- Quick start: [docs/QUICKSTART.md](docs/QUICKSTART.md)
- Full guide: [docs/GUIDE.md](docs/GUIDE.md)
- Architecture notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Key Contracts

- `task_id`, `turns`, and `status` are stable internal fields.
- Imported rows are exchanges. `session_id`, `exchange_id`, `exchange_time`, and `turns` are configured in `config/import_mapping.yaml`.
- Business-specific fields stay in `payload`.
- OpenClaw endpoint, API key, and model name come from config or environment variables.
- Exports must pass through masking.
- Label failures are logged to `logs/label_YYYYMMDD_HHMMSS.jsonl` for model-assisted troubleshooting.
