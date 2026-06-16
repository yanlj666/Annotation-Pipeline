import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.batch import archive_batch, batch_status, create_batch, merge_exports
from src.engine import run_labeling
from src.export import export_reviewed
from src.engine import render_prompt_template, resolve_sampling_config
from src.ingest import ingest_file, normalize_row, parse_turns
from src.llm_client import LLMClient
from src.store import Store
import asyncio


class IngestRegressionTests(unittest.TestCase):
    def test_parse_turns_repairs_smart_quote_json(self) -> None:
        value = "[{“role”: “user”, “content”: “你好”}, {“role”: “assistant”, “content”: “可以”}]"

        self.assertEqual(
            parse_turns(value),
            [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "可以"}],
        )

    def test_default_turn_mode_is_single_and_derives_session(self) -> None:
        row = {
            "会话ID": "6387743448_3",
            "对话内容": json.dumps(
                [
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "第一答"},
                    {"role": "user", "content": "当前问"},
                    {"role": "assistant", "content": "当前答"},
                ],
                ensure_ascii=False,
            ),
        }
        cfg = {"fields": {"conversation_id": "会话ID", "turns": "对话内容"}}

        task = normalize_row(row, cfg)

        self.assertEqual(task["task_id"], "6387743448_3")
        self.assertEqual(task["payload"]["session_id"], "6387743448")
        self.assertEqual(task["payload"]["conversation_id"], "6387743448_3")
        self.assertEqual(
            task["turns"],
            [{"role": "user", "content": "当前问"}, {"role": "assistant", "content": "当前答"}],
        )
        self.assertNotIn("context_turns", task["payload"])

    def test_conversation_mode_keeps_context_in_payload(self) -> None:
        row = {
            "会话ID": "s1_2",
            "对话内容": json.dumps(
                [
                    {"role": "user", "content": "上文问"},
                    {"role": "assistant", "content": "上文答"},
                    {"role": "user", "content": "当前问"},
                ],
                ensure_ascii=False,
            ),
        }
        cfg = {
            "turn_mode": "conversation",
            "fields": {"conversation_id": "会话ID", "turns": "对话内容"},
        }

        task = normalize_row(row, cfg)

        self.assertEqual(task["turns"], [{"role": "user", "content": "当前问"}])
        self.assertEqual(
            task["payload"]["context_turns"],
            [{"role": "user", "content": "上文问"}, {"role": "assistant", "content": "上文答"}],
        )

    def test_ingest_is_idempotent_by_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "input.csv"
            csv_path.write_text(
                "会话ID,对话内容\n"
                'task-1,"[{""role"": ""user"", ""content"": ""hello""}]"\n',
                encoding="utf-8",
            )
            store = Store(str(tmp_path / "pipeline.db"))
            mapping = {"import_mapping": {"source_format": "csv", "fields": {"conversation_id": "会话ID", "turns": "对话内容"}}}

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
                [{"role": "user", "content": f"当前手机号 {phone}"}],
                {
                    "session_id": "s1",
                    "context_turns": [{"role": "assistant", "content": "很长的历史上下文"}],
                },
            )
            store.mark_reviewed("s1_2", {"label": "ok"}, f"联系 {phone}")
            task_config_path = tmp_path / "task.yaml"
            task_config_path.write_text("output_schema: {}\n", encoding="utf-8")

            result = export_reviewed(
                store,
                str(task_config_path),
                {"export": {"masking": True, "mask_fields": ["phone"]}, "task": "intent_v1"},
                str(tmp_path / "out"),
                snippet_chars=3,
            )
            line = Path(result["cases_path"]).read_text(encoding="utf-8").strip()
            exported = json.loads(line)

        self.assertEqual(exported["payload"]["session_id"], "s1")
        self.assertEqual(exported["review_reason"], "联系 [PHONE]")
        self.assertEqual(exported["turns"][0]["content"], "当前手")
        self.assertEqual(exported["current_turn"], exported["turns"])
        self.assertEqual(exported["context_turns"][0]["content"], "很长的")
        self.assertNotIn("context_turns", exported["payload"])


class LLMClientRegressionTests(unittest.TestCase):
    def test_payload_body_uses_utf8_json_without_ascii_escaping(self) -> None:
        client = LLMClient({"model": {"endpoint": "http://example.test", "name": "m"}})
        body = client._payload_body([{"role": "user", "content": "中文…"}])

        self.assertIsInstance(body, bytes)
        self.assertIn("中文…", body.decode("utf-8"))
        self.assertEqual(json.loads(body.decode("utf-8"))["messages"][0]["content"], "中文…")

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

    def test_payload_omits_non_integer_seed(self) -> None:
        client = LLMClient({"model": {"endpoint": "http://example.test", "name": "m"}})

        payload = client._payload([{"role": "user", "content": "hello"}], {"seed": "42"})

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

        self.assertEqual(sampling, {"temperature": 0, "top_p": 1, "seed": 42})

    def test_model_sampling_falls_back_to_defaults_when_unset(self) -> None:
        sampling = resolve_sampling_config({"model": {"temperature": None, "seed": None}}, {})

        self.assertEqual(sampling, {"temperature": 0, "top_p": 1, "seed": None})


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


class BatchRegressionTests(unittest.TestCase):
    def test_batch_create_archive_status_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.csv"
            source.write_text("会话ID,对话内容\nb1,hello\nb2,world\n", encoding="utf-8")
            created = create_batch("batch1", str(source), str(tmp_path / "batches"), sample=1, seed=2)
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


if __name__ == "__main__":
    unittest.main()
