import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.engine import validate_output
from src.store import Store


UI_PATH = Path(__file__).with_name("ui.html")


def run_server(db_path: str, host: str = "127.0.0.1", port: int = 8000, output_schema: dict | None = None) -> None:
    store = Store(db_path)
    store.init()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                return self._send(UI_PATH.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if parsed.path == "/api/tasks":
                qs = parse_qs(parsed.query)
                status = qs.get("status", ["labeled"])[0]
                limit = int(qs.get("limit", ["100"])[0])
                tasks = store.list_tasks(status, limit)
                return self._json([{k: v for k, v in t.items() if k != "turns"} for t in tasks])
            if parsed.path.startswith("/api/tasks/"):
                task_id = parsed.path.rsplit("/", 1)[-1]
                task = store.get_task(task_id)
                return self._json(task or {}, 404 if not task else 200)
            return self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/review"):
                task_id = parsed.path.split("/")[-2]
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
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

    print(f"Review UI: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
