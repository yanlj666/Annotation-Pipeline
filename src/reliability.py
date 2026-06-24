import ast
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


FIELD_TYPES = {"binary", "nominal", "ordinal", "multilabel", "continuous"}


def run_reliability(
    left_path: str,
    right_path: str,
    task_config: dict[str, Any],
    out_dir: str,
    mode: str = "self_consistency",
) -> dict[str, Any]:
    left = read_records(left_path)
    right = read_records(right_path)
    return analyze_records(left, right, task_config, out_dir, mode)


def run_reliability_csv_pairs(
    input_path: str,
    task_config: dict[str, Any],
    out_dir: str,
    mode: str = "self_consistency",
) -> dict[str, Any]:
    left, right = read_paired_csv(input_path)
    return analyze_records(left, right, task_config, out_dir, mode)


def analyze_records(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
    task_config: dict[str, Any],
    out_dir: str,
    mode: str,
) -> dict[str, Any]:
    paired = pair_records(left, right)
    fields = resolve_fields(task_config, paired)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    confusion_matrices: dict[str, Any] = {}
    problem_samples: list[dict[str, Any]] = []

    for field, cfg in fields.items():
        kind = cfg["type"]
        values = [(task_id, a.get(field), b.get(field)) for task_id, a, b in paired]
        if kind in {"binary", "nominal"}:
            summary, detail = evaluate_nominal(field, values, kind)
            confusion_matrices[field] = detail["confusion_matrix"]
        elif kind == "ordinal":
            summary, detail = evaluate_ordinal(field, values, cfg)
        elif kind == "multilabel":
            summary, detail = evaluate_multilabel(field, values)
        elif kind == "continuous":
            summary, detail = evaluate_continuous(field, values)
        else:
            continue
        summary["conclusion"] = conclude(summary, kind, cfg.get("thresholds", "standard"))
        summaries.append(summary)
        details[field] = detail
        problem_samples.extend(find_problem_samples(field, kind, values))

    result = {
        "mode": mode,
        "total_left": len(left),
        "total_right": len(right),
        "paired": len(paired),
        "fields": summaries,
        "outputs": {
            "summary_json": str(output_dir / "summary.json"),
            "summary_csv": str(output_dir / "summary.csv"),
            "confusion_matrices": str(output_dir / "confusion_matrices.json"),
            "problem_samples": str(output_dir / "problem_samples.jsonl"),
            "report": str(output_dir / "report.md"),
        },
    }

    write_outputs(output_dir, result, summaries, details, confusion_matrices, problem_samples)
    return result


def read_records(path: str) -> dict[str, dict[str, Any]]:
    source = Path(path)
    if source.suffix.lower() == ".csv":
        return read_csv_records(source)
    records: dict[str, dict[str, Any]] = {}
    with source.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            task_id = str(item.get("task_id") or "")
            if not task_id:
                raise ValueError(f"{path}:{line_no} missing task_id")
            records[task_id] = extract_annotation(item)
    return records


