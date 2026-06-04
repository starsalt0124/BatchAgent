from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from batchagent.models import ArtifactSubmission, BatchConfig, Task
from batchagent.tools import ToolContext, ToolError, invoke_tool, tool_specs
from batchagent.validation import ArtifactValidationError, validate_artifact


class ToolsValidationTests(unittest.TestCase):
    def test_write_file_blocks_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            run_dir = Path(tmp) / "run"
            workspace.mkdir()
            config = BatchConfig(tools=["write_file"])
            ctx = ToolContext(config, Task(status="todo", id="t1"), workspace, run_dir)
            with self.assertRaises(ValueError):
                invoke_tool(ctx, "write_file", {"path": "../outside.txt", "content": "x"})

    def test_read_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            run_dir = Path(tmp) / "run"
            workspace.mkdir()
            config = BatchConfig(tools=["write_file", "read_file"])
            ctx = ToolContext(config, Task(status="todo", id="t1"), workspace, run_dir)
            invoke_tool(ctx, "write_file", {"path": "a/b.txt", "content": "hello"})
            result = invoke_tool(ctx, "read_file", {"path": "a/b.txt"})
            self.assertEqual(result["content"], "hello")

    def test_run_command_requires_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            config = BatchConfig(tools=["run_command"])
            ctx = ToolContext(config, Task(status="todo", id="t1"), workspace, workspace / "run")
            with self.assertRaises(ToolError):
                invoke_tool(ctx, "run_command", {"command": ["python", "--version"]})

    def test_no_tools_loaded_by_default(self) -> None:
        self.assertEqual(tool_specs(BatchConfig()), [])

    def test_delete_only_task_created_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            config = BatchConfig(tools=["write_file", "delete_file"])
            ctx = ToolContext(config, Task(status="todo", id="t1"), workspace, workspace / "run")
            existing = workspace / "existing.txt"
            existing.write_text("keep", encoding="utf-8")
            with self.assertRaises(ToolError):
                invoke_tool(ctx, "delete_file", {"path": "existing.txt"})
            invoke_tool(ctx, "write_file", {"path": "created.txt", "content": "delete me"})
            result = invoke_tool(ctx, "delete_file", {"path": "created.txt"})
            self.assertTrue(result["deleted"])
            self.assertFalse((workspace / "created.txt").exists())

    def test_run_command_rejects_delete_even_if_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            config = BatchConfig(tools=["run_command"], allowed_command_prefixes=[["rm"]])
            ctx = ToolContext(config, Task(status="todo", id="t1"), workspace, workspace / "run")
            with self.assertRaises(ToolError):
                invoke_tool(ctx, "run_command", {"command": ["rm", "x.txt"]})

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
