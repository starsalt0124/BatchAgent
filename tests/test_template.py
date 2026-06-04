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


if __name__ == "__main__":
    unittest.main()

