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

    def test_parses_run_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "tools = [\"write_file\", \"submit_artifact\"]",
                "tools = [\"write_file\", \"submit_artifact\"]\nrun_variables = [{ name = \"market\", label = \"Market scope\", required = true }]",
            )
            path.write_text(text, encoding="utf-8")
            manifest = load_manifest(path)
            self.assertEqual(len(manifest.config.run_variables), 1)
            self.assertEqual(manifest.config.run_variables[0].name, "market")
            self.assertEqual(manifest.config.run_variables[0].label, "Market scope")

    def test_parses_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "tools = [\"write_file\", \"submit_artifact\"]",
                "tools = [\"write_file\", \"submit_artifact\"]\nskills = [\"patch-compatibility-analyzer\"]\nskill_roots = [\"skills\"]",
            )
            path.write_text(text, encoding="utf-8")
            manifest = load_manifest(path)
            self.assertEqual(manifest.config.skills, ["patch-compatibility-analyzer"])
            self.assertEqual(manifest.config.skill_roots, ["skills"])


if __name__ == "__main__":
    unittest.main()
