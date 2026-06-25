import argparse
import asyncio
import json
import os
import threading
import webbrowser
from pathlib import Path
from typing import Any

import yaml

from review.server import run_server
from src.batch import archive_batch, batch_status, create_batch, merge_exports
from src.engine import run_labeling
from src.engine import run_preflight
from src.export import export_reviewed
from src.quality import evaluate_gold
from src.ingest import ingest_file
from src.reliability import run_reliability, run_reliability_csv_pairs
from src.store import Store


ROOT = Path(__file__).parent


def main() -> None:
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument("--config", default=argparse.SUPPRESS, help="config file path; can also use AP_CONFIG")
    parser = argparse.ArgumentParser(description="Annotation pipeline", parents=[config_parent])
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db", parents=[config_parent])
    init_db.add_argument("--force", action="store_true", help="drop and recreate the tasks table")

    sub.add_parser("reset-db", parents=[config_parent], help="drop and recreate all pipeline tables")

    sub.add_parser("status", parents=[config_parent], help="show database status counts")

    ingest = sub.add_parser("ingest", parents=[config_parent])
    ingest.add_argument("input")
    ingest.add_argument("--mapping", default="config/import_mapping.yaml")

    label = sub.add_parser("label", parents=[config_parent])
    label.add_argument("--task")
    label.add_argument("--strict", action="store_true", help="fail instead of using mock annotations")
    label.add_argument(
        "--status",
        default="pending",
        help="comma-separated task statuses to label, e.g. pending, failed, pending,failed",
    )

    preflight = sub.add_parser(
        "preflight",
        aliases=["validate"],
        parents=[config_parent],
        help="validate model labeling on a small sample",
    )
    preflight.add_argument("--task")
    preflight.add_argument("--sample", type=int, default=1)
    preflight.add_argument("--strict", action="store_true", default=True, help="fail instead of using mock annotations")
    preflight.add_argument(
        "--status",
        default="pending",
        help="comma-separated task statuses to validate, e.g. pending, failed",
    )

    review = sub.add_parser("review", parents=[config_parent])
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8800)
    review.add_argument("--task")
    review.add_argument("--open", action="store_true", help="open the review page in the default browser")

    serve = sub.add_parser("serve", parents=[config_parent], help="start the full web workspace")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8800)
    serve.add_argument("--task")
    serve.add_argument("--open", action="store_true", help="open the workspace in the default browser")

    export = sub.add_parser("export", parents=[config_parent])
    export.add_argument("--out", "--output", dest="out", default="exports")
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

    gold = sub.add_parser("gold-eval", parents=[config_parent])
    gold.add_argument("gold_jsonl")
    gold.add_argument("--task")

    reliability = sub.add_parser("reliability", parents=[config_parent], help="measure annotation consistency or gold-set agreement")
    reliability.add_argument("--run-a", help="first run JSONL/CSV file for self-consistency")
    reliability.add_argument("--run-b", help="second run JSONL/CSV file for self-consistency")
    reliability.add_argument("--pred", help="prediction JSONL/CSV file for gold evaluation")
    reliability.add_argument("--gold", help="gold JSONL/CSV file for gold evaluation")
    reliability.add_argument("--input", help="paired CSV with <field>_r1 and <field>_r2 columns")
    reliability.add_argument("--mode", choices=["self_consistency", "gold_eval"], default="self_consistency")
    reliability.add_argument("--task")
    reliability.add_argument("--out", default="reports/reliability")

    batch = sub.add_parser("eval-batch", parents=[config_parent], help="manage evaluation batches")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    batch_create = batch_sub.add_parser("create", parents=[config_parent])
    batch_create.add_argument("--name", required=True)
    batch_create.add_argument("--source", required=True)
    batch_create.add_argument("--sample", type=int)
    batch_create.add_argument("--seed", type=int, default=1)
    batch_create.add_argument("--id-field", default="会话ID")
    batch_create.add_argument("--dir", default="data/batches")
    batch_archive = batch_sub.add_parser("archive", parents=[config_parent])
    batch_archive.add_argument("--name", required=True)
    batch_archive.add_argument("--export-path", required=True)
    batch_archive.add_argument("--dir", default="data/batches")
    batch_status_cmd = batch_sub.add_parser("status", parents=[config_parent])
    batch_status_cmd.add_argument("--dir", default="data/batches")
    batch_merge = batch_sub.add_parser("merge", parents=[config_parent])
    batch_merge.add_argument("--out", "--output", dest="out", required=True)
    batch_merge.add_argument("--dir", default="data/batches")

    args = parser.parse_args()
    config_path = getattr(args, "config", None) or os.environ.get("AP_CONFIG") or "config/config.yaml"
    config = load_yaml(config_path)
    store = Store(config.get("database", "data/pipeline.db"))

    if args.command == "init-db":
        if args.force:
            store.reset()
        else:
            store.init()
        print(json.dumps({"ok": True, "database": str(store.db_path)}, ensure_ascii=False))
    elif args.command == "reset-db":
        store.reset()
        print(json.dumps({"ok": True, "database": str(store.db_path), "reset": True}, ensure_ascii=False))
    elif args.command == "status":
        store.init()
        print(json.dumps(store.stats(), ensure_ascii=False))
    elif args.command == "ingest":
        print(json.dumps(ingest_file(args.input, load_yaml(args.mapping), store), ensure_ascii=False))
    elif args.command == "label":
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        try:
            result = asyncio.run(run_labeling(config, task_config, store, statuses=statuses, strict=args.strict))
        except RuntimeError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(2) from exc
        print(json.dumps(result, ensure_ascii=False))
    elif args.command in {"preflight", "validate"}:
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        result = asyncio.run(
            run_preflight(
                config,
                task_config,
                store,
                statuses=statuses,
                sample_size=max(1, args.sample),
                strict=args.strict,
            )
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("ok"):
            raise SystemExit(2)
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
    elif args.command == "reliability":
        task_name = args.task or config.get("task")
        task_config = load_task(task_name)
        if args.input:
            result = run_reliability_csv_pairs(args.input, task_config, args.out, args.mode)
        else:
            left_path = args.pred if args.mode == "gold_eval" else args.run_a
            right_path = args.gold if args.mode == "gold_eval" else args.run_b
            if not left_path or not right_path:
                parser.error("reliability requires --input or a pair of --run-a/--run-b or --pred/--gold")
            result = run_reliability(left_path, right_path, task_config, args.out, args.mode)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "eval-batch":
        if args.batch_command == "create":
            result = create_batch(args.name, args.source, args.dir, args.sample, args.seed, args.id_field)
        elif args.batch_command == "archive":
            result = archive_batch(args.name, args.export_path, args.dir)
        elif args.batch_command == "status":
            store.init()
            result = batch_status(store, args.dir)
        elif args.batch_command == "merge":
            result = merge_exports(args.out, args.dir)
        else:
            raise ValueError(f"unknown eval-batch command: {args.batch_command}")
        print(json.dumps(result, ensure_ascii=False))


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
