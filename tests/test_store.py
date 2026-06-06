from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from batchagent.store import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_all_runs_returns_persisted_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "state.sqlite3")
            try:
                store.start_run("run-a", "task-a", 1, Path(tmp) / "runs" / "task-a-run-a", work_id="work-a")
                store.finish_run("run-a", "done")
                store.start_run("run-b", "task-a", 2, Path(tmp) / "runs" / "task-a-run-b", work_id="work-b")
                store.finish_run("run-b", "failed", "boom")

                runs = store.all_runs()
                self.assertEqual([run["run_id"] for run in runs], ["run-b", "run-a"])
                self.assertEqual(runs[0]["work_id"], "work-b")
                self.assertEqual(runs[0]["task_id"], "task-a")
                self.assertEqual(runs[0]["run_dir"], str(Path(tmp) / "runs" / "task-a-run-b"))
                self.assertEqual(runs[0]["error"], "boom")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
