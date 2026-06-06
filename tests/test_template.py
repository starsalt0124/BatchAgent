from __future__ import annotations

import unittest

from batchagent.models import BatchConfig, Task
from batchagent.template import render_template


class TemplateTests(unittest.TestCase):
    def test_renders_nested_task_input(self) -> None:
        task = Task(status="todo", id="abc", kind="patch", input={"patch_file": "patches/a.patch"})
        config = BatchConfig(workspace="repo")
        result = render_template("{{task.id}} {{task.input.patch_file}} {{workspace}}", task, config)
        self.assertEqual(result, "abc patches/a.patch repo")

    def test_renders_current_date_keyword(self) -> None:
        task = Task(status="todo", id="abc")
        config = BatchConfig()
        result = render_template("today=CURR_DATE templated={{CURR_DATE}}", task, config, current_date="2026-06-05")
        self.assertEqual(result, "today=2026-06-05 templated=2026-06-05")

    def test_renders_runtime_variables(self) -> None:
        task = Task(status="todo", id="abc")
        config = BatchConfig(run_vars={"market": "A股", "date": "2026-06-06"})
        result = render_template("market={{vars.market}} date={{run_vars.date}}", task, config)
        self.assertEqual(result, "market=A股 date=2026-06-06")


if __name__ == "__main__":
    unittest.main()
