import json
from typing import Any

from .store import Store


def evaluate_gold(store: Store, gold_path: str, schema: dict[str, Any]) -> dict[str, Any]:
    total = matched = missing = 0
    field_totals = {key: 0 for key in schema}
    field_matches = {key: 0 for key in schema}
    mismatches: list[dict[str, Any]] = []
    with open(gold_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            total += 1
            gold = json.loads(line)
            task_id = str(gold["task_id"])
            expected = gold.get("annotation") or gold.get("expected_annotation") or {}
            task = store.get_task(task_id)
            actual = task.get("annotation") if task else None
            if not actual:
                missing += 1
                mismatches.append({"task_id": task_id, "reason": "missing_annotation"})
                continue
            row_ok = True
            for field in schema:
                field_totals[field] += 1
                if actual.get(field) == expected.get(field):
                    field_matches[field] += 1
                else:
                    row_ok = False
            if row_ok:
                matched += 1
            else:
                mismatches.append({"task_id": task_id, "expected": expected, "actual": actual})
    accuracy = matched / total if total else 0
    field_accuracy = {
        key: (field_matches[key] / field_totals[key] if field_totals[key] else 0)
        for key in schema
    }
    return {
        "total": total,
        "matched": matched,
        "missing": missing,
        "accuracy": round(accuracy, 4),
        "field_accuracy": {k: round(v, 4) for k, v in field_accuracy.items()},
        "mismatches": mismatches[:50],
    }