def read_csv_records(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        records: dict[str, dict[str, Any]] = {}
        for line_no, row in enumerate(reader, 2):
            task_id = str(row.get("task_id") or "").strip()
            if not task_id:
                raise ValueError(f"{path}:{line_no} missing task_id")
            records[task_id] = extract_annotation(row)
        return records


def extract_annotation(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("annotation") or item.get("expected_annotation")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = ast.literal_eval(raw)
        if isinstance(parsed, dict):
            return parsed
    if isinstance(raw, dict):
        return raw
    return {
        key: parse_cell(value)
        for key, value in item.items()
        if key not in {"task_id", "turns", "payload", "status", "updated_at", "created_at"}
        and not key.endswith(("_r1", "_r2"))
    }


def read_paired_csv(path: str, suffix_a: str = "_r1", suffix_b: str = "_r2") -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    source = Path(path)
    with source.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        left: dict[str, dict[str, Any]] = {}
        right: dict[str, dict[str, Any]] = {}
        for line_no, row in enumerate(reader, 2):
            task_id = str(row.get("task_id") or "").strip()
            if not task_id:
                raise ValueError(f"{path}:{line_no} missing task_id")
            left[task_id] = {}
            right[task_id] = {}
            for key, value in row.items():
                if key.endswith(suffix_a):
                    left[task_id][key[: -len(suffix_a)]] = parse_cell(value)
                elif key.endswith(suffix_b):
                    right[task_id][key[: -len(suffix_b)]] = parse_cell(value)
        return left, right


def parse_cell(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped == "":
        return None
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return stripped


def pair_records(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    task_ids = sorted(set(left) & set(right))
    return [(task_id, left[task_id], right[task_id]) for task_id in task_ids]


def resolve_fields(task_config: dict[str, Any], paired: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    configured = ((task_config.get("evaluation") or {}).get("fields") or {})
    fields: dict[str, dict[str, Any]] = {}
    for name, value in configured.items():
        cfg = value if isinstance(value, dict) else {"type": value}
        kind = cfg.get("type")
        if kind not in FIELD_TYPES:
            raise ValueError(f"unsupported evaluation field type for {name}: {kind}")
        fields[name] = {"type": kind, "thresholds": cfg.get("thresholds", "standard"), **cfg}
    seen = set(fields)
    for _, left, right in paired:
        for name, value in {**left, **right}.items():
            if name not in seen:
                fields[name] = {"type": infer_field_type(value), "thresholds": "standard"}
                seen.add(name)
    return fields


def infer_field_type(value: Any) -> str:
    if isinstance(value, bool):
        return "binary"
    if isinstance(value, (list, tuple, set)):
        return "multilabel"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "continuous"
    return "nominal"


def evaluate_nominal(
    field: str,
    values: list[tuple[str, Any, Any]],
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pairs = [(normalize_scalar(a), normalize_scalar(b)) for _, a, b in values if a is not None and b is not None]
    n = len(pairs)
    exact = mean([a == b for a, b in pairs])
    classes = sorted({v for pair in pairs for v in pair})
    matrix = confusion_matrix(pairs, classes)
    kappa = cohen_kappa_from_pairs(pairs, classes)
    summary = {
        "field": field,
        "type": kind,
        "n": n,
        "exact": round_float(exact),
        "kappa": round_float(kappa),
        "pabak": round_float(pabak(exact, len(classes))),
        "n_class": len(classes),
    }
    disagreements = Counter((a, b) for a, b in pairs if a != b).most_common(8)
    detail = {
        "confusion_matrix": matrix,
        "top_disagreements": [{"left": a, "right": b, "count": c} for (a, b), c in disagreements],
        "left_distribution": dict(Counter(a for a, _ in pairs)),
        "right_distribution": dict(Counter(b for _, b in pairs)),
    }
    return summary, detail


def evaluate_ordinal(
    field: str,
    values: list[tuple[str, Any, Any]],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pairs = [(to_number(a), to_number(b)) for _, a, b in values]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    diffs = [abs(a - b) for a, b in pairs]
    signed = [b - a for a, b in pairs]
    classes = cfg.get("scale") or sorted({int(v) if float(v).is_integer() else v for pair in pairs for v in pair})
    summary = {
        "field": field,
        "type": "ordinal",
        "n": n,
        "exact": round_float(mean([a == b for a, b in pairs])),
        "within_1": round_float(mean([diff <= 1 for diff in diffs])),
        "mae": round_float(mean(diffs)),
        "mean_left": round_float(mean([a for a, _ in pairs])),
        "mean_right": round_float(mean([b for _, b in pairs])),
        "mean_diff": round_float(mean(signed)),
        "kappa": round_float(cohen_kappa_from_pairs(pairs, classes)),
        "qwk": round_float(weighted_kappa(pairs, classes, "quadratic")),
    }
    detail = {
        "abs_diff_distribution": {str(k): v for k, v in Counter(diffs).items()},
        "left_distribution": dict(Counter(a for a, _ in pairs)),
        "right_distribution": dict(Counter(b for _, b in pairs)),
        "distribution_delta": distribution_delta(pairs),
    }
    return summary, detail


def evaluate_multilabel(
    field: str,
    values: list[tuple[str, Any, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pairs = [(to_set(a), to_set(b)) for _, a, b in values if a is not None and b is not None]
    labels = sorted(set().union(*(left | right for left, right in pairs))) if pairs else []
    exact = mean([left == right for left, right in pairs])
    jaccard = mean([len(left & right) / len(left | right) if left | right else 1.0 for left, right in pairs])
    tp = fp = fn = tn = 0
    per_label = []
    for label in labels:
        binary_pairs = []
        for left, right in pairs:
            l_hit = label in left
            r_hit = label in right
            binary_pairs.append((l_hit, r_hit))
            if l_hit and r_hit:
                tp += 1
            elif l_hit and not r_hit:
                fp += 1
            elif not l_hit and r_hit:
                fn += 1
            else:
                tn += 1
        exact_label = mean([a == b for a, b in binary_pairs])
        per_label.append(
            {
                "label": label,
                "exact": round_float(exact_label),
                "kappa": round_float(cohen_kappa_from_pairs(binary_pairs, [False, True])),
                "pabak": round_float(pabak(exact_label, 2)),
                "left_prevalence": round_float(mean([a for a, _ in binary_pairs])),
                "right_prevalence": round_float(mean([b for _, b in binary_pairs])),
            }
        )
    micro_precision = tp / (tp + fp) if tp + fp else 0
    micro_recall = tp / (tp + fn) if tp + fn else 0
    micro_f1 = f1(micro_precision, micro_recall)
    macro_f1 = mean(
        [
            f1(
                sum(1 for left, right in pairs if label in left and label in right)
                / max(1, sum(1 for left, _ in pairs if label in left)),
                sum(1 for left, right in pairs if label in left and label in right)
                / max(1, sum(1 for _, right in pairs if label in right)),
            )
            for label in labels
        ]
    )
    summary = {
        "field": field,
        "type": "multilabel",
        "n": len(pairs),
        "exact": round_float(exact),
        "mean_jaccard": round_float(jaccard),
        "hamming_loss": round_float((fp + fn) / ((tp + fp + fn + tn) or 1)),
        "micro_f1": round_float(micro_f1),
        "macro_f1": round_float(macro_f1),
        "n_label": len(labels),
    }
    return summary, {"labels": per_label}


def evaluate_continuous(
    field: str,
    values: list[tuple[str, Any, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pairs = [(to_number(a), to_number(b)) for _, a, b in values]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    diffs = [b - a for a, b in pairs]
    abs_diffs = [abs(diff) for diff in diffs]
    summary = {
        "field": field,
        "type": "continuous",
        "n": len(pairs),
        "mae": round_float(mean(abs_diffs)),
        "rmse": round_float(math.sqrt(mean([diff * diff for diff in diffs])) if diffs else None),
        "mean_left": round_float(mean([a for a, _ in pairs])),
        "mean_right": round_float(mean([b for _, b in pairs])),
        "mean_diff": round_float(mean(diffs)),
        "pearson": round_float(pearson([a for a, _ in pairs], [b for _, b in pairs])),
        "spearman": round_float(spearman([a for a, _ in pairs], [b for _, b in pairs])),
    }
    return summary, {}


def conclude(summary: dict[str, Any], kind: str, thresholds: str) -> str:
    if kind == "ordinal":
        qwk = summary.get("qwk") or 0
        within = summary.get("within_1") or 0
        mae = summary.get("mae") if summary.get("mae") is not None else 999
        exact = summary.get("exact") or 0
        mean_diff = abs(summary.get("mean_diff") or 0)
        if thresholds == "strict":
            if qwk >= 0.85 and within >= 0.95 and mae <= 0.35 and exact >= 0.70 and mean_diff <= 0.15:
                return "pass"
        elif qwk >= 0.80 and within >= 0.90 and mae <= 0.5 and exact >= 0.60 and mean_diff <= 0.2:
            return "pass"
        if qwk < 0.70 or within < 0.85 or mae > 0.7 or exact < 0.50 or mean_diff > 0.3:
            return "fail"
        return "watch"
    if kind in {"binary", "nominal"}:
        kappa = summary.get("kappa") or 0
        exact = summary.get("exact") or 0
        if exact >= 0.80 and kappa >= 0.60:
            return "pass"
        if exact < 0.60 or kappa < 0.20:
            return "fail"
        return "watch"
    if kind == "multilabel":
        f1_value = summary.get("micro_f1") or 0
        jaccard = summary.get("mean_jaccard") or 0
        if f1_value >= 0.80 and jaccard >= 0.70:
            return "pass"
        if f1_value < 0.60 or jaccard < 0.50:
            return "fail"
        return "watch"
    if kind == "continuous":
        return "watch"
    return "watch"


def find_problem_samples(field: str, kind: str, values: list[tuple[str, Any, Any]], limit: int = 50) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for task_id, left, right in values:
        reason = None
        if kind == "ordinal":
            a, b = to_number(left), to_number(right)
            if a is not None and b is not None and abs(a - b) >= 2:
                reason = "ordinal_diff_ge_2"
            elif a is not None and b is not None and abs(a - b) == 1:
                reason = "ordinal_boundary"
        elif kind in {"binary", "nominal"} and left != right:
            reason = "classification_disagreement"
        elif kind == "multilabel":
            l_set, r_set = to_set(left), to_set(right)
            if l_set != r_set:
                reason = "multilabel_disagreement"
        elif kind == "continuous":
            a, b = to_number(left), to_number(right)
            if a is not None and b is not None and abs(a - b) > 0:
                reason = "continuous_difference"
        if reason:
            problems.append({"task_id": task_id, "field": field, "reason": reason, "left": left, "right": right})
        if len(problems) >= limit:
            break
    return problems


def write_outputs(
    out_dir: Path,
    result: dict[str, Any],
    summaries: list[dict[str, Any]],
    details: dict[str, Any],
    confusion_matrices: dict[str, Any],
    problem_samples: list[dict[str, Any]],
) -> None:
    (out_dir / "summary.json").write_text(
        json.dumps({**result, "details": details}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    all_keys = sorted({key for row in summaries for key in row})
    with (out_dir / "summary.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=all_keys)
        writer.writeheader()
        writer.writerows(summaries)
    (out_dir / "confusion_matrices.json").write_text(
        json.dumps(confusion_matrices, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (out_dir / "problem_samples.jsonl").open("w", encoding="utf-8") as fh:
        for item in problem_samples:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    (out_dir / "report.md").write_text(render_report(result, summaries, problem_samples), encoding="utf-8")


def render_report(result: dict[str, Any], summaries: list[dict[str, Any]], problem_samples: list[dict[str, Any]]) -> str:
    lines = [
        "# Reliability Report",
        "",
        f"- Mode: `{result['mode']}`",
        f"- Paired samples: {result['paired']}",
        "",
        "## Field Summary",
        "",
        "| field | type | n | conclusion | key metrics |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for row in summaries:
        metric_items = [
            f"{key}={value}"
            for key, value in row.items()
            if key not in {"field", "type", "n", "conclusion"} and value is not None
        ][:8]
        lines.append(
            f"| {row['field']} | {row['type']} | {row['n']} | {row['conclusion']} | {'; '.join(metric_items)} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `self_consistency` checks stability between two runs; it does not prove labels are correct.",
            "- `gold_eval` compares predictions against a human gold set.",
            "- Nominal, multilabel, and continuous conclusions use conservative default heuristics in v1.",
            f"- Problem samples exported: {len(problem_samples)}",
            "",
        ]
    )
    return "\n".join(lines)


def confusion_matrix(pairs: list[tuple[Any, Any]], classes: list[Any]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {str(left): {str(right): 0 for right in classes} for left in classes}
    for left, right in pairs:
        matrix[str(left)][str(right)] += 1
    return matrix


def cohen_kappa_from_pairs(pairs: list[tuple[Any, Any]], classes: list[Any]) -> float | None:
    n = len(pairs)
    if not n or len(classes) < 2:
        return None
    observed = sum(1 for left, right in pairs if left == right) / n
    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    expected = sum((left_counts[c] / n) * (right_counts[c] / n) for c in classes)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def weighted_kappa(pairs: list[tuple[float, float]], classes: list[Any], weight: str) -> float | None:
    if not pairs or len(classes) < 2:
        return None
    ordered = [float(c) for c in classes]
    index = {value: i for i, value in enumerate(ordered)}
    n_class = len(ordered)
    observed = [[0.0 for _ in ordered] for _ in ordered]
    left_counts = Counter()
    right_counts = Counter()
    for left, right in pairs:
        if float(left) not in index or float(right) not in index:
            continue
        i, j = index[float(left)], index[float(right)]
        observed[i][j] += 1
        left_counts[i] += 1
        right_counts[j] += 1
    n = sum(sum(row) for row in observed)
    if not n:
        return None
    weighted_observed = weighted_expected = 0.0
    for i in range(n_class):
        for j in range(n_class):
            distance = abs(i - j) / (n_class - 1)
            penalty = distance if weight == "linear" else distance * distance
            weighted_observed += penalty * observed[i][j] / n
            weighted_expected += penalty * (left_counts[i] * right_counts[j]) / (n * n)
    if weighted_expected == 0:
        return None
    return 1 - weighted_observed / weighted_expected


def pabak(observed: float | None, n_class: int) -> float | None:
    if observed is None or n_class <= 1:
        return None
    return (observed - 1 / n_class) / (1 - 1 / n_class)


def to_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, set):
        return {str(v) for v in value}
    if isinstance(value, (list, tuple)):
        return {str(v) for v in value}
    if isinstance(value, str):
        parsed = parse_cell(value)
        if parsed is not value:
            return to_set(parsed)
        return {part.strip() for part in value.split("|") if part.strip()}
    return {str(value)}


def to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.upper().startswith("L") and text[1:].isdigit():
        return float(text[1:])
    try:
        return float(text)
    except ValueError:
        return None


def normalize_scalar(value: Any) -> str:
    if isinstance(value, (list, dict, set, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def distribution_delta(pairs: list[tuple[float, float]]) -> dict[str, float]:
    left_counts = Counter(a for a, _ in pairs)
    right_counts = Counter(b for _, b in pairs)
    n = len(pairs) or 1
    keys = sorted(set(left_counts) | set(right_counts))
    return {str(key): round_float((right_counts[key] - left_counts[key]) / n) for key in keys}


def pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(a) != len(b):
        return None
    mean_a, mean_b = mean(a), mean(b)
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    den_b = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    if den_a == 0 or den_b == 0:
        return None
    return num / (den_a * den_b)


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(a) != len(b):
        return None
    return pearson(rank(a), rank(b))


def rank(values: list[float]) -> list[float]:
    ordered = sorted((value, idx) for idx, value in enumerate(values))
    ranks = [0.0 for _ in values]
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        avg_rank = (i + j + 2) / 2
        for _, idx in ordered[i : j + 1]:
            ranks[idx] = avg_rank
        i = j + 1
    return ranks


def mean(values: list[Any]) -> float | None:
    if not values:
        return None
    return sum(float(v) for v in values) / len(values)


def f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def round_float(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 4)
    return value
