from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from batchagent.agent import build_task_prompt
from batchagent.manifest import create_sample_manifest, load_manifest, save_manifest
from batchagent.store import SessionStore


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

    def test_can_omit_batchagent_protocol_from_external_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                'name = "demo"',
                'name = "demo"\ninject_batchagent_protocol = false',
            ).replace(
                'system_prompt = """\nYou are a batch task agent. Complete exactly one assigned task.\nUse submit_artifact when the task is complete. For this demo, write a short result file under outputs/.\n"""',
                'system_prompt = ""',
            ).replace(
                'user_prompt_template = """\nTask id: {{task.id}}\nTask kind: {{task.kind}}\nTask input: {{task.input}}\n\nIf kind is echo, write outputs/{{task.id}}.txt containing the input message, then call submit_artifact with artifact_path set to outputs/{{task.id}}.txt and metadata containing task_id.\n"""',
                'user_prompt_template = "Analyze {{task.id}}."',
            )
            path.write_text(text, encoding="utf-8")
            manifest = load_manifest(path)
            self.assertFalse(manifest.config.inject_batchagent_protocol)

            store = SessionStore(Path(tmp) / "state.sqlite3")
            try:
                prompt = build_task_prompt(
                    manifest.config,
                    manifest.tasks[0],
                    store,
                    Path(tmp),
                    path,
                )
            finally:
                store.close()
            self.assertEqual(prompt, "Analyze demo-1.")
            self.assertNotIn("BatchAgent harness protocol", prompt)


if __name__ == "__main__":
    unittest.main()
