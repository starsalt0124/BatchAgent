from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from batchagent.cli import _auto_run_args, main
from batchagent.manifest import create_sample_manifest, load_manifest
from batchagent.scheduler import state_db_path
from batchagent.store import SessionStore


class CliTests(unittest.TestCase):
    def test_help_uses_bagent_as_primary_command_name(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(output.getvalue().startswith("usage: bagent "))

    def test_packaging_exposes_primary_and_compatibility_entry_points(self) -> None:
        project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = project["project"]["scripts"]
        self.assertEqual(scripts["bagent"], "batchagent.cli:main")
        self.assertEqual(scripts["batchagent"], "batchagent.cli:main")

    def test_auto_run_args_preserve_cli_run_options(self) -> None:
        args = SimpleNamespace(
            limit=2,
            retry_failed=True,
            only=["demo-1", "demo-2"],
            focus="demo-1",
            var=["market=A-share"],
        )
        self.assertEqual(
            _auto_run_args(args),
            ["--limit", "2", "--retry-failed", "--only", "demo-1", "--only", "demo-2", "--var", "market=A-share"],
        )

    def test_auto_run_args_pass_focus_when_no_only_filter(self) -> None:
        args = SimpleNamespace(limit=None, retry_failed=False, only=None, focus="demo-1", var=None)
        self.assertEqual(_auto_run_args(args), ["--focus", "demo-1"])

    def test_harness_show_reads_persisted_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": tmp}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["harness", "show"]), 0)
            self.assertIn("current: built-in", output.getvalue())
            self.assertIn("opencode", output.getvalue())
            self.assertIn("claudecode", output.getvalue())
            self.assertIn("codex", output.getvalue())

    def test_runs_command_lists_run_ids_not_attempt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            manifest_path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(manifest_path)
            manifest = load_manifest(manifest_path)
            store = SessionStore(state_db_path(manifest))
            try:
                store.start_batch_run("run-a", manifest.path, manifest.config.name, manifest.tasks)
                store.start_attempt("attempt-a", "run-a", "demo-1", 1, Path(tmp) / "attempt-a")
                store.finish_attempt("attempt-a", "done")
                store.finish_batch_run("run-a", "completed")
            finally:
                store.close()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["runs", str(manifest_path)]), 0)
            self.assertIn("run-a\tcompleted", output.getvalue())
            self.assertNotIn("attempt-a", output.getvalue())

    def test_inspect_uses_frozen_run_task_after_manifest_task_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            manifest_path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(manifest_path)
            manifest = load_manifest(manifest_path)
            store = SessionStore(state_db_path(manifest))
            try:
                store.start_batch_run(
                    "run-a",
                    manifest.path,
                    manifest.config.name,
                    manifest.tasks,
                    selected_task_ids=["demo-1"],
                )
                store.start_attempt("attempt-a", "run-a", "demo-1", 1, Path(tmp) / "attempt-a")
                store.finish_attempt("attempt-a", "done", result={"answer": "frozen"})
                store.finish_batch_run("run-a", "completed")
            finally:
                store.close()

            text = manifest_path.read_text(encoding="utf-8")
            text = text.replace(
                '| todo | demo-1 | echo | {"message":"first task"} |  | 0 |  |  |  |\n',
                "",
            )
            manifest_path.write_text(text, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["inspect", str(manifest_path), "demo-1", "--run-id", "run-a"]), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["task"]["task_id"], "demo-1")
            self.assertEqual(payload["selected_attempt"]["attempt_id"], "attempt-a")

    def test_explicit_run_id_rejects_another_batch_before_inspect_or_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            first_path = Path(tmp) / "first" / "BATCHAGENT.md"
            second_path = Path(tmp) / "second" / "BATCHAGENT.md"
            first_path.parent.mkdir()
            second_path.parent.mkdir()
            create_sample_manifest(first_path)
            create_sample_manifest(second_path)
            first = load_manifest(first_path)
            store = SessionStore(state_db_path(first))
            try:
                store.start_batch_run(
                    "run-first",
                    first.path,
                    first.config.name,
                    first.tasks,
                    selected_task_ids=["demo-1"],
                )
                store.start_attempt("attempt-first", "run-first", "demo-1", 1, Path(tmp) / "attempt-first")
                store.finish_attempt("attempt-first", "failed", "boom")
                store.finish_batch_run("run-first", "failed")
            finally:
                store.close()

            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(
                    main(["inspect", str(second_path), "demo-1", "--run-id", "run-first"]),
                    2,
                )
                self.assertEqual(
                    main(["retry", str(second_path), "demo-1", "--run-id", "run-first"]),
                    2,
                )
            self.assertIn("belongs to another batch config", errors.getvalue())
            store = SessionStore(state_db_path(first))
            try:
                self.assertEqual(store.run_task("run-first", "demo-1")["status"], "failed")  # type: ignore[index]
                self.assertEqual(len(store.task_attempts("run-first", "demo-1")), 1)
            finally:
                store.close()

    def test_recover_run_id_interrupts_persisted_stale_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            manifest_path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(manifest_path)
            manifest = load_manifest(manifest_path)
            store = SessionStore(state_db_path(manifest))
            try:
                store.start_batch_run(
                    "run-stale",
                    manifest.path,
                    manifest.config.name,
                    manifest.tasks,
                    selected_task_ids=["demo-1"],
                )
                store.start_attempt("attempt-stale", "run-stale", "demo-1", 1, Path(tmp) / "attempt-stale")
            finally:
                store.close()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["recover", str(manifest_path), "--run-id", "run-stale"]), 0)
            self.assertIn("running -> interrupted", output.getvalue())
            self.assertIn(f"bagent resume {manifest_path} run-stale", output.getvalue())
            store = SessionStore(state_db_path(manifest))
            try:
                self.assertEqual(store.batch_run("run-stale")["status"], "interrupted")  # type: ignore[index]
                self.assertEqual(store.run_task("run-stale", "demo-1")["status"], "retry")  # type: ignore[index]
                self.assertEqual(store.task_attempts("run-stale", "demo-1")[0]["status"], "interrupted")
            finally:
                store.close()

    def test_resume_plain_reports_persisted_paused_state_and_queued_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            manifest_path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(manifest_path)
            manifest = load_manifest(manifest_path)
            store = SessionStore(state_db_path(manifest))
            try:
                store.start_batch_run(
                    "run-paused",
                    manifest.path,
                    manifest.config.name,
                    manifest.tasks,
                    selected_task_ids=["demo-1"],
                )
                store.finish_batch_run("run-paused", "paused")
            finally:
                store.close()

            output = io.StringIO()
            with patch("batchagent.cli._resume_with_progress", new=AsyncMock(return_value=[])):
                with contextlib.redirect_stdout(output):
                    code = main(["resume", str(manifest_path), "run-paused", "--no-progress"])
            self.assertEqual(code, 0)
            self.assertIn("Run run-paused: paused", output.getvalue())
            self.assertIn("queued=1", output.getvalue())
            self.assertIn(f"bagent resume {manifest_path} run-paused", output.getvalue())

    def test_resume_plain_returns_failure_from_persisted_run_when_no_attempt_ran(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            manifest_path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(manifest_path)
            manifest = load_manifest(manifest_path)
            store = SessionStore(state_db_path(manifest))
            try:
                store.start_batch_run(
                    "run-failed",
                    manifest.path,
                    manifest.config.name,
                    manifest.tasks,
                    selected_task_ids=["demo-1"],
                )
                store.start_attempt("attempt-failed", "run-failed", "demo-1", 1, Path(tmp) / "attempt-failed")
                store.finish_attempt("attempt-failed", "failed", "boom")
                store.finish_batch_run("run-failed", "failed")
            finally:
                store.close()

            output = io.StringIO()
            with patch("batchagent.cli._resume_with_progress", new=AsyncMock(return_value=[])):
                with contextlib.redirect_stdout(output):
                    code = main(["resume", str(manifest_path), "run-failed", "--no-progress"])
            self.assertEqual(code, 1)
            self.assertIn("Run run-failed: failed", output.getvalue())
            self.assertIn("Retry failed Task", output.getvalue())


if __name__ == "__main__":
    unittest.main()
