from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from textual.widgets import Input

from batchagent.manifest import create_sample_manifest
from batchagent.tui import BatchAgentTui


class TuiTests(unittest.TestCase):
    def test_completion_candidates_include_commands_manifests_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                app = BatchAgentTui()
                app.discover_manifests()
                app.load_manifest_by_token("1")

                self.assertIn("/show_batch", app.completion_candidates("/sho"))
                self.assertIn("1", app.completion_candidates("/run "))
                self.assertIn("BATCHAGENT.md", app.completion_candidates("/show_batch B"))
                self.assertIn("demo", app.completion_candidates("/show_batch d"))
                self.assertIn("demo-1", app.completion_candidates("/show_task demo"))
                self.assertIn("demo-2", app.completion_candidates("/run --only demo"))
                self.assertIn("all", app.completion_candidates("/history "))
            finally:
                os.chdir(previous)

    def test_tui_loads_manifest_and_switches_batch_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")

                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)):
                        self.assertEqual(len(app.manifest_paths), 1)
                        await app.handle_command("/show_batch 1")
                        self.assertEqual(app.page, "batch")
                        self.assertIsNotNone(app.selected_manifest)
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_tab_completes_inside_command_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")

                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)) as pilot:
                        command = app.query_one("#command", Input)
                        command.focus()
                        command.value = "/sho"
                        await pilot.press("tab")
                        self.assertEqual(command.value, "/show_batch")
                        self.assertTrue(command.has_focus)
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_command_palette_describes_slash_commands(self) -> None:
        app = BatchAgentTui()
        lines = app.command_palette_lines("/")
        self.assertTrue(any("/history [task-id|all]" in line for line in lines))
        self.assertTrue(any("/run [number|path|name]" in line for line in lines))

    def test_history_rows_do_not_create_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                app = BatchAgentTui()
                app.discover_manifests()
                app.load_manifest_by_token("1")

                self.assertEqual(app.history_rows(), [])
                self.assertFalse((Path(tmp) / ".batchagent").exists())
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
