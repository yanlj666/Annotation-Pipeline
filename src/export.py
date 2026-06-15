import json
import re
import shutil
from pathlib import Path
from typing import Any

from .store import Store


MASKS = {
    "phone": (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    "id_card": (re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])"), "[ID_CARD]"),
    "full_address": (
        re.compile(
            r"[\u4e00-\u9fa5A-Za-z0-9]{2,}"
            r"(?:省|市|区|县|镇|乡|街道|路|号|栋|幢|单元|室)"
            r"[\u4e00-\u9fa5A-Za-z0-9\-#]{2,40}"
        ),
        "[ADDRESS]",
    ),
    "name": (re.compile(r"(?:(?:姓名|收件人|联系人)[:：\s]*)[\u4e00-\u9fa5]{2,4}"), "[NAME]"),
}


def export_reviewed(
    store: Store,
    task_config_path: str,
    config: dict[str, Any],
    output_dir: str,
    snippet_chars: int = 800,
    mark_exported: bool = False,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    export_cfg = config.get("export", {})
    mask_fields = export_cfg.get("mask_fields", list(MASKS)) if export_cfg.get("masking", True) else []
    cases_path = out / "cases.jsonl"
    count = 0
    with cases_path.open("w", encoding="utf-8") as fh:
        for task in store.list_tasks(statuses or ["reviewed"]):
            masked_turns = mask_value(task["turns"], mask_fields)
            masked_payload = mask_value(task["payload"], mask_fields)
            context_turns = []
            if isinstance(masked_payload, dict):
                context_turns = masked_payload.pop("context_turns", []) or []
            item = {
                "task_id": task["task_id"],
                "turns": _snippets(masked_turns, snippet_chars),
                "current_turn": _snippets(masked_turns, snippet_chars),
                "context_turns": _snippets(context_turns, snippet_chars) if isinstance(context_turns, list) else [],
                "payload": masked_payload,
                "annotation": mask_value(task["annotation"], mask_fields),
                "review_reason": mask_value(task.get("review_reason", ""), mask_fields),
            }
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            if mark_exported:
                store.mark_exported(task["task_id"])
            count += 1
    prompt_dir = out / "prompt_pack"
    prompt_dir.mkdir(exist_ok=True)
    shutil.copy2(task_config_path, prompt_dir / Path(task_config_path).name)
    with (prompt_dir / "metadata.json").open("w", encoding="utf-8") as fh:
        json.dump({"task": config.get("task"), "model": config.get("model", {}).get("name")}, fh, ensure_ascii=False, indent=2)
    return {"cases": count, "cases_path": str(cases_path), "prompt_pack": str(prompt_dir)}


def mask_value(value: Any, fields: list[str]) -> Any:
    if isinstance(value, str):
        text = value
        for field in fields:
            rule = MASKS.get(field)
            if rule:
                text = rule[0].sub(rule[1], text)
        return text
    if isinstance(value, list):
        return [mask_value(v, fields) for v in value]
    if isinstance(value, dict):
        return {k: mask_value(v, fields) for k, v in value.items()}
    return value


def _snippets(turns: list[dict[str, str]], snippet_chars: int) -> list[dict[str, str]]:
    return [{"role": t["role"], "content": t["content"][:snippet_chars]} for t in turns]
