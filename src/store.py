import json
import sqlite3
from contextlib import closing
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
        with closing(self.connect()) as conn:
            with conn:
                self._migrate_legacy_cases(conn)
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

    def reset(self) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("DROP TABLE IF EXISTS tasks")
                conn.execute("DROP TABLE IF EXISTS cases")
        self.init()

    def upsert_task(self, task_id: str, turns: list[dict[str, str]], payload: dict[str, Any]) -> bool:
        now = _utc_now_ms()
        with closing(self.connect()) as conn:
            with conn:
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
        with closing(self.connect()) as conn:
            return [self._decode(row) for row in conn.execute(sql, params)]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._decode(row) if row else None

    def stats(self) -> dict[str, Any]:
        counts = {status: 0 for status in sorted(STATUSES)}
        with closing(self.connect()) as conn:
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"):
                counts[row["status"]] = row["count"]
            row = conn.execute(
                "SELECT COUNT(*) AS total, MAX(updated_at) AS last_updated_at FROM tasks"
            ).fetchone()
        total = row["total"] if row else 0
        return {
            "counts": counts,
            "total": total,
            "remaining": counts.get("pending", 0) + counts.get("failed", 0),
            "last_updated_at": row["last_updated_at"] if row else None,
            "database": str(self.db_path),
        }

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

    def status_counts_for(self, task_ids: list[str]) -> dict[str, int]:
        counts = {status: 0 for status in sorted(STATUSES)}
        counts["missing"] = 0
        if not task_ids:
            return counts
        with closing(self.connect()) as conn:
            for task_id in task_ids:
                row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                if row:
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                else:
                    counts["missing"] += 1
        return counts

    def _update(self, task_id: str, status: str, **fields: Any) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, _utc_now_ms()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            params.append(value)
        params.append(task_id)
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", params)

    def _migrate_legacy_cases(self, conn: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "tasks" in tables or "cases" not in tables:
            return
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(cases)")]
        if "task_id" not in columns:
            raise RuntimeError(
                "legacy table 'cases' was found but cannot be migrated automatically: missing task_id column"
            )
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
        now = _utc_now_ms()
        rows = conn.execute("SELECT * FROM cases").fetchall()
        for row in rows:
            item = dict(row)
            task_id = str(item.get("task_id", "")).strip()
            if not task_id:
                continue
            turns = _json_text(item, ["turns_json", "turns"], [])
            payload = _json_text(item, ["payload_json", "payload"], {})
            annotation = _json_text(item, ["annotation_json", "annotation"], None)
            status = str(item.get("status") or ("labeled" if annotation else "pending"))
            if status not in STATUSES:
                status = "pending"
            conn.execute(
                """
                INSERT OR IGNORE INTO tasks(
                    task_id, turns_json, payload_json, status, annotation_json,
                    error, review_reason, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    turns,
                    payload,
                    status,
                    annotation,
                    item.get("error"),
                    item.get("review_reason"),
                    str(item.get("created_at") or now),
                    str(item.get("updated_at") or now),
                ),
            )

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


def _json_text(item: dict[str, Any], keys: list[str], default: Any) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            try:
                json.loads(value)
                return value
            except json.JSONDecodeError:
                return json.dumps(value, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)
    if default is None:
        return None
    return json.dumps(default, ensure_ascii=False)
