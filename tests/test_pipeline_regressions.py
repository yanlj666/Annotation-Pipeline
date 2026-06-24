import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.batch import archive_batch, batch_status, create_batch, merge_exports
from src.engine import render_prompt_template, resolve_sampling_config, run_labeling, run_preflight, validate_output
from src.export import export_reviewed
from src.ingest import ingest_file, normalize_row, parse_turns
from src.llm_client import LLMClient
from src.reliability import (
    cohen_kappa_from_pairs,
    pabak,
    run_reliability,
    run_reliability_csv_pairs,
    spearman,
    weighted_kappa,
)
from src.store import Store


FIELDS = {
    "session_id": "session_id",
    "exchange_id": "exchange_id",
    "exchange_time": "exchange_time",
    "turns": "turns",
}


class IngestRegressionTests(unittest.TestCase):
    def test_parse_turns_repairs_smart_quote_json(self) -> None:
        value = '[{“role”: “user”, “content”: “hello”}, {“role”: “assistant”, “content”: “ok”}]'

        self.assertEqual(
            parse_turns(value),
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "ok"}],
        )

    def test_default_task_mode_keeps_current_exchange(self) -> None:
        row = {
            "session_id": "s1",
            "exchange_id": "s1_2",
            "exchange_time": "2026-01-01T10:01:00",
            "turns": json.dumps(
                [
                    {"role": "user", "content": "current question"},
                    {"role": "assistant", "content": "current answer"},
                ]
            ),
        }

        task = normalize_row(row, {"fields": FIELDS})

        self.assertEqual(task["task_id"], "s1_2")
        self.assertEqual(task["payload"]["session_id"], "s1")
        self.assertEqual(task["payload"]["exchange_id"], "s1_2")
        self.assertEqual(
            task["turns"],
            [{"role": "user", "content": "current question"}, {"role": "assistant", "content": "current answer"}],
        )
        self.assertNotIn("context_turns", task["payload"])

    def test_turn_only_omits_context(self) -> None:
        row = {
            "session_id": "s1",
            "exchange_id": "s1_2",
            "exchange_time": "2026-01-01T10:01:00",
            "turns": json.dumps(
                [
                    {"role": "user", "content": "current question"},
                ]
            ),
        }

        task = normalize_row(row, {"task_mode": "turn_only", "fields": FIELDS})

        self.assertEqual(task["turns"], [{"role": "user", "content": "current question"}])
        self.assertNotIn("context_turns", task["payload"])

    def test_ingest_groups_exchanges_by_session_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "input.csv"
            csv_path.write_text(
                "session_id,exchange_id,exchange_time,turns\n"
                's1,s1_2,2026-01-01T10:02:00,"[{""role"": ""user"", ""content"": ""second""}]"\n'
                's1,s1_1,2026-01-01T10:01:00,"[{""role"": ""user"", ""content"": ""first""}]"\n',
                encoding="utf-8",
            )
            store = Store(str(tmp_path / "pipeline.db"))
            mapping = {"import_mapping": {"source_format": "csv", "task_mode": "turn_with_context", "fields": FIELDS}}

            ingest_file(str(csv_path), mapping, store)
            task = store.get_task("s1_2")

        self.assertEqual(task["turns"], [{"role": "user", "content": "second"}])
        self.assertEqual(task["payload"]["context_turns"], [{"role": "user", "content": "first"}])

    def test_session_mode_creates_one_task_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "input.csv"
            csv_path.write_text(
                "session_id,exchange_id,exchange_time,turns\n"
                's1,s1_2,2026-01-01T10:02:00,"[{""role"": ""assistant"", ""content"": ""second""}]"\n'
                's1,s1_1,2026-01-01T10:01:00,"[{""role"": ""user"", ""content"": ""first""}]"\n',
                encoding="utf-8",
            )
            store = Store(str(tmp_path / "pipeline.db"))
            mapping = {"import_mapping": {"source_format": "csv", "task_mode": "session", "fields": FIELDS}}

            ingest_file(str(csv_path), mapping, store)
            task = store.get_task("s1")

        self.assertEqual(task["turns"], [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}])
        self.assertEqual(task["payload"]["exchange_ids"], ["s1_1", "s1_2"])

    def test_old_turn_mode_fails_with_migration_hint(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_mode"):
            normalize_row({}, {"turn_mode": "single", "fields": {}})

    def test_ingest_is_idempotent_by_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "input.csv"
            csv_path.write_text(
                "session_id,exchange_id,exchange_time,turns\n"
                's1,task-1,2026-01-01T10:00:00,"[{""role"": ""user"", ""content"": ""hello""}]"\n',
                encoding="utf-8",
            )
            store = Store(str(tmp_path / "pipeline.db"))
            mapping = {"import_mapping": {"source_format": "csv", "fields": FIELDS}}

            first = ingest_file(str(csv_path), mapping, store)
            second = ingest_file(str(csv_path), mapping, store)

        self.assertEqual(first, {"created": 1, "skipped_existing": 0, "invalid": 0, "skipped_by_status": {}})
        self.assertEqual(second, {"created": 0, "skipped_existing": 1, "invalid": 0, "skipped_by_status": {"pending": 1}})


class ExportRegressionTests(unittest.TestCase):
    def test_export_includes_review_reason_payload_and_snipped_context(self) -> None:
        phone = "138" + "00138000"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(str(tmp_path / "pipeline.db"))
            store.init()
            store.upsert_task(
                "s1_2",
                [{"role": "user", "content": f"current phone {phone}"}],
                {
                    "session_id": "s1",
                    "context_turns": [{"role": "assistant", "content": "long history context"}],
                },
            )
            store.mark_reviewed("s1_2", {"label": "ok"}, f"contact {phone}")
            task_config_path = tmp_path / "task.yaml"
            task_config_path.write_text("output_schema: {}\n", encoding="utf-8")

            result = export_reviewed(
                store,
                str(task_config_path),
                {"export": {"masking": True, "mask_fields": ["phone"]}, "task": "intent_v1"},
                str(tmp_path / "out"),
                snippet_chars=7,
            )
            line = Path(result["cases_path"]).read_text(encoding="utf-8").strip()
            exported = json.loads(line)

        self.assertEqual(exported["payload"]["session_id"], "s1")
        self.assertEqual(exported["review_reason"], "contact [PHONE]")
        self.assertEqual(exported["turns"][0]["content"], "current")
        self.assertEqual(exported["current_turn"], exported["turns"])
        self.assertEqual(exported["context_turns"][0]["content"], "long hi")
        self.assertNotIn("context_turns", exported["payload"])


class LLMClientRegressionTests(unittest.TestCase):
    def test_payload_body_uses_utf8_json_without_ascii_escaping(self) -> None:
        client = LLMClient({"model": {"endpoint": "http://example.test", "name": "m"}})
        body = client._payload_body([{"role": "user", "content": "中文"}])

        self.assertIsInstance(body, bytes)
        self.assertIn("中文", body.decode("utf-8"))
        self.assertEqual(json.loads(body.decode("utf-8"))["messages"][0]["content"], "中文")

    def test_payload_includes_default_sampling_parameters_without_null_seed(self) -> None:
        client = LLMClient({"model": {"endpoint": "http://example.test", "name": "m"}})

        payload = client._payload([{"role": "user", "content": "hello"}])

        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["top_p"], 1)
        self.assertNotIn("seed", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_payload_uses_sampling_overrides_and_integer_seed(self) -> None:
        client = LLMClient({"model": {"endpoint": "http://example.test", "name": "m"}})

        payload = client._payload(
            [{"role": "user", "content": "hello"}],
            {"temperature": 0.2, "top_p": 0.9, "seed": 42},
        )

        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["seed"], 42)

    def test_payload_omits_sampling_when_thinking_enabled(self) -> None:
        client = LLMClient({"model": {"endpoint": "http://example.test", "name": "m"}})

        payload = client._payload([{"role": "user", "content": "hello"}], {"thinking": {"enabled": True}, "seed": 42})

        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("seed", payload)


