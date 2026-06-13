import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUSES = {"pending", "labeled", "reviewed", "exported", "failed"}


class Store:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    turns_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    annotation_json TEXT,
                    error TEXT,
                    review_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")

    def upsert_task(self, task_id: str, turns: list[dict[str, str]], payload: dict[str, Any]) -> bool:
        now = _utc_now_ms()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO tasks(task_id, turns_json, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (
                    task_id,
                    json.dumps(turns, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return cur.rowcount == 1

    def list_tasks(
        self,
        status: str | list[str] | tuple[str, ...] | None = None,
        limit: int | None = None,
        q: str | None = None,
        sort: str = "created_at",
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        filters: list[str] = []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            statuses = [s for s in statuses if s]
            if statuses:
                filters.append("status IN (" + ", ".join("?" for _ in statuses) + ")")
                params.extend(statuses)
        if q:
            like = f"%{q}%"
            filters.append("(task_id LIKE ? OR payload_json LIKE ? OR error LIKE ? OR review_reason LIKE ?)")
            params.extend([like, like, like, like])
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        if sort not in {"created_at", "updated_at", "task_id", "status"}:
            sort = "created_at"
        direction = "DESC" if order.lower() == "desc" else "ASC"
        sql += f" ORDER BY {sort} {direction}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [self._decode(row) for row in conn.execute(sql, params)]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._decode(row) if row else None

    def mark_labeled(self, task_id: str, annotation: dict[str, Any]) -> None:
        self._update(task_id, "labeled", annotation_json=json.dumps(annotation, ensure_ascii=False), error=None)

    def mark_failed(self, task_id: str, error: str) -> None:
        self._update(task_id, "failed", error=error[:2000])

    def mark_reviewed(self, task_id: str, annotation: dict[str, Any], reason: str = "") -> None:
        self._update(
            task_id,
            "reviewed",
            annotation_json=json.dumps(annotation, ensure_ascii=False),
            review_reason=reason,
            error=None,
        )

    def mark_exported(self, task_id: str) -> None:
        self._update(task_id, "exported")

    def _update(self, task_id: str, status: str, **fields: Any) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, _utc_now_ms()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            params.append(value)
        params.append(task_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", params)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["turns"] = json.loads(item.pop("turns_json"))
        item["payload"] = json.loads(item.pop("payload_json"))
        raw_annotation = item.pop("annotation_json")
        item["annotation"] = json.loads(raw_annotation) if raw_annotation else None
        return item


def _utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
