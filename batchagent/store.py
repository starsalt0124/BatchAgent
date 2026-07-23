from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .paths import ensure_private_dir, ensure_private_file
from .util import utc_now


SCHEMA_VERSION = 3


class SessionStore:
    """Durable Batch Config -> Run -> Task -> Attempt state.

    The public ``start_run`` / ``finish_run`` methods remain as compatibility
    shims for the pre-v2 code, where ``run_id`` actually meant a task attempt
    and ``work_id`` meant the containing batch run.
    """

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path.expanduser()
        self.read_only = read_only
        if read_only:
            # TUI reads are latency-sensitive and the database is already
            # initialized by the scheduler. Avoid schema DDL and its commit.
            uri = self.path.resolve(strict=False).as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=30000")
            return
        ensure_private_dir(self.path.parent)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()
        ensure_private_file(self.path)

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self._rename_legacy_tables()
        self._repair_duplicate_attempt_numbers()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS batch_configs (
                config_id TEXT PRIMARY KEY,
                manifest_path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                config_hash TEXT NOT NULL DEFAULT '',
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS batch_runs (
                run_id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                manifest_path TEXT NOT NULL,
                batch_name TEXT NOT NULL,
                status TEXT NOT NULL,
                harness TEXT NOT NULL DEFAULT 'native',
                harness_version TEXT NOT NULL DEFAULT '',
                run_vars_json TEXT NOT NULL DEFAULT '{}',
                config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                selected_task_ids_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL,
                started_epoch REAL NOT NULL,
                finished_at TEXT,
                finished_epoch REAL,
                duration_ms INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                metadata_incomplete INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(config_id) REFERENCES batch_configs(config_id)
            );

            CREATE TABLE IF NOT EXISTS run_tasks (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                input_json TEXT NOT NULL DEFAULT '{}',
                definition_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                latest_attempt_id TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                started_epoch REAL,
                finished_at TEXT,
                finished_epoch REAL,
                duration_ms INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER,
                PRIMARY KEY(run_id, task_id),
                FOREIGN KEY(run_id) REFERENCES batch_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_attempts (
                attempt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                harness TEXT NOT NULL DEFAULT 'native',
                harness_version TEXT NOT NULL DEFAULT '',
                external_session_id TEXT NOT NULL DEFAULT '',
                pid INTEGER,
                exit_code INTEGER,
                run_dir TEXT NOT NULL,
                started_at TEXT NOT NULL,
                started_epoch REAL NOT NULL,
                finished_at TEXT,
                finished_epoch REAL,
                duration_ms INTEGER,
                usage_json TEXT NOT NULL DEFAULT '{}',
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(run_id, task_id) REFERENCES run_tasks(run_id, task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES task_attempts(attempt_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tool_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES task_attempts(attempt_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                artifact_path TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES task_attempts(attempt_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER,
                usage_json TEXT NOT NULL DEFAULT '{}',
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES task_attempts(attempt_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS legacy_import_sources (
                source_path TEXT PRIMARY KEY,
                source_fingerprint TEXT NOT NULL,
                source_schema TEXT NOT NULL,
                first_imported_at TEXT NOT NULL,
                last_imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS legacy_import_items (
                source_path TEXT NOT NULL,
                item_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY(source_path, item_type, source_id),
                FOREIGN KEY(source_path) REFERENCES legacy_import_sources(source_path)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_batch_runs_manifest_started
                ON batch_runs(manifest_path, started_epoch DESC);
            CREATE INDEX IF NOT EXISTS idx_run_tasks_status
                ON run_tasks(run_id, status, ordinal);
            CREATE INDEX IF NOT EXISTS idx_attempts_run_task_started
                ON task_attempts(run_id, task_id, started_epoch DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_run_task_number
                ON task_attempts(run_id, task_id, attempt_no);
            CREATE INDEX IF NOT EXISTS idx_messages_attempt_seq
                ON messages(attempt_id, seq);
            CREATE INDEX IF NOT EXISTS idx_tools_attempt_seq
                ON tool_events(attempt_id, seq);
            CREATE INDEX IF NOT EXISTS idx_artifacts_attempt
                ON artifacts(attempt_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_calls_attempt
                ON model_calls(attempt_id, seq);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_import_items_target
                ON legacy_import_items(item_type, target_id);
            """
        )
        self._import_renamed_legacy_tables()
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _repair_duplicate_attempt_numbers(self) -> None:
        """Make pre-index v2 Attempts unique without deleting history."""
        if "task_attempts" not in self._table_names():
            return
        duplicate = self.conn.execute(
            """
            SELECT 1 FROM task_attempts
            GROUP BY run_id, task_id, attempt_no HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate is None:
            return
        rows = self.conn.execute(
            """
            SELECT rowid, run_id, task_id, attempt_no FROM task_attempts
            ORDER BY run_id, task_id, started_epoch, rowid
            """
        ).fetchall()
        used: dict[tuple[str, str], set[int]] = {}
        with self.conn:
            self.conn.execute("DROP INDEX IF EXISTS idx_attempts_run_task_number")
            for row in rows:
                key = (str(row["run_id"]), str(row["task_id"]))
                attempt_no = int(row["attempt_no"])
                numbers = used.setdefault(key, set())
                while attempt_no in numbers:
                    attempt_no += 1
                numbers.add(attempt_no)
                if attempt_no != int(row["attempt_no"]):
                    self.conn.execute(
                        "UPDATE task_attempts SET attempt_no = ? WHERE rowid = ?",
                        (attempt_no, row["rowid"]),
                    )

    def _rename_legacy_tables(self) -> None:
        tables = self._table_names()
        if "runs" not in tables or "task_attempts" in tables:
            return
        self.conn.execute("PRAGMA foreign_keys=OFF")
        with self.conn:
            self.conn.execute("ALTER TABLE runs RENAME TO legacy_runs")
            for name in ("messages", "tool_events", "artifacts"):
                if name in tables:
                    self.conn.execute(f"ALTER TABLE {name} RENAME TO legacy_{name}")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _import_renamed_legacy_tables(self) -> None:
        if "legacy_runs" not in self._table_names():
            return
        source = f"legacy://{self.path.resolve(strict=False)}"
        config_id = self.register_batch(source, "Legacy BatchAgent history")
        rows = self.conn.execute("SELECT * FROM legacy_runs ORDER BY started_at, rowid").fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            keys = set(row.keys())
            attempt_id = str(row["run_id"])
            parent = str(row["work_id"] or "") if "work_id" in keys else ""
            parent = parent or f"legacy-unscoped-{attempt_id}"
            grouped.setdefault(parent, []).append(row)

        for run_id, attempts in grouped.items():
            statuses = {str(row["status"]) for row in attempts}
            status = self._aggregate_legacy_status(statuses)
            started_at = min(str(row["started_at"]) for row in attempts)
            finished_values = [str(row["finished_at"]) for row in attempts if row["finished_at"]]
            finished_at = max(finished_values) if finished_values else None
            selected = list(dict.fromkeys(str(row["task_id"]) for row in attempts))
            started_epoch = _parse_epoch(started_at) or time.time()
            finished_epoch = _parse_epoch(finished_at) if finished_at else None
            self.conn.execute(
                """
                INSERT OR IGNORE INTO batch_runs(
                    run_id, config_id, manifest_path, batch_name, status, harness,
                    selected_task_ids_json, started_at, started_epoch, finished_at,
                    finished_epoch, duration_ms, metadata_incomplete
                ) VALUES (?, ?, ?, ?, ?, 'legacy', ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    run_id,
                    config_id,
                    source,
                    "Legacy BatchAgent history",
                    status,
                    _json(selected),
                    started_at,
                    started_epoch,
                    finished_at,
                    finished_epoch,
                    _duration_ms(started_epoch, finished_epoch),
                ),
            )
            for ordinal, task_id in enumerate(selected):
                task_rows = [row for row in attempts if str(row["task_id"]) == task_id]
                latest = task_rows[-1]
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO run_tasks(
                        run_id, task_id, ordinal, status, attempt_count,
                        latest_attempt_id, error, started_at, started_epoch,
                        finished_at, finished_epoch, duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        ordinal,
                        str(latest["status"]),
                        len(task_rows),
                        str(latest["run_id"]),
                        str(latest["error"] or ""),
                        str(task_rows[0]["started_at"]),
                        _parse_epoch(str(task_rows[0]["started_at"])),
                        str(latest["finished_at"]) if latest["finished_at"] else None,
                        _parse_epoch(str(latest["finished_at"])) if latest["finished_at"] else None,
                        _duration_ms(
                            _parse_epoch(str(task_rows[0]["started_at"])),
                            _parse_epoch(str(latest["finished_at"])) if latest["finished_at"] else None,
                        ),
                    ),
                )
            used_attempt_numbers: dict[str, set[int]] = {}
            for row in attempts:
                started_epoch = _parse_epoch(str(row["started_at"])) or time.time()
                finished_epoch = _parse_epoch(str(row["finished_at"])) if row["finished_at"] else None
                task_id = str(row["task_id"])
                attempt_no = max(1, int(row["attempt"]))
                used = used_attempt_numbers.setdefault(task_id, set())
                while attempt_no in used:
                    attempt_no += 1
                used.add(attempt_no)
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO task_attempts(
                        attempt_id, run_id, task_id, attempt_no, status, harness,
                        run_dir, started_at, started_epoch, finished_at,
                        finished_epoch, duration_ms, error
                    ) VALUES (?, ?, ?, ?, ?, 'legacy', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["run_id"]),
                        run_id,
                        task_id,
                        attempt_no,
                        str(row["status"]),
                        str(row["run_dir"]),
                        str(row["started_at"]),
                        started_epoch,
                        str(row["finished_at"]) if row["finished_at"] else None,
                        finished_epoch,
                        _duration_ms(started_epoch, finished_epoch),
                        str(row["error"] or ""),
                    ),
                )

        self._copy_legacy_children()
        with self.conn:
            for name in ("legacy_messages", "legacy_tool_events", "legacy_artifacts", "legacy_runs"):
                if name in self._table_names():
                    self.conn.execute(f"DROP TABLE {name}")

    def _copy_legacy_children(self) -> None:
        tables = self._table_names()
        if "legacy_messages" in tables:
            self.conn.execute(
                """
                INSERT INTO messages(attempt_id, seq, role, content, raw_json, created_at)
                SELECT run_id, seq, role, content, raw_json, created_at FROM legacy_messages
                """
            )
        if "legacy_tool_events" in tables:
            self.conn.execute(
                """
                INSERT INTO tool_events(attempt_id, seq, tool_name, arguments_json, result_json, error, created_at)
                SELECT run_id, seq, tool_name, arguments_json, result_json, error, created_at FROM legacy_tool_events
                """
            )
        if "legacy_artifacts" in tables:
            self.conn.execute(
                """
                INSERT INTO artifacts(attempt_id, task_id, summary, artifact_path, metadata_json, created_at)
                SELECT run_id, task_id, summary, artifact_path, metadata_json, created_at FROM legacy_artifacts
                """
            )

    def _table_names(self) -> set[str]:
        return {
            str(row[0])
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

    @staticmethod
    def _aggregate_legacy_status(statuses: set[str]) -> str:
        if "running" in statuses:
            return "interrupted"
        if statuses and statuses <= {"done", "skipped"}:
            return "completed"
        if "failed" in statuses and statuses <= {"done", "skipped", "failed"}:
            return "failed"
        return "paused"

    def import_legacy_database(
        self,
        source_path: str | Path,
        manifest_path: str | Path,
        batch_name: str,
    ) -> int:
        """Import a pre-global state database without modifying the source.

        The source is copied with SQLite's backup API so WAL-backed databases
        are read consistently.  Import mappings live in the destination and
        make repeated discovery idempotent while allowing colliding legacy IDs
        to be renamed instead of replacing unrelated global history.

        Returns the number of newly imported Attempts.
        """
        source = Path(source_path).expanduser().resolve(strict=False)
        destination = self.path.resolve(strict=False)
        if source == destination or not source.is_file():
            return 0

        source_key = str(source)
        fingerprint = _sqlite_fingerprint(source)
        previous = self.conn.execute(
            "SELECT source_fingerprint FROM legacy_import_sources WHERE source_path = ?",
            (source_key,),
        ).fetchone()
        if previous is not None and str(previous[0]) == fingerprint:
            return 0

        source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            source_conn.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in source_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "runs" in tables and "task_attempts" not in tables:
                source_schema = "v1"
            elif {"batch_runs", "run_tasks", "task_attempts"} <= tables:
                source_schema = "v2"
            else:
                return 0

            with tempfile.TemporaryDirectory(prefix=".legacy-import-", dir=self.path.parent) as tmp:
                snapshot_path = Path(tmp) / "state.sqlite3"
                snapshot_conn = sqlite3.connect(str(snapshot_path))
                try:
                    source_conn.backup(snapshot_conn)
                finally:
                    snapshot_conn.close()

                staged = SessionStore(snapshot_path)
                try:
                    return self._import_staged_database(
                        staged,
                        source_key=source_key,
                        source_fingerprint=fingerprint,
                        source_schema=source_schema,
                        manifest_path=manifest_path,
                        batch_name=batch_name,
                    )
                finally:
                    staged.close()
        finally:
            source_conn.close()

    def _import_staged_database(
        self,
        staged: SessionStore,
        *,
        source_key: str,
        source_fingerprint: str,
        source_schema: str,
        manifest_path: str | Path,
        batch_name: str,
    ) -> int:
        manifest = _canonical_manifest_path(manifest_path)
        now = utc_now()
        config_id = hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:24]
        imported_attempts = 0
        attempt_ids: dict[str, str] = {}

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                INSERT INTO legacy_import_sources(
                    source_path, source_fingerprint, source_schema,
                    first_imported_at, last_imported_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_schema = excluded.source_schema,
                    last_imported_at = excluded.last_imported_at
                """,
                (source_key, source_fingerprint, source_schema, now, now),
            )
            self.conn.execute(
                """
                INSERT INTO batch_configs(
                    config_id, manifest_path, name, config_hash,
                    registered_at, updated_at
                ) VALUES (?, ?, ?, '', ?, ?)
                ON CONFLICT(manifest_path) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (config_id, manifest, batch_name, now, now),
            )

            source_runs = staged.conn.execute(
                "SELECT * FROM batch_runs ORDER BY started_epoch, rowid"
            ).fetchall()
            for source_run in source_runs:
                source_run_id = str(source_run["run_id"])
                target_run_id, _new_run = self._claim_legacy_import_id(
                    source_key,
                    "run",
                    source_run_id,
                    table="batch_runs",
                    id_column="run_id",
                )
                source_tasks = staged.conn.execute(
                    "SELECT * FROM run_tasks WHERE run_id = ? ORDER BY ordinal, rowid",
                    (source_run_id,),
                ).fetchall()
                source_attempts = staged.conn.execute(
                    """
                    SELECT * FROM task_attempts
                    WHERE run_id = ? ORDER BY started_epoch, rowid
                    """,
                    (source_run_id,),
                ).fetchall()
                selected_json = str(source_run["selected_task_ids_json"] or "[]")
                if not _loads(selected_json, []):
                    selected_json = _json(list(dict.fromkeys(str(row["task_id"]) for row in source_tasks)))

                self.conn.execute(
                    """
                    INSERT INTO batch_runs(
                        run_id, config_id, manifest_path, batch_name, status,
                        harness, harness_version, run_vars_json,
                        config_snapshot_json, selected_task_ids_json,
                        started_at, started_epoch, finished_at, finished_epoch,
                        duration_ms, prompt_tokens, completion_tokens,
                        total_tokens, cached_tokens, reasoning_tokens,
                        result_json, error, metadata_incomplete
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        config_id = excluded.config_id,
                        manifest_path = excluded.manifest_path,
                        batch_name = excluded.batch_name,
                        status = excluded.status,
                        harness = excluded.harness,
                        harness_version = excluded.harness_version,
                        run_vars_json = excluded.run_vars_json,
                        config_snapshot_json = excluded.config_snapshot_json,
                        selected_task_ids_json = excluded.selected_task_ids_json,
                        started_at = excluded.started_at,
                        started_epoch = excluded.started_epoch,
                        finished_at = excluded.finished_at,
                        finished_epoch = excluded.finished_epoch,
                        duration_ms = excluded.duration_ms,
                        prompt_tokens = excluded.prompt_tokens,
                        completion_tokens = excluded.completion_tokens,
                        total_tokens = excluded.total_tokens,
                        cached_tokens = excluded.cached_tokens,
                        reasoning_tokens = excluded.reasoning_tokens,
                        result_json = excluded.result_json,
                        error = excluded.error,
                        metadata_incomplete = excluded.metadata_incomplete
                    """,
                    (
                        target_run_id,
                        config_id,
                        manifest,
                        batch_name,
                        source_run["status"],
                        source_run["harness"],
                        source_run["harness_version"],
                        source_run["run_vars_json"],
                        source_run["config_snapshot_json"],
                        selected_json,
                        source_run["started_at"],
                        source_run["started_epoch"],
                        source_run["finished_at"],
                        source_run["finished_epoch"],
                        source_run["duration_ms"],
                        source_run["prompt_tokens"],
                        source_run["completion_tokens"],
                        source_run["total_tokens"],
                        source_run["cached_tokens"],
                        source_run["reasoning_tokens"],
                        source_run["result_json"],
                        source_run["error"],
                        source_run["metadata_incomplete"],
                    ),
                )

                task_rows = {str(row["task_id"]): row for row in source_tasks}
                for source_attempt in source_attempts:
                    task_id = str(source_attempt["task_id"])
                    if task_id not in task_rows:
                        self.conn.execute(
                            """
                            INSERT OR IGNORE INTO run_tasks(
                                run_id, task_id, ordinal, definition_json, status
                            ) VALUES (?, ?, ?, ?, 'queued')
                            """,
                            (target_run_id, task_id, len(task_rows), _json({"id": task_id})),
                        )
                    else:
                        task_row = task_rows[task_id]
                        self.conn.execute(
                            """
                            INSERT OR IGNORE INTO run_tasks(
                                run_id, task_id, ordinal, kind, input_json,
                                definition_json, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                target_run_id,
                                task_id,
                                task_row["ordinal"],
                                task_row["kind"],
                                task_row["input_json"],
                                task_row["definition_json"],
                                task_row["status"],
                            ),
                        )

                for task_id, task_row in task_rows.items():
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO run_tasks(
                            run_id, task_id, ordinal, kind, input_json,
                            definition_json, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_run_id,
                            task_id,
                            task_row["ordinal"],
                            task_row["kind"],
                            task_row["input_json"],
                            task_row["definition_json"],
                            task_row["status"],
                        ),
                    )

                used_numbers: dict[str, set[int]] = {}
                for existing in self.conn.execute(
                    "SELECT task_id, attempt_no FROM task_attempts WHERE run_id = ?",
                    (target_run_id,),
                ).fetchall():
                    used_numbers.setdefault(str(existing["task_id"]), set()).add(int(existing["attempt_no"]))

                for source_attempt in source_attempts:
                    source_attempt_id = str(source_attempt["attempt_id"])
                    task_id = str(source_attempt["task_id"])
                    target_attempt_id, is_new = self._claim_legacy_import_id(
                        source_key,
                        "attempt",
                        source_attempt_id,
                        table="task_attempts",
                        id_column="attempt_id",
                    )
                    attempt_ids[source_attempt_id] = target_attempt_id
                    existing = self.conn.execute(
                        "SELECT attempt_no FROM task_attempts WHERE attempt_id = ?",
                        (target_attempt_id,),
                    ).fetchone()
                    if existing is not None:
                        attempt_no = int(existing["attempt_no"])
                    else:
                        attempt_no = max(1, int(source_attempt["attempt_no"]))
                        used = used_numbers.setdefault(task_id, set())
                        while attempt_no in used:
                            attempt_no += 1
                        used.add(attempt_no)

                    self.conn.execute(
                        """
                        INSERT INTO task_attempts(
                            attempt_id, run_id, task_id, attempt_no, status,
                            harness, harness_version, external_session_id, pid,
                            exit_code, run_dir, started_at, started_epoch,
                            finished_at, finished_epoch, duration_ms, usage_json,
                            prompt_tokens, completion_tokens, total_tokens,
                            cached_tokens, reasoning_tokens, result_json, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(attempt_id) DO UPDATE SET
                            status = excluded.status,
                            harness = excluded.harness,
                            harness_version = excluded.harness_version,
                            external_session_id = excluded.external_session_id,
                            pid = excluded.pid,
                            exit_code = excluded.exit_code,
                            run_dir = excluded.run_dir,
                            started_at = excluded.started_at,
                            started_epoch = excluded.started_epoch,
                            finished_at = excluded.finished_at,
                            finished_epoch = excluded.finished_epoch,
                            duration_ms = excluded.duration_ms,
                            usage_json = excluded.usage_json,
                            prompt_tokens = excluded.prompt_tokens,
                            completion_tokens = excluded.completion_tokens,
                            total_tokens = excluded.total_tokens,
                            cached_tokens = excluded.cached_tokens,
                            reasoning_tokens = excluded.reasoning_tokens,
                            result_json = excluded.result_json,
                            error = excluded.error
                        """,
                        (
                            target_attempt_id,
                            target_run_id,
                            task_id,
                            attempt_no,
                            source_attempt["status"],
                            source_attempt["harness"],
                            source_attempt["harness_version"],
                            source_attempt["external_session_id"],
                            source_attempt["pid"],
                            source_attempt["exit_code"],
                            source_attempt["run_dir"],
                            source_attempt["started_at"],
                            source_attempt["started_epoch"],
                            source_attempt["finished_at"],
                            source_attempt["finished_epoch"],
                            source_attempt["duration_ms"],
                            source_attempt["usage_json"],
                            source_attempt["prompt_tokens"],
                            source_attempt["completion_tokens"],
                            source_attempt["total_tokens"],
                            source_attempt["cached_tokens"],
                            source_attempt["reasoning_tokens"],
                            source_attempt["result_json"],
                            source_attempt["error"],
                        ),
                    )
                    if is_new:
                        imported_attempts += 1

                for task_id, task_row in task_rows.items():
                    latest_source = str(task_row["latest_attempt_id"] or "")
                    latest_target = attempt_ids.get(latest_source, "")
                    actual_count = int(
                        self.conn.execute(
                            "SELECT COUNT(*) FROM task_attempts WHERE run_id = ? AND task_id = ?",
                            (target_run_id, task_id),
                        ).fetchone()[0]
                    )
                    self.conn.execute(
                        """
                        UPDATE run_tasks SET
                            ordinal = ?, kind = ?, input_json = ?, definition_json = ?,
                            status = ?, attempt_count = ?, latest_attempt_id = ?,
                            result_json = ?, error = ?, started_at = ?, started_epoch = ?,
                            finished_at = ?, finished_epoch = ?, duration_ms = ?,
                            prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
                            cached_tokens = ?, reasoning_tokens = ?
                        WHERE run_id = ? AND task_id = ?
                        """,
                        (
                            task_row["ordinal"],
                            task_row["kind"],
                            task_row["input_json"],
                            task_row["definition_json"],
                            task_row["status"],
                            actual_count,
                            latest_target,
                            task_row["result_json"],
                            task_row["error"],
                            task_row["started_at"],
                            task_row["started_epoch"],
                            task_row["finished_at"],
                            task_row["finished_epoch"],
                            task_row["duration_ms"],
                            task_row["prompt_tokens"],
                            task_row["completion_tokens"],
                            task_row["total_tokens"],
                            task_row["cached_tokens"],
                            task_row["reasoning_tokens"],
                            target_run_id,
                            task_id,
                        ),
                    )

            self._import_staged_children(staged, source_key, attempt_ids)
            self.conn.execute(
                """
                UPDATE legacy_import_sources
                SET source_fingerprint = ?, source_schema = ?, last_imported_at = ?
                WHERE source_path = ?
                """,
                (source_fingerprint, source_schema, now, source_key),
            )
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        return imported_attempts

    def _import_staged_children(
        self,
        staged: SessionStore,
        source_key: str,
        attempt_ids: Mapping[str, str],
    ) -> None:
        child_specs = (
            (
                "messages",
                "message",
                "seq, role, content, raw_json, created_at",
                lambda row: (
                    row["seq"],
                    row["role"],
                    row["content"],
                    row["raw_json"],
                    row["created_at"],
                ),
            ),
            (
                "tool_events",
                "tool_event",
                "seq, tool_name, arguments_json, result_json, error, created_at",
                lambda row: (
                    row["seq"],
                    row["tool_name"],
                    row["arguments_json"],
                    row["result_json"],
                    row["error"],
                    row["created_at"],
                ),
            ),
            (
                "artifacts",
                "artifact",
                "task_id, summary, artifact_path, metadata_json, created_at",
                lambda row: (
                    row["task_id"],
                    row["summary"],
                    row["artifact_path"],
                    row["metadata_json"],
                    row["created_at"],
                ),
            ),
            (
                "model_calls",
                "model_call",
                "seq, provider, model, latency_ms, usage_json, prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens, created_at",
                lambda row: (
                    row["seq"],
                    row["provider"],
                    row["model"],
                    row["latency_ms"],
                    row["usage_json"],
                    row["prompt_tokens"],
                    row["completion_tokens"],
                    row["total_tokens"],
                    row["cached_tokens"],
                    row["reasoning_tokens"],
                    row["created_at"],
                ),
            ),
        )
        for table, item_type, columns, values in child_specs:
            rows = staged.conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for row in rows:
                target_attempt = attempt_ids.get(str(row["attempt_id"]))
                if not target_attempt:
                    continue
                source_id = str(row["id"])
                if self._legacy_import_target(source_key, item_type, source_id) is not None:
                    continue
                prefix = (target_attempt,)
                row_values = values(row)
                placeholders = ", ".join("?" for _ in range(len(prefix) + len(row_values)))
                cursor = self.conn.execute(
                    f"INSERT INTO {table}(attempt_id, {columns}) VALUES ({placeholders})",
                    (*prefix, *row_values),
                )
                self._record_legacy_import_target(
                    source_key,
                    item_type,
                    source_id,
                    str(cursor.lastrowid),
                )

    def _claim_legacy_import_id(
        self,
        source_key: str,
        item_type: str,
        source_id: str,
        *,
        table: str,
        id_column: str,
    ) -> tuple[str, bool]:
        existing = self._legacy_import_target(source_key, item_type, source_id)
        if existing is not None:
            return existing, False

        candidate = source_id
        digest = hashlib.sha256(f"{source_key}\0{item_type}\0{source_id}".encode("utf-8")).hexdigest()
        suffix_length = 12
        while self._legacy_target_is_used(item_type, candidate, table, id_column):
            candidate = f"{source_id}-legacy-{digest[:suffix_length]}"
            suffix_length += 4
            if suffix_length > len(digest):
                candidate = f"{source_id}-legacy-{digest}-{suffix_length}"
        self._record_legacy_import_target(source_key, item_type, source_id, candidate)
        return candidate, True

    def _legacy_target_is_used(
        self,
        item_type: str,
        candidate: str,
        table: str,
        id_column: str,
    ) -> bool:
        mapped = self.conn.execute(
            "SELECT 1 FROM legacy_import_items WHERE item_type = ? AND target_id = ?",
            (item_type, candidate),
        ).fetchone()
        occupied = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_column} = ?",
            (candidate,),
        ).fetchone()
        return mapped is not None or occupied is not None

    def _legacy_import_target(self, source_key: str, item_type: str, source_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT target_id FROM legacy_import_items
            WHERE source_path = ? AND item_type = ? AND source_id = ?
            """,
            (source_key, item_type, source_id),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _record_legacy_import_target(
        self,
        source_key: str,
        item_type: str,
        source_id: str,
        target_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO legacy_import_items(
                source_path, item_type, source_id, target_id, imported_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (source_key, item_type, source_id, target_id, utc_now()),
        )

    def register_batch(self, manifest_path: str | Path, name: str, config_hash: str = "") -> str:
        path = _canonical_manifest_path(manifest_path)
        config_id = hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO batch_configs(config_id, manifest_path, name, config_hash, registered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(manifest_path) DO UPDATE SET
                name = excluded.name,
                config_hash = excluded.config_hash,
                updated_at = excluded.updated_at
            """,
            (config_id, path, name, config_hash, now, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT config_id FROM batch_configs WHERE manifest_path = ?", (path,)
        ).fetchone()
        assert row is not None
        return str(row[0])

    def start_batch_run(
        self,
        run_id: str,
        manifest_path: str | Path,
        batch_name: str,
        tasks: Iterable[Any],
        *,
        harness: str = "native",
        harness_version: str = "",
        run_vars: Mapping[str, Any] | None = None,
        config_snapshot: Mapping[str, Any] | None = None,
        selected_task_ids: Iterable[str] | None = None,
        config_hash: str = "",
        status: str = "running",
    ) -> None:
        if not run_id.strip():
            raise ValueError("run_id is required")
        path = _canonical_manifest_path(manifest_path)
        config_id = self.register_batch(path, batch_name, config_hash)
        task_defs = [_task_definition(task) for task in tasks]
        selected = list(selected_task_ids) if selected_task_ids is not None else [item["id"] for item in task_defs]
        selected_set = set(selected)
        now_text, now_epoch = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO batch_runs(
                    run_id, config_id, manifest_path, batch_name, status, harness,
                    harness_version, run_vars_json, config_snapshot_json,
                    selected_task_ids_json, started_at, started_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    config_id,
                    path,
                    batch_name,
                    status,
                    harness,
                    harness_version,
                    _json(dict(run_vars or {})),
                    _json(dict(config_snapshot or {})),
                    _json(selected),
                    now_text,
                    now_epoch,
                ),
            )
            for ordinal, definition in enumerate(task_defs):
                task_id = definition["id"]
                task_status = "queued" if task_id in selected_set else "skipped"
                self.conn.execute(
                    """
                    INSERT INTO run_tasks(
                        run_id, task_id, ordinal, kind, input_json,
                        definition_json, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        ordinal,
                        definition.get("kind", ""),
                        _json(definition.get("input", {})),
                        _json(definition),
                        task_status,
                    ),
                )

    def resume_batch_run(self, run_id: str) -> None:
        now_text, now_epoch = _now()
        with self.conn:
            changed = self.conn.execute(
                """
                UPDATE batch_runs
                SET status = 'running', finished_at = NULL, finished_epoch = NULL,
                    duration_ms = NULL, error = ''
                WHERE run_id = ? AND status IN ('paused', 'failed', 'interrupted')
                """,
                (run_id,),
            ).rowcount
            if not changed:
                raise ValueError(f"run is not resumable or does not exist: {run_id}")
            self._reconcile_run_model_usage(run_id)
            self.conn.execute(
                """
                UPDATE task_attempts
                SET status = 'interrupted', finished_at = ?, finished_epoch = ?,
                    duration_ms = CAST((? - started_epoch) * 1000 AS INTEGER),
                    error = CASE WHEN error = '' THEN 'interrupted before resume' ELSE error END
                WHERE run_id = ? AND status = 'running'
                """,
                (now_text, now_epoch, now_epoch, run_id),
            )
            self.conn.execute(
                """
                UPDATE run_tasks
                SET status = 'retry', error = CASE WHEN error = '' THEN 'interrupted before resume' ELSE error END
                WHERE run_id = ? AND status = 'running'
                """,
                (run_id,),
            )
            self._refresh_task_usage(run_id)
            self._refresh_run_usage(run_id)

    def interrupt_batch_run(self, run_id: str, reason: str = "recovered stale active Run") -> None:
        now_text, now_epoch = _now()
        with self.conn:
            changed = self.conn.execute(
                """
                UPDATE batch_runs
                SET status = 'interrupted', finished_at = ?, finished_epoch = ?,
                    duration_ms = CAST((? - started_epoch) * 1000 AS INTEGER),
                    error = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (now_text, now_epoch, now_epoch, reason, run_id),
            ).rowcount
            if not changed:
                raise ValueError(f"run is not active or does not exist: {run_id}")
            self._reconcile_run_model_usage(run_id)
            self.conn.execute(
                """
                UPDATE task_attempts
                SET status = 'interrupted', finished_at = ?, finished_epoch = ?,
                    duration_ms = CAST((? - started_epoch) * 1000 AS INTEGER),
                    error = CASE WHEN error = '' THEN ? ELSE error END
                WHERE run_id = ? AND status = 'running'
                """,
                (now_text, now_epoch, now_epoch, reason, run_id),
            )
            self.conn.execute(
                """
                UPDATE run_tasks
                SET status = 'retry', error = CASE WHEN error = '' THEN ? ELSE error END
                WHERE run_id = ? AND status = 'running'
                """,
                (reason, run_id),
            )
            self._refresh_task_usage(run_id)
            self._refresh_run_usage(run_id)

    def finish_batch_run(
        self,
        run_id: str,
        status: str,
        error: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> None:
        now_text, now_epoch = _now()
        with self.conn:
            self._reconcile_run_model_usage(run_id)
            self._refresh_task_usage(run_id)
            self._refresh_run_usage(run_id)
            changed = self.conn.execute(
                """
                UPDATE batch_runs
                SET status = ?, finished_at = ?, finished_epoch = ?,
                    duration_ms = CAST((? - started_epoch) * 1000 AS INTEGER),
                    error = ?, result_json = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (status, now_text, now_epoch, now_epoch, error, _json(dict(result or {})), run_id),
            ).rowcount
            if not changed:
                existing = self.conn.execute(
                    "SELECT status FROM batch_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"run not found: {run_id}")
                raise ValueError(f"run {run_id} is not active: status={existing['status']}")

    def pause_batch_run(self, run_id: str, error: str = "") -> None:
        self.finish_batch_run(run_id, "paused", error)

    def batch_runs(self, manifest_path: str | Path | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if manifest_path is not None:
            where = "WHERE manifest_path = ?"
            params.append(_canonical_manifest_path(manifest_path))
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT * FROM batch_runs
            {where}
            ORDER BY started_epoch DESC, rowid DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_batch_run_dict(row) for row in rows]

    def batch_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM batch_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _batch_run_dict(row) if row is not None else None

    def run_tasks(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM run_tasks WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()
        return [_run_task_dict(row) for row in rows]

    def run_task(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM run_tasks WHERE run_id = ? AND task_id = ?", (run_id, task_id)
        ).fetchone()
        return _run_task_dict(row) if row is not None else None

    def mark_run_task_retry(self, run_id: str, task_id: str) -> None:
        with self.conn:
            row = self.conn.execute(
                """
                SELECT tasks.status AS task_status, runs.status AS run_status
                FROM run_tasks tasks
                JOIN batch_runs runs ON runs.run_id = tasks.run_id
                WHERE tasks.run_id = ? AND tasks.task_id = ?
                """,
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"task not found in run {run_id}: {task_id}")
            if str(row["run_status"]) not in {"running", "paused", "failed", "interrupted"}:
                raise ValueError(f"run {run_id} is not retryable from status {row['run_status']}")
            if str(row["task_status"]) not in {"failed", "interrupted", "retry"}:
                raise ValueError(f"task {task_id} is not retryable from status {row['task_status']}")
            self.conn.execute(
                "UPDATE run_tasks SET status = 'retry', error = '' WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            )

    def update_run_task(
        self,
        run_id: str,
        task_id: str,
        status: str,
        *,
        error: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> None:
        with self.conn:
            changed = self.conn.execute(
                """
                UPDATE run_tasks
                SET status = ?, error = ?,
                    result_json = CASE WHEN ? IS NULL THEN result_json ELSE ? END
                WHERE run_id = ? AND task_id = ?
                """,
                (
                    status,
                    error,
                    None if result is None else 1,
                    _json(dict(result or {})),
                    run_id,
                    task_id,
                ),
            ).rowcount
            if not changed:
                raise ValueError(f"task not found in run {run_id}: {task_id}")

    def start_attempt(
        self,
        attempt_id: str,
        run_id: str,
        task_id: str,
        attempt_no: int,
        run_dir: Path,
        *,
        harness: str = "native",
        harness_version: str = "",
        pid: int | None = None,
    ) -> None:
        if not attempt_id.strip():
            raise ValueError("attempt_id is required")
        self._ensure_placeholder_run_task(run_id, task_id)
        now_text, now_epoch = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO task_attempts(
                    attempt_id, run_id, task_id, attempt_no, status, harness,
                    harness_version, pid, run_dir, started_at, started_epoch
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    run_id,
                    task_id,
                    attempt_no,
                    harness,
                    harness_version,
                    pid,
                    str(run_dir),
                    now_text,
                    now_epoch,
                ),
            )
            self.conn.execute(
                """
                UPDATE run_tasks
                SET status = 'running', attempt_count = attempt_count + 1,
                    latest_attempt_id = ?, started_at = COALESCE(started_at, ?),
                    started_epoch = COALESCE(started_epoch, ?),
                    finished_at = NULL, finished_epoch = NULL, duration_ms = NULL,
                    error = ''
                WHERE run_id = ? AND task_id = ?
                """,
                (attempt_id, now_text, now_epoch, run_id, task_id),
            )

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        error: str = "",
        *,
        result: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        external_session_id: str = "",
        exit_code: int | None = None,
        pid: int | None = None,
    ) -> None:
        if status not in {"done", "failed", "interrupted"}:
            raise ValueError(f"invalid terminal attempt status: {status}")
        now_text, now_epoch = _now()
        with self.conn:
            self._reconcile_attempt_model_usage(attempt_id)
            row = self.conn.execute(
                """
                SELECT run_id, task_id, status, usage_json, result_json
                FROM task_attempts WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"attempt not found: {attempt_id}")
            current_status = str(row["status"])
            if current_status != "running":
                # Retrying a successful database write must not double-count
                # usage or move timestamps. A conflicting transition remains
                # an error so historical terminal state cannot be rewritten.
                if current_status == status:
                    return
                raise ValueError(
                    f"attempt {attempt_id} is already {current_status}; cannot finish as {status}"
                )
            existing_usage = _loads(row["usage_json"], {})
            merged_usage = _merge_mappings(existing_usage, dict(usage or {})) if usage is not None else existing_usage
            tokens = _usage_tokens(merged_usage)
            result_value = dict(result) if result is not None else _loads(row["result_json"], {})
            result_json = _json(result_value)
            changed = self.conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, finished_epoch = ?,
                    duration_ms = CAST((? - started_epoch) * 1000 AS INTEGER),
                    error = ?, result_json = ?, usage_json = ?,
                    prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
                    cached_tokens = ?, reasoning_tokens = ?,
                    external_session_id = CASE WHEN ? = '' THEN external_session_id ELSE ? END,
                    exit_code = COALESCE(?, exit_code), pid = COALESCE(?, pid)
                WHERE attempt_id = ? AND status = 'running'
                """,
                (
                    status,
                    now_text,
                    now_epoch,
                    now_epoch,
                    error,
                    result_json,
                    _json(merged_usage),
                    tokens["prompt_tokens"],
                    tokens["completion_tokens"],
                    tokens["total_tokens"],
                    tokens["cached_tokens"],
                    tokens["reasoning_tokens"],
                    external_session_id,
                    external_session_id,
                    exit_code,
                    pid,
                    attempt_id,
                ),
            ).rowcount
            if not changed:
                raise ValueError(f"attempt is no longer running: {attempt_id}")
            task_status = "done" if status == "done" else ("retry" if status == "interrupted" else status)
            self.conn.execute(
                """
                UPDATE run_tasks
                SET status = ?, latest_attempt_id = ?, result_json = ?, error = ?,
                    finished_at = ?, finished_epoch = ?,
                    duration_ms = CAST((? - started_epoch) * 1000 AS INTEGER)
                WHERE run_id = ? AND task_id = ?
                """,
                (
                    task_status,
                    attempt_id,
                    result_json,
                    error,
                    now_text,
                    now_epoch,
                    now_epoch,
                    str(row["run_id"]),
                    str(row["task_id"]),
                ),
            )
            self._refresh_task_usage(str(row["run_id"]), str(row["task_id"]))
            self._refresh_run_usage(str(row["run_id"]))

    def update_attempt_process(
        self,
        attempt_id: str,
        *,
        pid: int | None = None,
        external_session_id: str = "",
        exit_code: int | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE task_attempts SET
                    pid = COALESCE(?, pid),
                    external_session_id = CASE WHEN ? = '' THEN external_session_id ELSE ? END,
                    exit_code = COALESCE(?, exit_code)
                WHERE attempt_id = ?
                """,
                (pid, external_session_id, external_session_id, exit_code, attempt_id),
            )

    def update_attempt_usage(self, attempt_id: str, usage: Mapping[str, Any]) -> None:
        """Persist cumulative/partial usage while an Attempt is still active."""

        with self.conn:
            row = self.conn.execute(
                "SELECT run_id, task_id, status, usage_json FROM task_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"attempt not found: {attempt_id}")
            if str(row["status"]) != "running":
                return
            merged = _merge_mappings(_loads(row["usage_json"], {}), dict(usage))
            tokens = _usage_tokens(merged)
            self.conn.execute(
                """
                UPDATE task_attempts SET
                    usage_json = ?, prompt_tokens = ?, completion_tokens = ?,
                    total_tokens = ?, cached_tokens = ?, reasoning_tokens = ?
                WHERE attempt_id = ?
                """,
                (
                    _json(merged),
                    tokens["prompt_tokens"],
                    tokens["completion_tokens"],
                    tokens["total_tokens"],
                    tokens["cached_tokens"],
                    tokens["reasoning_tokens"],
                    attempt_id,
                ),
            )
            self._refresh_task_usage(str(row["run_id"]), str(row["task_id"]))
            self._refresh_run_usage(str(row["run_id"]))

    def task_attempts(self, run_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self.conn.execute(
                "SELECT * FROM task_attempts WHERE run_id = ? ORDER BY started_epoch DESC, rowid DESC",
                (run_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM task_attempts
                WHERE run_id = ? AND task_id = ?
                ORDER BY started_epoch DESC, rowid DESC
                """,
                (run_id, task_id),
            ).fetchall()
        return [_attempt_dict(row) for row in rows]

    def attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM task_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return _attempt_dict(row) if row is not None else None

    def add_model_call(
        self,
        attempt_id: str,
        seq: int,
        provider: str,
        model: str,
        usage: Mapping[str, Any] | None,
        *,
        latency_ms: int | None = None,
    ) -> None:
        values = dict(usage or {})
        tokens = _usage_tokens(values)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO model_calls(
                    attempt_id, seq, provider, model, latency_ms, usage_json,
                    prompt_tokens, completion_tokens, total_tokens,
                    cached_tokens, reasoning_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    seq,
                    provider,
                    model,
                    latency_ms,
                    _json(values),
                    tokens["prompt_tokens"],
                    tokens["completion_tokens"],
                    tokens["total_tokens"],
                    tokens["cached_tokens"],
                    tokens["reasoning_tokens"],
                    utc_now(),
                ),
            )
            self._reconcile_attempt_model_usage(attempt_id)
            row = self.conn.execute(
                "SELECT run_id, task_id FROM task_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                self._refresh_task_usage(str(row["run_id"]), str(row["task_id"]))
                self._refresh_run_usage(str(row["run_id"]))

    def model_calls(self, attempt_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_calls WHERE attempt_id = ? ORDER BY seq, id", (attempt_id,)
        ).fetchall()
        return [
            {
                **dict(row),
                "usage": _loads(row["usage_json"], {}),
            }
            for row in rows
        ]

    # Compatibility API: old run_id == new attempt_id; old work_id == new run_id.
    def start_run(self, run_id: str, task_id: str, attempt: int, run_dir: Path, work_id: str = "") -> None:
        parent_run_id = work_id or f"run-{run_id}"
        self._ensure_placeholder_run_task(parent_run_id, task_id)
        self.start_attempt(run_id, parent_run_id, task_id, attempt, run_dir)

    def finish_run(self, run_id: str, status: str, error: str = "") -> None:
        self.finish_attempt(run_id, status, error)

    def add_message(self, run_id: str, seq: int, role: str, content: str | None, raw: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO messages(attempt_id, seq, role, content, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, role, content, _json(raw or {}), utc_now()),
        )
        self.conn.commit()

    def add_tool_event(
        self,
        run_id: str,
        seq: int,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO tool_events(attempt_id, seq, tool_name, arguments_json, result_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, tool_name, _json(arguments), _json(result or {}), error, utc_now()),
        )
        self.conn.commit()

    def add_artifact(self, run_id: str, task_id: str, summary: str, artifact_path: str, metadata: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO artifacts(attempt_id, task_id, summary, artifact_path, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, summary, artifact_path, _json(metadata), utc_now()),
        )
        self.conn.commit()

    def recent_failures(
        self,
        task_id: str,
        limit: int = 3,
        *,
        manifest_path: str | Path | None = None,
        run_id: str | None = None,
    ) -> list[str]:
        clauses = ["a.task_id = ?", "a.status = 'failed'", "a.error != ''"]
        params: list[Any] = [task_id]
        if run_id is not None:
            clauses.append("a.run_id = ?")
            params.append(run_id)
        if manifest_path is not None:
            clauses.append("r.manifest_path = ?")
            params.append(_canonical_manifest_path(manifest_path))
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT a.attempt_no, a.error FROM task_attempts a
            JOIN batch_runs r ON r.run_id = a.run_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.started_epoch DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [f"attempt {row['attempt_no']}: {row['error']}" for row in rows]

    def task_runs(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM task_attempts
            WHERE task_id = ?
            ORDER BY started_epoch DESC, rowid DESC
            """,
            (task_id,),
        ).fetchall()
        return [_compat_attempt_dict(row) for row in rows]

    def all_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM task_attempts ORDER BY started_epoch DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_compat_attempt_dict(row) for row in rows]

    def run_messages(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT seq, role, content, raw_json, created_at FROM messages
            WHERE attempt_id = ? ORDER BY seq DESC, id DESC LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        rows = list(reversed(rows))
        return [
            {
                "seq": row["seq"],
                "role": row["role"],
                "content": row["content"] or "",
                "raw": _loads(row["raw_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def run_tool_events(self, run_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT seq, tool_name, arguments_json, result_json, error, created_at FROM tool_events
            WHERE attempt_id = ? ORDER BY seq DESC, id DESC LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        rows = list(reversed(rows))
        return [
            {
                "seq": row["seq"],
                "tool_name": row["tool_name"],
                "arguments": _loads(row["arguments_json"], {}),
                "result": _loads(row["result_json"], {}),
                "error": row["error"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT summary, artifact_path, metadata_json, created_at FROM artifacts
            WHERE attempt_id = ? ORDER BY created_at DESC, id DESC
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                "summary": row["summary"],
                "artifact_path": row["artifact_path"] or "",
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _ensure_placeholder_run_task(self, run_id: str, task_id: str) -> None:
        if self.batch_run(run_id) is None:
            source = f"compat://{self.path.resolve(strict=False)}"
            config_id = self.register_batch(source, "Compatibility run")
            now_text, now_epoch = _now()
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO batch_runs(
                        run_id, config_id, manifest_path, batch_name, status,
                        harness, selected_task_ids_json, started_at, started_epoch,
                        metadata_incomplete
                    ) VALUES (?, ?, ?, 'Compatibility run', 'running', 'native', ?, ?, ?, 1)
                    """,
                    (run_id, config_id, source, _json([task_id]), now_text, now_epoch),
                )
        if self.run_task(run_id, task_id) is None:
            ordinal_row = self.conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM run_tasks WHERE run_id = ?", (run_id,)
            ).fetchone()
            ordinal = int(ordinal_row[0]) if ordinal_row else 0
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO run_tasks(run_id, task_id, ordinal, status, definition_json)
                    VALUES (?, ?, ?, 'queued', ?)
                    """,
                    (run_id, task_id, ordinal, _json({"id": task_id})),
                )

    def _reconcile_attempt_model_usage(self, attempt_id: str) -> None:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS call_count,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(cached_tokens) AS cached_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens
            FROM model_calls WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None or int(row["call_count"] or 0) == 0:
            return
        attempt = self.conn.execute(
            "SELECT usage_json FROM task_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            return
        aggregate = {
            key: row[key]
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_tokens",
                "reasoning_tokens",
            )
            if row[key] is not None
        }
        merged = _merge_mappings(_loads(attempt["usage_json"], {}), aggregate)
        tokens = _usage_tokens(merged)
        self.conn.execute(
            """
            UPDATE task_attempts SET
                usage_json = ?, prompt_tokens = ?, completion_tokens = ?,
                total_tokens = ?, cached_tokens = ?, reasoning_tokens = ?
            WHERE attempt_id = ?
            """,
            (
                _json(merged),
                tokens["prompt_tokens"],
                tokens["completion_tokens"],
                tokens["total_tokens"],
                tokens["cached_tokens"],
                tokens["reasoning_tokens"],
                attempt_id,
            ),
        )

    def _reconcile_run_model_usage(self, run_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT DISTINCT calls.attempt_id
            FROM model_calls calls
            JOIN task_attempts attempts ON attempts.attempt_id = calls.attempt_id
            WHERE attempts.run_id = ?
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            self._reconcile_attempt_model_usage(str(row["attempt_id"]))

    def _refresh_task_usage(self, run_id: str, task_id: str | None = None) -> None:
        params: list[Any] = [run_id]
        task_filter = ""
        if task_id is not None:
            task_filter = " AND task_id = ?"
            params.append(task_id)
        self.conn.execute(
            f"""
            UPDATE run_tasks SET
                prompt_tokens = (
                    SELECT SUM(a.prompt_tokens) FROM task_attempts a
                    WHERE a.run_id = run_tasks.run_id AND a.task_id = run_tasks.task_id
                ),
                completion_tokens = (
                    SELECT SUM(a.completion_tokens) FROM task_attempts a
                    WHERE a.run_id = run_tasks.run_id AND a.task_id = run_tasks.task_id
                ),
                total_tokens = (
                    SELECT SUM(a.total_tokens) FROM task_attempts a
                    WHERE a.run_id = run_tasks.run_id AND a.task_id = run_tasks.task_id
                ),
                cached_tokens = (
                    SELECT SUM(a.cached_tokens) FROM task_attempts a
                    WHERE a.run_id = run_tasks.run_id AND a.task_id = run_tasks.task_id
                ),
                reasoning_tokens = (
                    SELECT SUM(a.reasoning_tokens) FROM task_attempts a
                    WHERE a.run_id = run_tasks.run_id AND a.task_id = run_tasks.task_id
                )
            WHERE run_id = ?{task_filter}
            """,
            params,
        )

    def _refresh_run_usage(self, run_id: str) -> None:
        row = self.conn.execute(
            """
            SELECT
                SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens),
                SUM(cached_tokens), SUM(reasoning_tokens)
            FROM task_attempts WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        self.conn.execute(
            """
            UPDATE batch_runs SET
                prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
                cached_tokens = ?, reasoning_tokens = ?
            WHERE run_id = ?
            """,
            (*row, run_id),
        )


def _task_definition(task: Any) -> dict[str, Any]:
    if isinstance(task, Mapping):
        value = dict(task)
    else:
        value = {
            "id": getattr(task, "id", ""),
            "kind": getattr(task, "kind", ""),
            "input": getattr(task, "input", {}),
        }
    task_id = str(value.get("id") or "").strip()
    if not task_id:
        raise ValueError("task id is required in run snapshot")
    value["id"] = task_id
    value["kind"] = str(value.get("kind") or "")
    value["input"] = dict(value.get("input") or {})
    return value


def _batch_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["run_vars"] = _loads(value.pop("run_vars_json"), {})
    value["config_snapshot"] = _loads(value.pop("config_snapshot_json"), {})
    value["selected_task_ids"] = _loads(value.pop("selected_task_ids_json"), [])
    value["result"] = _loads(value.pop("result_json"), {})
    value["metadata_incomplete"] = bool(value["metadata_incomplete"])
    value["elapsed_seconds"] = _elapsed_seconds(value["started_epoch"], value["finished_epoch"], value["duration_ms"])
    return value


def _run_task_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["input"] = _loads(value.pop("input_json"), {})
    value["definition"] = _loads(value.pop("definition_json"), {})
    value["result"] = _loads(value.pop("result_json"), {})
    value["elapsed_seconds"] = _elapsed_seconds(value["started_epoch"], value["finished_epoch"], value["duration_ms"])
    return value


def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["usage"] = _loads(value.pop("usage_json"), {})
    value["result"] = _loads(value.pop("result_json"), {})
    value["elapsed_seconds"] = _elapsed_seconds(value["started_epoch"], value["finished_epoch"], value["duration_ms"])
    return value


def _compat_attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
    attempt = _attempt_dict(row)
    return {
        **attempt,
        "attempt_id": attempt["attempt_id"],
        "batch_run_id": attempt["run_id"],
        "work_id": attempt["run_id"],
        "run_id": attempt["attempt_id"],
        "attempt": attempt["attempt_no"],
    }


def _usage_tokens(usage: Mapping[str, Any]) -> dict[str, int | None]:
    tokens = usage.get("tokens") if isinstance(usage.get("tokens"), Mapping) else {}
    prompt = _first_int(usage, "prompt_tokens", "input_tokens")
    completion = _first_int(usage, "completion_tokens", "output_tokens")
    total = _first_int(usage, "total_tokens")
    if prompt is None:
        prompt = _first_int(tokens, "prompt_tokens", "input_tokens", "input")
    if completion is None:
        completion = _first_int(tokens, "completion_tokens", "output_tokens", "output")
    if total is None:
        total = _first_int(tokens, "total_tokens", "total")
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), Mapping) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), Mapping) else {}
    cached = _first_int(usage, "cached_tokens", "cached_input_tokens", "cache_read_input_tokens")
    if cached is None:
        cached = _first_int(prompt_details, "cached_tokens")
    reasoning = _first_int(usage, "reasoning_tokens")
    if reasoning is None:
        reasoning = _first_int(completion_details, "reasoning_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def _first_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _canonical_manifest_path(path: str | Path) -> str:
    value = str(path)
    if "://" in value:
        return value
    return str(Path(path).expanduser().resolve(strict=False))


def _sqlite_fingerprint(path: Path) -> str:
    """Fingerprint the database and WAL metadata used by a consistent snapshot."""
    parts: list[str] = []
    for candidate in (path, Path(f"{path}-wal")):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            parts.append(f"{candidate.name}:missing")
        else:
            parts.append(f"{candidate.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _now() -> tuple[str, float]:
    return utc_now(), time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _merge_mappings(base: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = {str(key): value for key, value in base.items()}
    for raw_key, value in incoming.items():
        key = str(raw_key)
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mappings(existing, value)
        elif isinstance(value, Mapping):
            merged[key] = _merge_mappings({}, value)
        else:
            merged[key] = value
    return merged


def _parse_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _duration_ms(started: float | None, finished: float | None) -> int | None:
    if started is None or finished is None:
        return None
    return max(0, int((finished - started) * 1000))


def _elapsed_seconds(started: float | None, finished: float | None, duration_ms: int | None) -> float:
    if duration_ms is not None:
        return max(0.0, duration_ms / 1000)
    if started is None:
        return 0.0
    return max(0.0, (finished or time.time()) - started)