class PromptRenderingRegressionTests(unittest.TestCase):
    def test_render_prompt_template_replaces_known_placeholders_and_keeps_unknowns(self) -> None:
        rendered = render_prompt_template(
            "Task {task_name}: {schema}; sample {turns}; keep {unknown}",
            task_name="intent_v1",
            schema='{"intent": {"type": "string"}}',
            turns="[]",
        )

        self.assertIn("Task intent_v1", rendered)
        self.assertIn('{"intent": {"type": "string"}}', rendered)
        self.assertIn("sample []", rendered)
        self.assertIn("{unknown}", rendered)

    def test_task_sampling_overrides_model_sampling(self) -> None:
        sampling = resolve_sampling_config(
            {"model": {"temperature": 0.4, "top_p": 0.8, "seed": 7}},
            {"temperature": 0, "top_p": 1, "seed": 42},
        )

        self.assertEqual(sampling, {"temperature": 0, "top_p": 1, "seed": 42, "thinking": {"enabled": False}})

    def test_model_sampling_falls_back_to_defaults_when_unset(self) -> None:
        sampling = resolve_sampling_config({"model": {"temperature": None, "seed": None}}, {})

        self.assertEqual(sampling, {"temperature": 0, "top_p": 1, "seed": None, "thinking": {"enabled": False}})


