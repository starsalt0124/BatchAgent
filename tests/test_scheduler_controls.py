from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from batchagent.manifest import create_sample_manifest, load_manifest, save_manifest
from batchagent.scheduler import mark_tasks_for_retry, rerun_tasks, run_manifest


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
            self.assertEqual(events[0]["total_tasks"], 2)
            self.assertEqual(events[0]["eligible_tasks"], 0)


if __name__ == "__main__":
    unittest.main()

