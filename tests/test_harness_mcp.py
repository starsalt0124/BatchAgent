from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from batchagent.harness_mcp import (
    HarnessMcpError,
    HarnessMcpServer,
    HarnessSpool,
    SpoolIdentity,
    load_progress_events,
    load_submission,
    serve,
)


class HarnessMcpTests(unittest.TestCase):
    def test_json_rpc_tools_write_nonce_scoped_atomic_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "attempt"
            spool_dir = run_dir / "harness-ipc"
            identity = SpoolIdentity("nonce-1", "run-1", "attempt-1", "task-1")
            server = HarnessMcpServer(HarnessSpool(spool_dir, identity, run_dir=run_dir))
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "report_progress",
                        "arguments": {"message": "half way", "percent": 50, "metadata": {"phase": "test"}},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "submit_artifact",
                        "arguments": {"summary": "done", "artifact_path": "out.txt", "metadata": {"ok": True}},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "submit_artifact",
                        "arguments": {"summary": "again", "metadata": {}},
                    },
                },
            ]
            stdin = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
            stdout = io.StringIO()

            serve(server, stdin, stdout)

            responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "bagent")
            self.assertEqual({tool["name"] for tool in responses[1]["result"]["tools"]}, {"submit_artifact", "report_progress"})
            self.assertFalse(responses[2]["result"]["isError"])
            self.assertFalse(responses[3]["result"]["isError"])
            self.assertTrue(responses[4]["result"]["isError"])

            submission = load_submission(
                spool_dir,
                nonce="nonce-1",
                run_id="run-1",
                attempt_id="attempt-1",
                task_id="task-1",
            )
            self.assertIsNotNone(submission)
            self.assertEqual(submission.summary, "done")
            self.assertEqual(submission.artifact_path, "out.txt")
            events = load_progress_events(
                spool_dir,
                nonce="nonce-1",
                run_id="run-1",
                attempt_id="attempt-1",
                task_id="task-1",
            )
            self.assertEqual(events[0][1]["message"], "half way")

            with self.assertRaises(HarnessMcpError):
                load_submission(
                    spool_dir,
                    nonce="wrong",
                    run_id="run-1",
                    attempt_id="attempt-1",
                    task_id="task-1",
                )

    def test_spool_must_stay_inside_attempt_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(HarnessMcpError):
                HarnessSpool(
                    root / "outside",
                    SpoolIdentity("nonce", "run", "attempt", "task"),
                    run_dir=root / "run",
                )


if __name__ == "__main__":
    unittest.main()
