from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import utc_now


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                run_dir TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS tool_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                artifact_path TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            """
        )
        self.conn.commit()

    def start_run(self, run_id: str, task_id: str, attempt: int, run_dir: Path) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO runs(run_id, task_id, attempt, status, run_dir, started_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (run_id, task_id, attempt, str(run_dir), utc_now()),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, status: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE run_id = ?",
            (status, utc_now(), error, run_id),
        )
        self.conn.commit()

    def add_message(self, run_id: str, seq: int, role: str, content: str | None, raw: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO messages(run_id, seq, role, content, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, role, content, json.dumps(raw or {}, ensure_ascii=False), utc_now()),
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
            INSERT INTO tool_events(run_id, seq, tool_name, arguments_json, result_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                tool_name,
                json.dumps(arguments, ensure_ascii=False),
                json.dumps(result or {}, ensure_ascii=False),
                error,
                utc_now(),
            ),
        )
        self.conn.commit()

    def add_artifact(self, run_id: str, task_id: str, summary: str, artifact_path: str, metadata: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO artifacts(run_id, task_id, summary, artifact_path, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, summary, artifact_path, json.dumps(metadata, ensure_ascii=False), utc_now()),
        )
        self.conn.commit()

    def recent_failures(self, task_id: str, limit: int = 3) -> list[str]:
        cursor = self.conn.execute(
            """
            SELECT attempt, error FROM runs
            WHERE task_id = ? AND status = 'failed' AND error IS NOT NULL AND error != ''
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (task_id, limit),
        )
        return [f"attempt {attempt}: {error}" for attempt, error in cursor.fetchall()]

    def task_runs(self, task_id: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT run_id, task_id, attempt, status, run_dir, started_at, finished_at, error
            FROM runs
            WHERE task_id = ?
            ORDER BY started_at DESC, rowid DESC
            """,
            (task_id,),
        )
        return [
            {
                "run_id": run_id,
                "task_id": task_id,
                "attempt": attempt,
                "status": status,
                "run_dir": run_dir,
                "started_at": started_at,
                "finished_at": finished_at,
                "error": error or "",
            }
            for run_id, task_id, attempt, status, run_dir, started_at, finished_at, error in cursor.fetchall()
        ]

    def all_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT run_id, task_id, attempt, status, run_dir, started_at, finished_at, error
            FROM runs
            ORDER BY started_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "run_id": run_id,
                "task_id": task_id,
                "attempt": attempt,
                "status": status,
                "run_dir": run_dir,
                "started_at": started_at,
                "finished_at": finished_at,
                "error": error or "",
            }
            for run_id, task_id, attempt, status, run_dir, started_at, finished_at, error in cursor.fetchall()
        ]

    def run_messages(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT seq, role, content, raw_json, created_at
            FROM messages
            WHERE run_id = ?
            ORDER BY seq DESC
            LIMIT ?
            """,
            (run_id, limit),
        )
        rows = cursor.fetchall()
        rows.reverse()
        return [
            {
                "seq": seq,
                "role": role,
                "content": content or "",
                "raw": json.loads(raw_json or "{}"),
                "created_at": created_at,
            }
            for seq, role, content, raw_json, created_at in rows
        ]

    def run_tool_events(self, run_id: str, limit: int = 30) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT seq, tool_name, arguments_json, result_json, error, created_at
            FROM tool_events
            WHERE run_id = ?
            ORDER BY seq DESC
            LIMIT ?
            """,
            (run_id, limit),
        )
        rows = cursor.fetchall()
        rows.reverse()
        return [
            {
                "seq": seq,
                "tool_name": tool_name,
                "arguments": json.loads(arguments_json or "{}"),
                "result": json.loads(result_json or "{}"),
                "error": error or "",
                "created_at": created_at,
            }
            for seq, tool_name, arguments_json, result_json, error, created_at in rows
        ]

    def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT summary, artifact_path, metadata_json, created_at
            FROM artifacts
            WHERE run_id = ?
            ORDER BY created_at DESC
            """,
            (run_id,),
        )
        return [
            {
                "summary": summary,
                "artifact_path": artifact_path or "",
                "metadata": json.loads(metadata_json or "{}"),
                "created_at": created_at,
            }
            for summary, artifact_path, metadata_json, created_at in cursor.fetchall()
        ]