class ValidationRegressionTests(unittest.TestCase):
    def test_enum_must_match_exactly(self) -> None:
        schema = {"role_level": {"type": "string", "enum": ["L6级参谋"]}}

        validate_output({"role_level": "L6级参谋"}, schema)
        with self.assertRaisesRegex(ValueError, "must be one of"):
            validate_output({"role_level": "L6级"}, schema)


class StoreRegressionTests(unittest.TestCase):
    def test_reset_db_clears_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "pipeline.db"))
            store.init()
            store.upsert_task("t1", [{"role": "user", "content": "hello"}], {})

            store.reset()

            self.assertEqual(store.stats()["total"], 0)

    def test_init_migrates_legacy_cases_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            conn = sqlite3.connect(db_path)
            with conn:
                conn.execute("CREATE TABLE cases(task_id TEXT PRIMARY KEY, turns TEXT, payload TEXT, annotation TEXT, status TEXT)")
                conn.execute(
                    "INSERT INTO cases(task_id, turns, payload, annotation, status) VALUES (?, ?, ?, ?, ?)",
                    (
                        "legacy-1",
                        json.dumps([{"role": "user", "content": "hi"}]),
                        json.dumps({"session_id": "legacy"}),
                        json.dumps({"label": "ok"}),
                        "reviewed",
                    ),
                )
            conn.close()

            store = Store(str(db_path))
            store.init()
            task = store.get_task("legacy-1")

            self.assertIsNotNone(task)
            self.assertEqual(task["status"], "reviewed")
            self.assertEqual(task["annotation"], {"label": "ok"})


class LabelingRegressionTests(unittest.TestCase):
    def test_strict_mode_refuses_mock_labeling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "pipeline.db"))
            store.init()
            store.upsert_task("t1", [{"role": "user", "content": "hello"}], {})

            with self.assertRaisesRegex(RuntimeError, "mock mode refused"):
                asyncio.run(
                    run_labeling(
                        {"model": {"endpoint": "${MISSING_ENDPOINT}"}, "engine": {"log_dir": str(Path(tmp) / "logs")}},
                        {"output_schema": {"label": {"type": "string"}}, "prompt": {"system": "", "user": "{turns} {payload} {schema}"}},
                        store,
                        strict=True,
                    )
                )

    def test_preflight_refuses_mock_without_changing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "pipeline.db"))
            store.init()
            store.upsert_task("t1", [{"role": "user", "content": "hello"}], {})

            result = asyncio.run(
                run_preflight(
                    {"model": {"endpoint": "${MISSING_ENDPOINT}"}, "engine": {"log_dir": str(Path(tmp) / "logs")}},
                    {"output_schema": {"label": {"type": "string"}}, "prompt": {"system": "", "user": "{turns} {payload} {schema}"}},
                    store,
                )
            )

            self.assertFalse(result["ok"])
            self.assertEqual(store.get_task("t1")["status"], "pending")


