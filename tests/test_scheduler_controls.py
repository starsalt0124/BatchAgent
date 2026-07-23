from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

from batchagent.manifest import create_sample_manifest, load_manifest, save_manifest
from batchagent.scheduler import (
    SchedulerError,
    _resolve_run_vars,
    mark_tasks_for_retry,
    resume_manifest,
    rerun_tasks,
    retry_run_task,
    run_manifest,
    state_db_path,
    validate_manifest,
)
from batchagent.store import SessionStore


class SchedulerControlsTests(unittest.TestCase):
    def test_explicit_harness_controls_submit_tool_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(manifest_path)
            manifest = load_manifest(manifest_path)
            manifest.config.tools.remove("submit_artifact")

            validate_manifest(manifest, harness_name="opencode")
            with self.assertRaisesRegex(RuntimeError, "native harness requires submit_artifact"):
                validate_manifest(manifest, harness_name="built-in")

    def test_state_path_auto_imports_manifest_legacy_run_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'run_dir = "~/.bagent/runs"',
                    'run_dir = ".batchagent/runs"',
                ),
                encoding="utf-8",
            )
            legacy_path = root / ".batchagent" / "runs" / "state.sqlite3"
            legacy_path.parent.mkdir(parents=True)
            legacy = sqlite3.connect(legacy_path)
            legacy.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );
                INSERT INTO runs VALUES (
                    'legacy-attempt', 'legacy-run', 'demo-1', 1, 'done', '/tmp/legacy',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', ''
                );
                """
            )
            legacy.commit()
            legacy.close()

            home = root / "home"
            with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                manifest = load_manifest(path)
                db_path = state_db_path(manifest)
                self.assertEqual(db_path, home / "state.sqlite3")
                store = SessionStore(db_path)
                try:
                    runs = store.batch_runs(manifest.path)
                    self.assertEqual([row["run_id"] for row in runs], ["legacy-run"])
                    self.assertEqual(store.task_attempts("legacy-run")[0]["attempt_id"], "legacy-attempt")
                    self.assertEqual(store.batch_run("legacy-run")["harness"], "legacy")  # type: ignore[index]
                finally:
                    store.close()

            source = sqlite3.connect(legacy_path)
            try:
                tables = {
                    row[0]
                    for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                source.close()
            self.assertIn("runs", tables)
            self.assertNotIn("batch_runs", tables)

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
            self.assertTrue(events[0]["run_id"].startswith("run-"))
            self.assertEqual(events[0]["work_id"], events[0]["run_id"])
            self.assertEqual(events[0]["total_tasks"], 2)
            self.assertEqual(events[0]["eligible_tasks"], 0)

    def test_new_run_does_not_inherit_done_status_from_manifest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            manifest = load_manifest(path)
            for task in manifest.tasks:
                task.status = "done"
                task.attempts = 9
            save_manifest(manifest)
            pause = asyncio.Event()
            pause.set()
            events: list[dict] = []
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                results = asyncio.run(run_manifest(path, pause_event=pause, progress_callback=events.append))
            self.assertEqual(results, [])
            self.assertEqual(events[0]["eligible_tasks"], 2)

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
            self.assertTrue(events[0]["run_id"].startswith("run-"))

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

    def test_paused_run_is_persisted_and_resumed_with_new_attempt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            fake = root / "fake_harness.py"
            fake.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "if '--version' in sys.argv:",
                        "    print('fake-harness 1.0')",
                        "    raise SystemExit(0)",
                        "_prompt = sys.stdin.read()",
                        "print(json.dumps({'type':'result','session_id':'session-a','usage':{'input_tokens':4,'output_tokens':2},'result':'ok'}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            text = path.read_text(encoding="utf-8")
            text = text.replace("retries = 1", "retries = 0")
            harness_toml = "\n".join(
                [
                    "[harness]",
                    'name = "opencode"',
                    f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]",
                    "inject_tools = false",
                    "",
                ]
            )
            text = text.replace("[artifact]\nrequire_submit = true", harness_toml + "[artifact]\nrequire_submit = false")
            path.write_text(text, encoding="utf-8")

            pause_event = asyncio.Event()
            pause_event.set()
            events: list[dict] = []
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                first = asyncio.run(run_manifest(path, limit=1, pause_event=pause_event, progress_callback=events.append))
                self.assertEqual(first, [])
                run_id = events[0]["run_id"]
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    self.assertEqual(store.batch_run(run_id)["status"], "paused")  # type: ignore[index]
                    self.assertEqual(store.run_tasks(run_id)[0]["status"], "queued")
                finally:
                    store.close()

                resumed = asyncio.run(resume_manifest(path, run_id))
                self.assertEqual(len(resumed), 1)
                self.assertTrue(resumed[0].success)
                self.assertTrue(resumed[0].attempt_id.startswith("attempt-"))
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    run = store.batch_run(run_id)
                    self.assertEqual(run["status"], "completed")  # type: ignore[index]
                    self.assertEqual(run["total_tokens"], 6)  # type: ignore[index]
                    attempts = store.task_attempts(run_id, "demo-1")
                    self.assertEqual(len(attempts), 1)
                    self.assertEqual(attempts[0]["external_session_id"], "session-a")
                    messages = store.run_messages(attempts[0]["attempt_id"])
                    self.assertTrue(any(message["role"] == "harness" for message in messages))
                finally:
                    store.close()

    def test_pause_after_runs_prefix_and_resume_finishes_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            fake = root / "fake_harness.py"
            fake.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "if '--version' in sys.argv:",
                        "    print('fake-harness 1.0')",
                        "    raise SystemExit(0)",
                        "_prompt = sys.stdin.read()",
                        "print(json.dumps({'type':'result','result':'ok'}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            text = path.read_text(encoding="utf-8").replace("retries = 1", "retries = 0")
            text = text.replace(
                "[artifact]\nrequire_submit = true",
                "\n".join(
                    [
                        "[harness]",
                        'name = "opencode"',
                        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]",
                        "inject_tools = false",
                        "",
                        "[artifact]",
                        "require_submit = false",
                    ]
                ),
            )
            path.write_text(text, encoding="utf-8")

            events: list[dict] = []
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                first = asyncio.run(run_manifest(path, pause_after=1, progress_callback=events.append))
                self.assertEqual(len(first), 1)
                self.assertTrue(first[0].success)
                run_id = first[0].run_id
                self.assertEqual(events[0]["eligible_tasks"], 2)
                self.assertEqual(events[0]["scheduled_tasks"], 1)

                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    self.assertEqual(store.batch_run(run_id)["status"], "paused")  # type: ignore[index]
                    self.assertEqual([row["status"] for row in store.run_tasks(run_id)], ["done", "queued"])
                finally:
                    store.close()

                resumed = asyncio.run(resume_manifest(path, run_id))
                self.assertEqual(len(resumed), 1)
                self.assertEqual(resumed[0].task_id, "demo-2")
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    self.assertEqual(store.batch_run(run_id)["status"], "completed")  # type: ignore[index]
                    self.assertEqual([row["status"] for row in store.run_tasks(run_id)], ["done", "done"])
                finally:
                    store.close()

    def test_retry_failed_task_appends_attempt_inside_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            fake = root / "flaky_harness.py"
            counter = root / "counter.txt"
            fake.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "if '--version' in sys.argv:",
                        "    print('flaky 1.0')",
                        "    raise SystemExit(0)",
                        f"counter = pathlib.Path({str(counter)!r})",
                        "count = int(counter.read_text() or '0') if counter.exists() else 0",
                        "counter.write_text(str(count + 1))",
                        "_prompt = sys.stdin.read()",
                        "if count == 0:",
                        "    print('first attempt failed', file=sys.stderr)",
                        "    raise SystemExit(3)",
                        "print(json.dumps({'type':'result','session_id':'session-success','usage':{'input_tokens':2,'output_tokens':1},'result':'ok'}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            text = path.read_text(encoding="utf-8")
            text = text.replace("retries = 1", "retries = 0")
            harness_toml = "\n".join(
                [
                    "[harness]",
                    'name = "opencode"',
                    f"command = [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]",
                    "inject_tools = false",
                    "",
                ]
            )
            text = text.replace("[artifact]\nrequire_submit = true", harness_toml + "[artifact]\nrequire_submit = false")
            path.write_text(text, encoding="utf-8")

            events: list[dict] = []
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                first = asyncio.run(run_manifest(path, limit=1, progress_callback=events.append))
                self.assertFalse(first[0].success)
                run_id = first[0].run_id
                first_attempt_id = first[0].attempt_id

                retried = asyncio.run(retry_run_task(path, run_id, "demo-1"))
                self.assertTrue(retried[0].success)
                self.assertEqual(retried[0].run_id, run_id)
                self.assertNotEqual(retried[0].attempt_id, first_attempt_id)

                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    attempts = store.task_attempts(run_id, "demo-1")
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual({row["status"] for row in attempts}, {"done", "failed"})
                    self.assertEqual(store.batch_run(run_id)["status"], "completed")  # type: ignore[index]
                finally:
                    store.close()

    def test_resume_without_eligible_tasks_preserves_failed_run_and_full_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    manifest = load_manifest(path)
                    store.start_batch_run(
                        "run-failed",
                        path,
                        "demo",
                        manifest.tasks,
                        selected_task_ids=["demo-1"],
                        config_snapshot=asdict(manifest.config),
                    )
                    store.start_attempt("attempt-failed", "run-failed", "demo-1", 1, root / "attempt")
                    store.finish_attempt("attempt-failed", "failed", "boom")
                    store.finish_batch_run("run-failed", "failed")
                finally:
                    store.close()

                results = asyncio.run(resume_manifest(path, "run-failed"))
                self.assertEqual(results, [])
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    run = store.batch_run("run-failed")
                    self.assertEqual(run["status"], "failed")  # type: ignore[index]
                    self.assertEqual(run["result"]["tasks"], 2)  # type: ignore[index]
                    self.assertEqual(run["result"]["failed"], 1)  # type: ignore[index]
                    self.assertEqual(run["result"]["skipped"], 1)  # type: ignore[index]
                finally:
                    store.close()

    def test_invalid_resume_does_not_mutate_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                manifest = load_manifest(path)
                store = SessionStore(state_db_path(manifest))
                try:
                    store.start_batch_run(
                        "run-complete",
                        path,
                        "demo",
                        manifest.tasks,
                        selected_task_ids=[],
                        config_snapshot=asdict(manifest.config),
                    )
                    store.finish_batch_run("run-complete", "completed")
                finally:
                    store.close()

                with self.assertRaises(ValueError):
                    asyncio.run(resume_manifest(path, "run-complete"))
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    self.assertEqual(store.batch_run("run-complete")["status"], "completed")  # type: ignore[index]
                finally:
                    store.close()

    def test_resume_only_done_task_leaves_other_queued_task_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                manifest = load_manifest(path)
                store = SessionStore(state_db_path(manifest))
                try:
                    store.start_batch_run(
                        "run-partial",
                        path,
                        "demo",
                        manifest.tasks,
                        config_snapshot=asdict(manifest.config),
                    )
                    store.start_attempt("attempt-done", "run-partial", "demo-1", 1, root / "attempt")
                    store.finish_attempt("attempt-done", "done", result={"output": "ok"})
                    store.pause_batch_run("run-partial")
                finally:
                    store.close()

                results = asyncio.run(resume_manifest(path, "run-partial", task_ids={"demo-1"}))
                self.assertEqual(results, [])
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    run = store.batch_run("run-partial")
                    self.assertEqual(run["status"], "paused")  # type: ignore[index]
                    self.assertEqual(run["result"]["done"], 1)  # type: ignore[index]
                    self.assertEqual(run["result"]["pending"], 1)  # type: ignore[index]
                finally:
                    store.close()

    def test_retry_rejects_run_owned_by_another_manifest_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = root / "first" / "BATCHAGENT.md"
            second_path = root / "second" / "BATCHAGENT.md"
            first_path.parent.mkdir()
            second_path.parent.mkdir()
            create_sample_manifest(first_path)
            create_sample_manifest(second_path)
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                manifest = load_manifest(first_path)
                store = SessionStore(state_db_path(manifest))
                try:
                    store.start_batch_run("run-owned", first_path, "first", manifest.tasks)
                    store.start_attempt("attempt-owned", "run-owned", "demo-1", 1, root / "attempt")
                    store.finish_attempt("attempt-owned", "failed", "boom")
                    store.finish_batch_run("run-owned", "failed")
                finally:
                    store.close()

                with self.assertRaises(SchedulerError):
                    asyncio.run(retry_run_task(second_path, "run-owned", "demo-1"))
                store = SessionStore(state_db_path(load_manifest(first_path)))
                try:
                    self.assertEqual(store.batch_run("run-owned")["status"], "failed")  # type: ignore[index]
                    self.assertEqual(store.run_task("run-owned", "demo-1")["status"], "failed")  # type: ignore[index]
                finally:
                    store.close()

    def test_resume_uses_frozen_prompt_and_harness_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            capture = root / "prompt.txt"
            frozen_harness = root / "frozen_harness.py"
            replacement_harness = root / "replacement_harness.py"
            frozen_harness.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "if '--version' in sys.argv:",
                        "    print('frozen 1.0')",
                        "    raise SystemExit(0)",
                        f"pathlib.Path({str(capture)!r}).write_text(sys.stdin.read(), encoding='utf-8')",
                        "print(json.dumps({'type':'result','result':'ok'}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            replacement_harness.write_text(
                "import sys\nprint('replacement 1.0' if '--version' in sys.argv else 'wrong harness')\nraise SystemExit(4)\n",
                encoding="utf-8",
            )
            text = path.read_text(encoding="utf-8")
            text = text.replace("retries = 1", "retries = 0")
            text = text.replace(
                "[artifact]\nrequire_submit = true",
                "\n".join(
                    [
                        "[harness]",
                        'name = "opencode"',
                        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(frozen_harness))}]",
                        "inject_tools = false",
                        "",
                        "[artifact]",
                        "require_submit = false",
                    ]
                ),
            )
            text = text.replace("You are a batch task agent.", "FROZEN PROMPT. You are a batch task agent.")
            path.write_text(text, encoding="utf-8")

            pause = asyncio.Event()
            pause.set()
            events: list[dict] = []
            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                asyncio.run(run_manifest(path, limit=1, pause_event=pause, progress_callback=events.append))
                run_id = events[0]["run_id"]
                updated = path.read_text(encoding="utf-8")
                updated = updated.replace(str(frozen_harness), str(replacement_harness))
                updated = updated.replace("FROZEN PROMPT.", "CHANGED PROMPT.")
                path.write_text(updated, encoding="utf-8")

                results = asyncio.run(resume_manifest(path, run_id))
                self.assertTrue(results[0].success)
                prompt = capture.read_text(encoding="utf-8")
                self.assertIn("FROZEN PROMPT.", prompt)
                self.assertNotIn("CHANGED PROMPT.", prompt)

    def test_running_external_harness_persists_pid_session_usage_and_output_before_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "BATCHAGENT.md"
            create_sample_manifest(path)
            ready = root / "ready"
            harness = root / "long_harness.py"
            harness.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys, time",
                        "if '--version' in sys.argv:",
                        "    print('long 1.0')",
                        "    raise SystemExit(0)",
                        "_prompt = sys.stdin.read()",
                        "print(json.dumps({'type':'system','session_id':'live-session'}), flush=True)",
                        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'live output'}]},'usage':{'input_tokens':5}}), flush=True)",
                        "print('live diagnostic', file=sys.stderr, flush=True)",
                        f"pathlib.Path({str(ready)!r}).write_text('ready')",
                        "time.sleep(60)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            text = path.read_text(encoding="utf-8")
            text = text.replace("retries = 1", "retries = 0")
            text = text.replace(
                "[artifact]\nrequire_submit = true",
                "\n".join(
                    [
                        "[harness]",
                        'name = "opencode"',
                        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(harness))}]",
                        "inject_tools = false",
                        "",
                        "[artifact]",
                        "require_submit = false",
                    ]
                ),
            )
            path.write_text(text, encoding="utf-8")

            async def exercise() -> None:
                future = asyncio.create_task(run_manifest(path, limit=1))
                for _ in range(200):
                    if ready.is_file():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(ready.is_file())
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    runs = store.batch_runs(path, limit=1)
                    self.assertEqual(len(runs), 1)
                    run_id = str(runs[0]["run_id"])
                    attempts = store.task_attempts(run_id, "demo-1")
                    self.assertEqual(len(attempts), 1)
                    attempt = attempts[0]
                    self.assertIsNotNone(attempt["pid"])
                    self.assertEqual(attempt["external_session_id"], "live-session")
                    self.assertEqual(attempt["total_tokens"], 5)
                    messages = store.run_messages(str(attempt["attempt_id"]))
                    self.assertTrue(any(message["role"] == "assistant" and "live output" in message["content"] for message in messages))
                    self.assertTrue(any(message["role"] == "harness-stderr" for message in messages))
                    run_dir = Path(str(attempt["run_dir"]))
                    self.assertIn("live output", (run_dir / "stdout.jsonl").read_text(encoding="utf-8"))
                    self.assertIn("live diagnostic", (run_dir / "stderr.log").read_text(encoding="utf-8"))
                finally:
                    store.close()

                future.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await future
                store = SessionStore(state_db_path(load_manifest(path)))
                try:
                    run = store.batch_run(run_id)
                    attempt = store.task_attempts(run_id, "demo-1")[0]
                    self.assertEqual(run["status"], "interrupted")  # type: ignore[index]
                    self.assertEqual(run["total_tokens"], 5)  # type: ignore[index]
                    self.assertEqual(attempt["status"], "interrupted")
                    self.assertEqual(attempt["total_tokens"], 5)
                finally:
                    store.close()

            with patch.dict(os.environ, {"BAGENT_HOME": str(root / "home")}):
                asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
