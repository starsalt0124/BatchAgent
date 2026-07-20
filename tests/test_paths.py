from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batchagent.paths import bagent_home, legacy_skills_dir, runs_dir, settings_path, skill_search_dirs, skills_dir, state_db_path
from batchagent.models import BatchConfig, Task
from batchagent.workspace import task_run_dir


class PathTests(unittest.TestCase):
    def test_default_home_is_dot_bagent(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAGENT_HOME", None)
            self.assertEqual(bagent_home(), Path.home() / ".bagent")

    def test_bagent_home_override_drives_state_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": tmp}):
            root = Path(tmp)
            self.assertEqual(settings_path(), root / "settings.json")
            self.assertEqual(state_db_path(), root / "state.sqlite3")
            self.assertEqual(runs_dir(), root / "runs")
            self.assertEqual(skills_dir(), root / "skills")

    def test_created_directories_are_private_when_modes_are_supported(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows permissions use ACLs rather than POSIX modes")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            home = bagent_home(create=True)
            skills = skills_dir(create=True)
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(skills.stat().st_mode), 0o700)

    def test_skill_search_uses_new_then_legacy_xdg_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {"BAGENT_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config")},
            ):
                self.assertEqual(legacy_skills_dir(), root / "config" / "batchagent" / "skills")
                self.assertEqual(skill_search_dirs(), [root / "state" / "skills", root / "config" / "batchagent" / "skills"])

    def test_legacy_default_run_dir_is_redirected_to_bagent_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            path = task_run_dir(
                BatchConfig(run_dir=".batchagent/runs"),
                Path(tmp) / "BATCHAGENT.md",
                Task(status="todo", id="task-a"),
                "attempt-a",
                "run-a",
            )
            self.assertEqual(path, (Path(tmp) / "home" / "runs" / "run-a" / "task-a" / "attempt-a").resolve())

    def test_current_default_run_dir_honors_bagent_home_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BAGENT_HOME": str(Path(tmp) / "home")}):
            for configured in ("~/.bagent/runs", str(Path.home() / ".bagent" / "runs")):
                with self.subTest(configured=configured):
                    path = task_run_dir(
                        BatchConfig(run_dir=configured),
                        Path(tmp) / "BATCHAGENT.md",
                        Task(status="todo", id="task-a"),
                        "attempt-a",
                        "run-a",
                    )
                    self.assertEqual(
                        path,
                        (Path(tmp) / "home" / "runs" / "run-a" / "task-a" / "attempt-a").resolve(),
                    )


if __name__ == "__main__":
    unittest.main()
