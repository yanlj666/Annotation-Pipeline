import argparse
import asyncio
import json
import threading
import webbrowser
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
    parser = argparse.ArgumentParser(description="Annotation pipeline")
    parser.add_argument("--config", default="config/config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("input")
    ingest.add_argument("--mapping", default="config/import_mapping.yaml")

    label = sub.add_parser("label")
    label.add_argument("--task")
    label.add_argument(
        "--status",
        default="pending",
        help="comma-separated task statuses to label, e.g. pending, failed, pending,failed",
    )

    review = sub.add_parser("review")
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8000)
    review.add_argument("--task")
    review.add_argument("--open", action="store_true", help="open the review page in the default browser")

    serve = sub.add_parser("serve", help="start the full web workspace")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--task")
    serve.add_argument("--open", action="store_true", help="open the workspace in the default browser")

    export = sub.add_parser("export")
    export.add_argument("--out", default="exports")
    export.add_argument("--task")
    export.add_argument(
        "--mark-exported",
        action="store_true",
        help="mark exported tasks as exported after writing files",
    )
    export.add_argument(
        "--status",
        default="reviewed",
        help="comma-separated statuses to export, e.g. reviewed,exported",
    )

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
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        result = asyncio.run(run_labeling(config, task_config, store, statuses=statuses))
        print(json.dumps(result, ensure_ascii=False))
    elif args.command in {"review", "serve"}:
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        task_path = ROOT / "config" / "tasks" / f"{task_name}.yaml"
        url = display_url(args.host, args.port)
        if args.open:
            threading.Timer(0.5, webbrowser.open, args=[url]).start()
        run_server(
            str(store.db_path),
            args.host,
            args.port,
            task_config["output_schema"],
            config=config,
            task_config=task_config,
            task_config_path=str(task_path),
            app_name="Annotation Workspace" if args.command == "serve" else "Review UI",
        )
    elif args.command == "export":
        task_name = args.task or config.get("task")
        task_path = ROOT / "config" / "tasks" / f"{task_name}.yaml"
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        print(
            json.dumps(
                export_reviewed(
                    store,
                    str(task_path),
                    config,
                    args.out,
                    mark_exported=args.mark_exported,
                    statuses=statuses,
                ),
                ensure_ascii=False,
            )
        )
    elif args.command == "gold-eval":
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        print(json.dumps(evaluate_gold(store, args.gold_jsonl, task_config["output_schema"]), ensure_ascii=False))


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_task(name: str) -> dict[str, Any]:
    return load_yaml(ROOT / "config" / "tasks" / f"{name}.yaml")


def display_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}"


if __name__ == "__main__":
    main()
