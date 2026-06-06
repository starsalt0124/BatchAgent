from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path

from batchagent.manifest import create_sample_manifest, load_manifest, save_manifest
from batchagent.scheduler import SchedulerError, _resolve_run_vars, mark_tasks_for_retry, rerun_tasks, run_manifest


class SchedulerControlsTests(unittest.TestCase):
    def test_retry_and_rerun_task_status_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            manifest = load_manifest(path)
            manifest.tasks[0].status = "failed"
            manifest.tasks[0].attempts = 2
            manifest.tasks[0].error = "max_turns exceeded"
            manifest.tasks[1].status = "done"
            manifest.tasks[1].attempts = 1
            manifest.tasks[1].result = "artifact.json"
            save_manifest(manifest)

            changed = mark_tasks_for_retry(path, {"demo-1"})
            self.assertEqual(changed, 1)
            retry_manifest = load_manifest(path)
            self.assertEqual(retry_manifest.tasks[0].status, "retry")
            self.assertEqual(retry_manifest.tasks[0].attempts, 2)
            self.assertEqual(retry_manifest.tasks[0].error, "")

            changed = rerun_tasks(path, {"demo-2"})
            self.assertEqual(changed, 1)
            rerun_manifest = load_manifest(path)
            self.assertEqual(rerun_manifest.tasks[1].status, "todo")
            self.assertEqual(rerun_manifest.tasks[1].attempts, 0)
            self.assertEqual(rerun_manifest.tasks[1].result, "")

    def test_run_manifest_emits_batch_loaded_for_empty_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            events: list[dict] = []
            results = asyncio.run(run_manifest(path, limit=0, progress_callback=events.append))
            self.assertEqual(results, [])
            self.assertEqual(events[0]["type"], "batch_loaded")
            self.assertTrue(events[0]["work_id"].startswith("work-"))
            self.assertEqual(events[0]["total_tasks"], 2)
            self.assertEqual(events[0]["eligible_tasks"], 0)

    def test_run_manifest_requires_declared_run_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "tools = [\"write_file\", \"submit_artifact\"]",
                "tools = [\"write_file\", \"submit_artifact\"]\nrun_variables = [{ name = \"market\", required = true }]",
            )
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(SchedulerError):
                asyncio.run(run_manifest(path, limit=0))

            events: list[dict] = []
            results = asyncio.run(run_manifest(path, limit=0, run_vars={"market": "A股"}, progress_callback=events.append))
            self.assertEqual(results, [])
            self.assertTrue(events[0]["work_id"].startswith("work-"))

    def test_run_variable_default_curr_date_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "tools = [\"write_file\", \"submit_artifact\"]",
                "tools = [\"write_file\", \"submit_artifact\"]\nrun_variables = [{ name = \"as_of_date\", default = \"CURR_DATE\", required = false }]",
            )
            path.write_text(text, encoding="utf-8")
            manifest = load_manifest(path)
            values = _resolve_run_vars(manifest, {})
            self.assertEqual(values["as_of_date"], date.today().isoformat())
            self.assertEqual(manifest.config.run_variables[0].default, "CURR_DATE")


if __name__ == "__main__":
    unittest.main()
