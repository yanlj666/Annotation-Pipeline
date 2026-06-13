import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from src.engine import validate_output
from src.engine import run_labeling
from src.errors import classify_error, error_summary
from src.export import export_reviewed
from src.store import STATUSES, Store


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = Path(__file__).with_name("ui.html")


def run_server(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8000,
    output_schema: dict | None = None,
    config: dict | None = None,
    task_config: dict | None = None,
    task_config_path: str | None = None,
    app_name: str = "Review UI",
) -> None:
    store = Store(db_path)
    store.init()
    app_config = config or {}
    export_task_path = task_config_path or str(ROOT / "config" / "tasks" / f"{app_config.get('task', 'intent_v1')}.yaml")
    active_task_config = task_config or {"output_schema": output_schema or {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                return self._send(UI_PATH.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if parsed.path == "/api/schema":
                return self._json(output_schema or {})
            if parsed.path == "/api/stats":
                return self._json(store.stats())
            if parsed.path == "/api/failures/summary":
                failed = store.list_tasks("failed", limit=200, sort="updated_at", order="desc")
                return self._json(error_summary(failed))
            if parsed.path == "/api/tasks":
                qs = parse_qs(parsed.query)
                status_arg = qs.get("status", ["labeled,reviewed"])[0]
                statuses = None if status_arg == "all" else [s.strip() for s in status_arg.split(",") if s.strip()]
                limit = int(qs.get("limit", ["100"])[0])
                q = qs.get("q", [""])[0].strip() or None
                sort = qs.get("sort", ["updated_at"])[0]
                order = qs.get("order", ["desc"])[0]
                tasks = store.list_tasks(statuses, limit, q=q, sort=sort, order=order)
                return self._json([{k: v for k, v in t.items() if k != "turns"} for t in tasks])
            if parsed.path.startswith("/api/tasks/"):
                task_id = unquote(parsed.path.rsplit("/", 1)[-1])
                task = store.get_task(task_id)
                return self._json(task or {}, 404 if not task else 200)
            return self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/export":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except json.JSONDecodeError:
                    return self._json({"error": "request body must be JSON"}, 400)
                statuses = body.get("statuses") or ["reviewed"]
                if isinstance(statuses, str):
                    statuses = [s.strip() for s in statuses.split(",") if s.strip()]
                if not isinstance(statuses, list) or not all(isinstance(s, str) and s for s in statuses):
                    return self._json({"error": "statuses must be a non-empty list or comma-separated string"}, 400)
                if "all" in statuses:
                    statuses = sorted(STATUSES)
                output_dir = str(body.get("output_dir") or "exports/ui_export")
                mark_exported = bool(body.get("mark_exported", False))
                try:
                    result = export_reviewed(
                        store,
                        export_task_path,
                        app_config,
                        output_dir,
                        mark_exported=mark_exported,
                        statuses=statuses,
                    )
                except Exception as exc:
                    return self._json({"error": str(exc)}, 500)
                result["stats"] = store.stats()
                return self._json(result)
            if parsed.path == "/api/failures/export":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except json.JSONDecodeError:
                    return self._json({"error": "request body must be JSON"}, 400)
                output_path = Path(str(body.get("output_path") or "exports/failure_report.jsonl"))
                output_path.parent.mkdir(parents=True, exist_ok=True)
                failed = store.list_tasks("failed", sort="updated_at", order="desc")
                with output_path.open("w", encoding="utf-8") as fh:
                    for task in failed:
                        error = task.get("error") or ""
                        item = {
                            "task_id": task["task_id"],
                            "payload": task["payload"],
                            "error": error,
                            "error_type": classify_error(error),
                            "updated_at": task["updated_at"],
                        }
                        fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                return self._json({"failures": len(failed), "path": str(output_path)})
            if parsed.path == "/api/label/retry-failed":
                try:
                    result = asyncio.run(run_labeling(app_config, active_task_config, store, statuses=["failed"]))
                except Exception as exc:
                    return self._json({"error": str(exc)}, 500)
                result["stats"] = store.stats()
                result["failures"] = error_summary(store.list_tasks("failed", limit=200, sort="updated_at", order="desc"))
                return self._json(result)
            if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/review"):
                task_id = unquote(parsed.path.split("/")[-2])
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except json.JSONDecodeError:
                    return self._json({"error": "request body must be JSON"}, 400)
                annotation = body.get("annotation")
                if not isinstance(annotation, dict):
                    return self._json({"error": "annotation must be object"}, 400)
                if output_schema:
                    try:
                        validate_output(annotation, output_schema)
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, 400)
                store.mark_reviewed(task_id, annotation, str(body.get("reason", "")))
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

        def log_message(self, fmt: str, *args) -> None:
            print(f"[review] {self.address_string()} {fmt % args}")

        def _json(self, data: object, status: int = 200) -> None:
            self._send(json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8", status)

        def _send(self, body: str, content_type: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    print(f"{app_name}: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
