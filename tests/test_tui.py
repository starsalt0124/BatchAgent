from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from batchagent.manifest import create_sample_manifest
from batchagent.tui import BatchAgentTui


class TuiTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

