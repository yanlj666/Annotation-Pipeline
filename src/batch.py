import csv
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ingest import make_task_id
from .store import Store


def create_batch(
    name: str,
    source: str,
    output_dir: str = "data/batches",
    sample: int | None = None,
    seed: int = 1,
    id_field: str = "会话ID",
) -> dict[str, Any]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _read_csv(source)
    if sample is not None and sample > 0 and sample < len(rows):
        rng = random.Random(seed)
        rows = rng.sample(rows, sample)
    output_path = out_dir / f"{name}.csv"
    _write_csv(output_path, rows)
    registry = _load_registry(out_dir)
    registry.setdefault("batches", {})[name] = {
        "name": name,
        "source": str(source),
        "batch_path": str(output_path),
        "count": len(rows),
        "status": "created",
        "export_path": None,
        "task_ids": _task_ids(rows, id_field),
        "created_at": _utc_now_ms(),
        "updated_at": _utc_now_ms(),
    }
    _save_registry(out_dir, registry)
    return {"name": name, "count": len(rows), "batch_path": str(output_path), "registry": str(_registry_path(out_dir))}


def archive_batch(name: str, export_path: str, output_dir: str = "data/batches") -> dict[str, Any]:
    out_dir = Path(output_dir)
    registry = _load_registry(out_dir)
    batch = registry.setdefault("batches", {}).get(name)
    if not batch:
        raise ValueError(f"unknown batch: {name}")
    batch["status"] = "completed"
    batch["export_path"] = export_path
    batch["updated_at"] = _utc_now_ms()
    _save_registry(out_dir, registry)
    return {"name": name, "status": batch["status"], "export_path": export_path}


def batch_status(store: Store, output_dir: str = "data/batches") -> dict[str, Any]:
    out_dir = Path(output_dir)
    registry = _load_registry(out_dir)
    batches = []
    all_task_ids: list[str] = []
    for name, batch in sorted(registry.get("batches", {}).items()):
        task_ids = list(batch.get("task_ids") or [])
        all_task_ids.extend(task_ids)
        counts = store.status_counts_for(task_ids)
        batches.append({
            "name": name,
            "status": batch.get("status"),
            "count": batch.get("count", len(task_ids)),
            "task_statuses": counts,
            "batch_path": batch.get("batch_path"),
            "export_path": batch.get("export_path"),
        })
    stats = store.stats()
    return {
        "database": stats["database"],
        "total_tasks": stats["total"],
        "counts": stats["counts"],
        "remaining": stats["remaining"],
        "registered_tasks": len(set(all_task_ids)),
        "batches": batches,
    }


def merge_exports(output_path: str, output_dir: str = "data/batches") -> dict[str, Any]:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    registry = _load_registry(Path(output_dir))
    seen: set[str] = set()
    count = 0
    with out.open("w", encoding="utf-8") as fh:
        for batch in registry.get("batches", {}).values():
            export_path = batch.get("export_path")
            if not export_path:
                continue
            path = Path(export_path)
            if path.is_dir():
                path = path / "cases.jsonl"
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    task_id = str(item.get("task_id", ""))
                    if task_id and task_id in seen:
                        continue
                    if task_id:
                        seen.add(task_id)
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                    count += 1
    return {"merged": count, "output": str(out)}


def _read_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _task_ids(rows: list[dict[str, str]], id_field: str) -> list[str]:
    return [make_task_id(str(row.get(id_field, "")).strip()) for row in rows if str(row.get(id_field, "")).strip()]


def _registry_path(output_dir: Path) -> Path:
    return output_dir / "registry.json"


def _load_registry(output_dir: Path) -> dict[str, Any]:
    path = _registry_path(output_dir)
    if not path.exists():
        return {"batches": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_registry(output_dir: Path, registry: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    registry["last_updated"] = _utc_now_ms()
    _registry_path(output_dir).write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
