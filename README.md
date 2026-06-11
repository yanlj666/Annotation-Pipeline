# AI XiaoWei Annotation Pipeline

A local LLM conversation annotation pipeline:

1. ingest CSV/JSONL into SQLite
2. label pending rows with an OpenAI-compatible chat endpoint or local mock mode
3. review and correct labels in a browser
4. export masked prompt packs and reviewed case sets

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Edit `config/config.yaml`, `config/import_mapping.yaml`, and task files in `config/tasks/`.

Endpoint and key can be injected from environment variables:

```bash
export OPENCLAW_ENDPOINT="http://your-openclaw-endpoint"
export OPENCLAW_API_KEY="..."
```

For local development without a model endpoint, set `MOCK_LLM=1` or leave the endpoint unresolved. The client returns deterministic mock JSON that still passes schema validation.

## Commands

```bash
python cli.py init-db
python cli.py ingest path/to/input.csv
python cli.py label --task intent_v1
python cli.py review --host 127.0.0.1 --port 8000
python cli.py export --out exports
python cli.py gold-eval path/to/gold.jsonl
```

Open `http://127.0.0.1:8000` for review.

## Input Contract

`config/import_mapping.yaml` maps source fields into the fixed internal schema:

- `conversation_id`: idempotency key source
- `turns`: conversation source field
- `passthrough`: optional business fields copied into `payload`

No business field is parsed by the code.

## Output

`python cli.py export --out exports` writes:

- `exports/cases.jsonl`: reviewed, masked cases
- `exports/prompt_pack/`: selected prompt template and config metadata

All exported case text is masked before writing.

## Gold Set Format

`gold-eval` expects JSONL rows with a `task_id` and either `annotation` or
`expected_annotation`:

```json
{"task_id":"c001","annotation":{"intent":"shipping","sentiment":"neutral","needs_followup":false}}
```
