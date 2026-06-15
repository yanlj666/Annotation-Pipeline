# Annotation Pipeline

A local LLM conversation annotation workspace:

1. ingest CSV/JSONL into SQLite
2. label rows with an OpenAI-compatible endpoint such as OpenClaw
3. review and correct labels in a browser
4. export masked prompt packs, reviewed cases, and failure evidence

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python cli.py serve --host 127.0.0.1 --port 8000
```

Windows users can run `start_review.bat`. Linux/OpenClaw VM users can run:

```bash
HOST=0.0.0.0 PORT=8000 bash start_review.sh
```

Open `http://127.0.0.1:8000` for the workspace.

## Core Commands

```bash
python cli.py init-db
python cli.py ingest path/to/input.csv
python cli.py label --task intent_v1
python cli.py label --status failed
python cli.py serve --host 127.0.0.1 --port 8000
python cli.py export --out exports
python cli.py gold-eval path/to/gold.jsonl
```

## Documentation

- Usage guide: [docs/USAGE.md](docs/USAGE.md)
- OpenClaw collaboration guide: [docs/OPENCLAW_COLLABORATION.md](docs/OPENCLAW_COLLABORATION.md)
- Architecture notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Key Contracts

- `task_id`, `turns`, and `status` are stable internal fields.
- Business-specific fields stay in `payload`.
- OpenClaw endpoint, API key, and model name come from config or environment variables.
- Exports must pass through masking.
- Label failures are logged to `logs/label_YYYYMMDD_HHMMSS.jsonl` for model-assisted troubleshooting.
