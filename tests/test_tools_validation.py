from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from batchagent.models import ArtifactSubmission, BatchConfig, Task
from batchagent.tools import ToolContext, ToolError, invoke_tool
from batchagent.validation import ArtifactValidationError, validate_artifact


class ToolsValidationTests(unittest.TestCase):
    def test_write_file_blocks_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            run_dir = Path(tmp) / "run"
            workspace.mkdir()
            ctx = ToolContext(BatchConfig(), Task(status="todo", id="t1"), workspace, run_dir)
            with self.assertRaises(ValueError):
                invoke_tool(ctx, "write_file", {"path": "../outside.txt", "content": "x"})

    def test_read_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            run_dir = Path(tmp) / "run"
            workspace.mkdir()
            ctx = ToolContext(BatchConfig(), Task(status="todo", id="t1"), workspace, run_dir)
            invoke_tool(ctx, "write_file", {"path": "a/b.txt", "content": "hello"})
            result = invoke_tool(ctx, "read_file", {"path": "a/b.txt"})
            self.assertEqual(result["content"], "hello")

    def test_run_command_requires_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ctx = ToolContext(BatchConfig(), Task(status="todo", id="t1"), workspace, workspace / "run")
            with self.assertRaises(ToolError):
                invoke_tool(ctx, "run_command", {"command": ["python", "--version"]})

    def test_artifact_requires_metadata_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            task = Task(status="todo", id="t1")
            config = BatchConfig()
            config.artifact.required_metadata_keys = ["task_id"]
            artifact = ArtifactSubmission(summary="done", metadata={})
            with self.assertRaises(ArtifactValidationError):
                validate_artifact(config, task, workspace, workspace / "run", artifact)


if __name__ == "__main__":
    unittest.main()

