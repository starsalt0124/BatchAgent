from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from batchagent.models import BatchConfig
from batchagent.skills import install_skill, installed_skills_dir, list_installed_skills, render_skills_prompt, resolve_skill


class SkillTests(unittest.TestCase):
    def test_default_install_root_uses_bagent_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"BAGENT_HOME": tmp}):
            self.assertEqual(installed_skills_dir(), Path(tmp) / "skills")

    def test_legacy_xdg_skills_remain_readable_and_new_skills_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            legacy_config = root / "legacy-config"
            for base, text in ((current / "skills", "new"), (legacy_config / "batchagent" / "skills", "old")):
                skill = base / "demo"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(text, encoding="utf-8")
            legacy_only = legacy_config / "batchagent" / "skills" / "legacy-only"
            legacy_only.mkdir()
            (legacy_only / "SKILL.md").write_text("legacy", encoding="utf-8")

            with patch.dict("os.environ", {"BAGENT_HOME": str(current), "XDG_CONFIG_HOME": str(legacy_config)}):
                rows = list_installed_skills()
                self.assertEqual([row["name"] for row in rows], ["demo", "legacy-only"])
                self.assertEqual(Path(rows[0]["path"]), current / "skills" / "demo")
                config = BatchConfig(skills=["legacy-only"])
                self.assertEqual(
                    resolve_skill("legacy-only", config, root / "BATCHAGENT.md"),
                    legacy_config / "batchagent" / "skills" / "legacy-only",
                )

    def test_install_and_resolve_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source-skill"
            source.mkdir()
            (source / "SKILL.md").write_text("# Source Skill\n\nUse me.\n", encoding="utf-8")
            install_root = root / "installed"

            target = install_skill(source, name="demo", root=install_root)

            self.assertEqual(target, install_root / "demo")
            self.assertEqual(list_installed_skills(install_root)[0]["name"], "demo")
            config = BatchConfig(skills=["demo"], skill_roots=[str(install_root)])
            self.assertEqual(resolve_skill("demo", config, root / "BATCHAGENT.md"), target)

    def test_render_skills_prompt_includes_skill_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Demo\n\nFollow these rules.\n", encoding="utf-8")

            config = BatchConfig(skills=["demo"], skill_roots=["skills"])
            prompt = render_skills_prompt(config, root / "BATCHAGENT.md")

            self.assertIn("Skill demo", prompt)
            self.assertIn("Follow these rules.", prompt)


if __name__ == "__main__":
    unittest.main()
