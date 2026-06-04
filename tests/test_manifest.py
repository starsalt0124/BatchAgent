from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from batchagent.manifest import create_sample_manifest, load_manifest, save_manifest


class ManifestTests(unittest.TestCase):
    def test_sample_manifest_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            manifest = load_manifest(path)
            self.assertEqual(manifest.config.provider, "deepseek")
            self.assertEqual(manifest.config.base_url, "https://api.deepseek.com")
            self.assertEqual(len(manifest.tasks), 2)
            manifest.tasks[0].status = "done"
            manifest.tasks[0].lease = ""
            manifest.tasks[0].result = ".batchagent/runs/demo/artifact.json"
            save_manifest(manifest)
            reloaded = load_manifest(path)
            self.assertEqual(reloaded.tasks[0].status, "done")
            self.assertEqual(reloaded.tasks[0].result, ".batchagent/runs/demo/artifact.json")

    def test_windows_path_cell_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            manifest = load_manifest(path)
            manifest.tasks[0].input = {"repo": "D:\\pcia_skill\\repo\\git", "patch": "a|b.patch"}
            save_manifest(manifest)
            reloaded = load_manifest(path)
            self.assertEqual(reloaded.tasks[0].input["repo"], "D:\\pcia_skill\\repo\\git")
            self.assertEqual(reloaded.tasks[0].input["patch"], "a|b.patch")


if __name__ == "__main__":
    unittest.main()

