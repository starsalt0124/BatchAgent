from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from batchagent.store import SessionStore


def _create_legacy_database(path: Path, *, work_id: str = "old-work", attempt_id: str = "old-attempt") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
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
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            artifact_path TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO runs VALUES (
            '{attempt_id}', '{work_id}', 'task-a', 1, 'done', '/tmp/old',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:02+00:00', ''
        );
        INSERT INTO messages(run_id, seq, role, content, raw_json, created_at)
        VALUES ('{attempt_id}', 1, 'assistant', 'legacy output', '{{}}', '2026-01-01T00:00:01+00:00');
        INSERT INTO tool_events(run_id, seq, tool_name, arguments_json, result_json, error, created_at)
        VALUES ('{attempt_id}', 2, 'submit_artifact', '{{}}', '{{"ok":true}}', '', '2026-01-01T00:00:01+00:00');
        INSERT INTO artifacts(run_id, task_id, summary, artifact_path, metadata_json, created_at)
        VALUES ('{attempt_id}', 'task-a', 'legacy artifact', 'answer.txt', '{{}}', '2026-01-01T00:00:02+00:00');
        """
    )
    connection.commit()
    connection.close()


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

    def test_persists_run_task_attempt_hierarchy_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "BATCHAGENT.md"
            manifest.write_text("# demo\n", encoding="utf-8")
            store = SessionStore(root / "state.sqlite3")
            try:
                store.start_batch_run(
                    "run-a",
                    manifest,
                    "demo",
                    [
                        {"id": "task-a", "kind": "write", "input": {"path": "a.txt"}},
                        {"id": "task-b", "kind": "check", "input": {}},
                    ],
                    harness="opencode",
                    harness_version="1.2.3",
                    run_vars={"market": "A-share"},
                    config_snapshot={"parallel": True},
                )
                store.start_attempt(
                    "attempt-a1",
                    "run-a",
                    "task-a",
                    1,
                    root / "runs" / "run-a" / "task-a" / "attempt-a1",
                    harness="opencode",
                )
                time.sleep(0.002)
                store.finish_attempt(
                    "attempt-a1",
                    "failed",
                    "first failure",
                    usage={"input_tokens": 10, "output_tokens": 3},
                )
                store.mark_run_task_retry("run-a", "task-a")
                store.start_attempt(
                    "attempt-a2",
                    "run-a",
                    "task-a",
                    2,
                    root / "runs" / "run-a" / "task-a" / "attempt-a2",
                    harness="opencode",
                )
                store.finish_attempt(
                    "attempt-a2",
                    "done",
                    result={"artifact_path": "a.txt"},
                    usage={
                        "prompt_tokens": 7,
                        "completion_tokens": 5,
                        "total_tokens": 12,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                    external_session_id="external-session",
                    exit_code=0,
                )
                store.finish_batch_run("run-a", "completed", result={"done": 1})

                run = store.batch_run("run-a")
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["manifest_path"], str(manifest.resolve()))
                self.assertEqual(run["harness"], "opencode")
                self.assertEqual(run["run_vars"], {"market": "A-share"})
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["total_tokens"], 25)
                self.assertGreaterEqual(run["elapsed_seconds"], 0)

                tasks = store.run_tasks("run-a")
                self.assertEqual([row["task_id"] for row in tasks], ["task-a", "task-b"])
                self.assertEqual(tasks[0]["attempt_count"], 2)
                self.assertEqual(tasks[0]["latest_attempt_id"], "attempt-a2")
                self.assertEqual(tasks[0]["status"], "done")
                self.assertEqual(tasks[0]["total_tokens"], 25)

                attempts = store.task_attempts("run-a", "task-a")
                self.assertEqual([row["attempt_id"] for row in attempts], ["attempt-a2", "attempt-a1"])
                self.assertEqual(attempts[0]["external_session_id"], "external-session")
                self.assertEqual(attempts[0]["result"], {"artifact_path": "a.txt"})
                self.assertEqual(attempts[0]["cached_tokens"], 2)
            finally:
                store.close()

    def test_resume_marks_orphan_attempt_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "BATCHAGENT.md"
            manifest.write_text("# demo\n", encoding="utf-8")
            store = SessionStore(root / "state.sqlite3")
            try:
                store.start_batch_run("run-a", manifest, "demo", [{"id": "task-a"}])
                store.start_attempt("attempt-a", "run-a", "task-a", 1, root / "attempt-a")
                store.pause_batch_run("run-a")
                store.resume_batch_run("run-a")

                self.assertEqual(store.batch_run("run-a")["status"], "running")  # type: ignore[index]
                self.assertEqual(store.attempt("attempt-a")["status"], "interrupted")  # type: ignore[index]
                self.assertEqual(store.run_task("run-a", "task-a")["status"], "retry")  # type: ignore[index]
            finally:
                store.close()

    def test_attempt_id_collision_never_replaces_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "state.sqlite3")
            try:
                store.start_run("attempt-a", "task-a", 1, root / "first", work_id="run-a")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.start_run("attempt-a", "task-a", 2, root / "second", work_id="run-a")
                self.assertEqual(store.attempt("attempt-a")["run_dir"], str(root / "first"))  # type: ignore[index]
            finally:
                store.close()

    def test_finish_attempt_is_idempotent_without_double_counting_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "state.sqlite3")
            try:
                store.start_batch_run("run-idempotent", root / "BATCHAGENT.md", "demo", [{"id": "task-a"}])
                store.start_attempt("attempt-idempotent", "run-idempotent", "task-a", 1, root / "attempt")
                store.finish_attempt(
                    "attempt-idempotent",
                    "done",
                    result={"answer": 1},
                    usage={"input_tokens": 3, "output_tokens": 2},
                )
                store.finish_attempt(
                    "attempt-idempotent",
                    "done",
                    result={"answer": 1},
                    usage={"input_tokens": 3, "output_tokens": 2},
                )

                self.assertEqual(store.attempt("attempt-idempotent")["total_tokens"], 5)  # type: ignore[index]
                self.assertEqual(store.run_task("run-idempotent", "task-a")["total_tokens"], 5)  # type: ignore[index]
                self.assertEqual(store.batch_run("run-idempotent")["total_tokens"], 5)  # type: ignore[index]
                with self.assertRaises(ValueError):
                    store.finish_attempt("attempt-idempotent", "failed", "late failure")
            finally:
                store.close()

    def test_model_call_usage_is_reconciled_live_and_on_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "state.sqlite3")
            try:
                store.start_batch_run("run-crash", root / "BATCHAGENT.md", "demo", [{"id": "task-a"}])
                store.start_attempt("attempt-crash", "run-crash", "task-a", 1, root / "attempt")
                store.add_model_call(
                    "attempt-crash",
                    1,
                    "test",
                    "model",
                    {"input_tokens": 7, "output_tokens": 3},
                )
                store.add_model_call(
                    "attempt-crash",
                    2,
                    "test",
                    "model",
                    {"input_tokens": 5, "output_tokens": 4},
                )

                self.assertEqual(store.attempt("attempt-crash")["total_tokens"], 19)  # type: ignore[index]
                self.assertEqual(store.run_task("run-crash", "task-a")["total_tokens"], 19)  # type: ignore[index]
                self.assertEqual(store.batch_run("run-crash")["total_tokens"], 19)  # type: ignore[index]
                store.interrupt_batch_run("run-crash")
                self.assertEqual(store.attempt("attempt-crash")["total_tokens"], 19)  # type: ignore[index]
                self.assertEqual(store.run_task("run-crash", "task-a")["total_tokens"], 19)  # type: ignore[index]
                self.assertEqual(store.batch_run("run-crash")["total_tokens"], 19)  # type: ignore[index]
            finally:
                store.close()

    def test_partial_external_usage_is_deep_merged_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "state.sqlite3")
            try:
                store.start_batch_run("run-stream", root / "BATCHAGENT.md", "demo", [{"id": "task-a"}])
                store.start_attempt("attempt-stream", "run-stream", "task-a", 1, root / "attempt")
                store.update_attempt_usage("attempt-stream", {"tokens": {"input_tokens": 5}})
                store.update_attempt_usage("attempt-stream", {"tokens": {"output_tokens": 2}})
                store.update_attempt_usage("attempt-stream", {"cached_input_tokens": 3})

                attempt = store.attempt("attempt-stream")
                self.assertEqual(
                    attempt["usage"]["tokens"],  # type: ignore[index]
                    {"input_tokens": 5, "output_tokens": 2},
                )
                self.assertEqual(attempt["total_tokens"], 7)  # type: ignore[index]
                self.assertEqual(attempt["cached_tokens"], 3)  # type: ignore[index]
                self.assertEqual(store.run_task("run-stream", "task-a")["total_tokens"], 7)  # type: ignore[index]
                self.assertEqual(store.batch_run("run-stream")["total_tokens"], 7)  # type: ignore[index]
            finally:
                store.close()

    def test_migrates_legacy_work_and_task_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
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
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    raw_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE tool_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    artifact_path TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO runs VALUES (
                    'old-attempt', 'old-work', 'task-a', 1, 'done', '/tmp/old',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:02+00:00', ''
                );
                INSERT INTO messages(run_id, seq, role, content, raw_json, created_at)
                VALUES ('old-attempt', 1, 'assistant', 'legacy output', '{}', '2026-01-01T00:00:01+00:00');
                """
            )
            connection.commit()
            connection.close()

            store = SessionStore(path)
            try:
                run = store.batch_run("old-work")
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["status"], "completed")
                self.assertTrue(run["metadata_incomplete"])
                attempt = store.attempt("old-attempt")
                self.assertEqual(attempt["run_id"], "old-work")  # type: ignore[index]
                self.assertEqual(store.run_messages("old-attempt")[0]["content"], "legacy output")
                self.assertNotIn("legacy_runs", store._table_names())
            finally:
                store.close()

    def test_imports_external_legacy_database_without_overwriting_colliding_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old-runs" / "state.sqlite3"
            _create_legacy_database(source)
            source_connection = sqlite3.connect(source)
            try:
                source_tables_before = {
                    row[0]
                    for row in source_connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                source_connection.close()

            target = SessionStore(root / "home" / "state.sqlite3")
            try:
                other_manifest = root / "other" / "BATCHAGENT.md"
                target.start_batch_run("old-work", other_manifest, "other", [{"id": "task-a"}])
                target.start_attempt("old-attempt", "old-work", "task-a", 1, root / "other-attempt")
                target.finish_attempt("old-attempt", "done")
                target.finish_batch_run("old-work", "completed")

                manifest = root / "BATCHAGENT.md"
                self.assertEqual(target.import_legacy_database(source, manifest, "demo"), 1)
                imported_runs = target.batch_runs(manifest)
                self.assertEqual(len(imported_runs), 1)
                imported_run_id = str(imported_runs[0]["run_id"])
                self.assertNotEqual(imported_run_id, "old-work")
                self.assertTrue(imported_run_id.startswith("old-work-legacy-"))
                attempts = target.task_attempts(imported_run_id, "task-a")
                self.assertEqual(len(attempts), 1)
                imported_attempt_id = str(attempts[0]["attempt_id"])
                self.assertNotEqual(imported_attempt_id, "old-attempt")
                self.assertEqual(target.run_messages(imported_attempt_id)[0]["content"], "legacy output")
                self.assertEqual(target.run_tool_events(imported_attempt_id)[0]["tool_name"], "submit_artifact")
                self.assertEqual(target.run_artifacts(imported_attempt_id)[0]["artifact_path"], "answer.txt")

                self.assertEqual(target.import_legacy_database(source, manifest, "demo"), 0)
                self.assertEqual(len(target.batch_runs(manifest)), 1)
                self.assertEqual(
                    target.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    1,
                )
                self.assertEqual(target.batch_run("old-work")["manifest_path"], str(other_manifest.resolve()))  # type: ignore[index]
            finally:
                target.close()

            source_connection = sqlite3.connect(source)
            try:
                source_tables_after = {
                    row[0]
                    for row in source_connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                source_connection.close()
            self.assertEqual(source_tables_after, source_tables_before)
            self.assertIn("runs", source_tables_after)
            self.assertNotIn("batch_runs", source_tables_after)

    def test_repairs_duplicate_attempt_numbers_before_creating_unique_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state.sqlite3"
            store = SessionStore(path)
            try:
                store.start_batch_run("run-a", root / "BATCHAGENT.md", "demo", [{"id": "task-a"}])
                store.start_attempt("attempt-a", "run-a", "task-a", 1, root / "attempt-a")
                store.finish_attempt("attempt-a", "done")
                store.conn.execute("DROP INDEX idx_attempts_run_task_number")
                store.conn.commit()
                store.start_attempt("attempt-b", "run-a", "task-a", 1, root / "attempt-b")
            finally:
                store.close()

            reopened = SessionStore(path)
            try:
                attempts = reopened.conn.execute(
                    """
                    SELECT attempt_no FROM task_attempts
                    WHERE run_id = 'run-a' AND task_id = 'task-a'
                    ORDER BY started_epoch, rowid
                    """
                ).fetchall()
                self.assertEqual([int(row[0]) for row in attempts], [1, 2])
                indexes = {row[1] for row in reopened.conn.execute("PRAGMA index_list(task_attempts)")}
                self.assertIn("idx_attempts_run_task_number", indexes)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
