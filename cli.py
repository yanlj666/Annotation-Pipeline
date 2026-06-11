import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from review.server import run_server
from src.engine import run_labeling
from src.export import export_reviewed
from src.quality import evaluate_gold
from src.ingest import ingest_file
from src.store import Store


ROOT = Path(__file__).parent


def main() -> None:
    parser = argparse.ArgumentParser(description="AI XiaoWei annotation pipeline")
    parser.add_argument("--config", default="config/config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("input")
    ingest.add_argument("--mapping", default="config/import_mapping.yaml")

    label = sub.add_parser("label")
    label.add_argument("--task")

    review = sub.add_parser("review")
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8000)
    review.add_argument("--task")

    export = sub.add_parser("export")
    export.add_argument("--out", default="exports")
    export.add_argument("--task")

    gold = sub.add_parser("gold-eval")
    gold.add_argument("gold_jsonl")
    gold.add_argument("--task")

    args = parser.parse_args()
    config = load_yaml(args.config)
    store = Store(config.get("database", "data/pipeline.db"))

    if args.command == "init-db":
        store.init()
        print(json.dumps({"ok": True, "database": str(store.db_path)}, ensure_ascii=False))
    elif args.command == "ingest":
        print(json.dumps(ingest_file(args.input, load_yaml(args.mapping), store), ensure_ascii=False))
    elif args.command == "label":
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        result = asyncio.run(run_labeling(config, task_config, store))
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "review":
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        run_server(str(store.db_path), args.host, args.port, task_config["output_schema"])
    elif args.command == "export":
        task_name = args.task or config.get("task")
        task_path = ROOT / "config" / "tasks" / f"{task_name}.yaml"
        print(json.dumps(export_reviewed(store, str(task_path), config, args.out), ensure_ascii=False))
    elif args.command == "gold-eval":
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        print(json.dumps(evaluate_gold(store, args.gold_jsonl, task_config["output_schema"]), ensure_ascii=False))


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_task(name: str) -> dict[str, Any]:
    return load_yaml(ROOT / "config" / "tasks" / f"{name}.yaml")


if __name__ == "__main__":
    main()