class BatchRegressionTests(unittest.TestCase):
    def test_batch_create_archive_status_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.csv"
            source.write_text("轮次ID,对话内容\nb1,hello\nb2,world\n", encoding="utf-8")
            created = create_batch("batch1", str(source), str(tmp_path / "batches"), sample=1, seed=2, id_field="轮次ID")
            self.assertEqual(created["count"], 1)

            store = Store(str(tmp_path / "pipeline.db"))
            store.init()
            store.upsert_task("b1", [{"role": "user", "content": "hello"}], {})
            status = batch_status(store, str(tmp_path / "batches"))
            self.assertEqual(status["registered_tasks"], 1)

            export_dir = tmp_path / "export"
            export_dir.mkdir()
            (export_dir / "cases.jsonl").write_text(json.dumps({"task_id": "b1"}, ensure_ascii=False) + "\n", encoding="utf-8")
            archive_batch("batch1", str(export_dir), str(tmp_path / "batches"))
            merged = merge_exports(str(tmp_path / "merged.jsonl"), str(tmp_path / "batches"))

            self.assertEqual(merged["merged"], 1)


class ReliabilityRegressionTests(unittest.TestCase):
    def test_core_metrics_have_expected_small_sample_values(self) -> None:
        pairs = [("yes", "yes"), ("yes", "no"), ("no", "no"), ("no", "no")]

        self.assertAlmostEqual(cohen_kappa_from_pairs(pairs, ["no", "yes"]), 0.5)
        self.assertAlmostEqual(pabak(0.75, 2), 0.5)
        self.assertAlmostEqual(weighted_kappa([(1, 1), (2, 3), (3, 3)], [1, 2, 3], "quadratic"), 0.8)
        self.assertAlmostEqual(spearman([1, 2, 3], [1, 3, 2]), 0.5)

    def test_reliability_outputs_reports_for_jsonl_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_a = tmp_path / "run_a.jsonl"
            run_b = tmp_path / "run_b.jsonl"
            out_dir = tmp_path / "reports"
            run_a.write_text(
                "\n".join(
                    [
                        json.dumps({"task_id": "1", "annotation": {"score": 1, "labels": ["a"], "ok": True}}),
                        json.dumps({"task_id": "2", "annotation": {"score": 2, "labels": ["a", "b"], "ok": False}}),
                        json.dumps({"task_id": "3", "annotation": {"score": 3, "labels": ["b"], "ok": False}}),
                    ]
                ),
                encoding="utf-8",
            )
            run_b.write_text(
                "\n".join(
                    [
                        json.dumps({"task_id": "1", "annotation": {"score": 1, "labels": ["a"], "ok": True}}),
                        json.dumps({"task_id": "2", "annotation": {"score": 3, "labels": ["b"], "ok": False}}),
                        json.dumps({"task_id": "3", "annotation": {"score": 3, "labels": ["b"], "ok": True}}),
                    ]
                ),
                encoding="utf-8",
            )
            task_config = {
                "evaluation": {
                    "fields": {
                        "score": {"type": "ordinal", "scale": [1, 2, 3]},
                        "labels": {"type": "multilabel"},
                        "ok": {"type": "binary"},
                    }
                }
            }

            result = run_reliability(str(run_a), str(run_b), task_config, str(out_dir))
            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(result["paired"], 3)
            self.assertTrue((out_dir / "summary.csv").exists())
            self.assertTrue((out_dir / "confusion_matrices.json").exists())
            self.assertTrue((out_dir / "problem_samples.jsonl").exists())
            self.assertTrue((out_dir / "report.md").exists())
            fields = {row["field"]: row for row in summary["fields"]}
            self.assertEqual(fields["score"]["type"], "ordinal")
            self.assertIn(fields["score"]["conclusion"], {"pass", "watch", "fail"})
            self.assertEqual(fields["labels"]["type"], "multilabel")

    def test_reliability_reads_paired_csv_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "paired.csv"
            out_dir = tmp_path / "reports"
            csv_path.write_text(
                "task_id,intent_r1,intent_r2,score_r1,score_r2\n"
                "1,buy,buy,1,1\n"
                "2,buy,refund,2,3\n",
                encoding="utf-8",
            )
            task_config = {
                "evaluation": {
                    "fields": {
                        "intent": {"type": "nominal"},
                        "score": {"type": "ordinal", "scale": [1, 2, 3]},
                    }
                }
            }

            result = run_reliability_csv_pairs(str(csv_path), task_config, str(out_dir))

            self.assertEqual(result["paired"], 2)
            self.assertTrue((out_dir / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
