from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable, Input

from batchagent import __version__
from batchagent.harness import HarnessProbe
from batchagent.manifest import create_sample_manifest, load_manifest
from batchagent.progress import ProgressState
from batchagent.scheduler import state_db_path
from batchagent.settings import load_settings
from batchagent.store import SessionStore
from batchagent.tui import BatchAgentTui, RunVariablesScreen, TaskDetailScreen


class TuiTests(unittest.TestCase):
    def test_tui_header_exposes_current_version(self) -> None:
        app = BatchAgentTui()
        self.assertEqual(app.title, "BatchAgent")
        self.assertEqual(app.sub_title, f"v{__version__}")

    def test_completion_candidates_include_commands_manifests_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                app = BatchAgentTui()
                app.discover_manifests()
                app.load_manifest_by_token("BATCHAGENT.md")

                self.assertIn("/show_batch", app.completion_candidates("/sho"))
                self.assertIn("BATCHAGENT.md", app.completion_candidates("/run "))
                self.assertIn("BATCHAGENT.md", app.completion_candidates("/show_batch B"))
                self.assertNotIn("1", app.completion_candidates("/show_batch "))
                self.assertNotIn("demo", app.completion_candidates("/show_batch d"))
                self.assertIn("demo-1", app.completion_candidates("/show_task demo"))
                self.assertIn("demo-2", app.completion_candidates("/run --only demo"))
                self.assertIn("opencode", app.completion_candidates("/harness use o"))
                self.assertIn("built-in", app.completion_candidates("/harness use b"))
                self.assertIn("claudecode", app.completion_candidates("/harness use c"))
                self.assertIn("codex", app.completion_candidates("/run --harness c"))
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
                        await app.handle_command("/show_batch BATCHAGENT.md")
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

    def test_up_down_selects_command_candidate(self) -> None:
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
                        command.value = "/"
                        await pilot.press("down")
                        await pilot.press("tab")
                        self.assertEqual(command.value, "/run")
                        self.assertTrue(command.has_focus)
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_ctrl_c_does_not_quit_tui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")

                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.press("ctrl+c")
                        self.assertTrue(app.is_running)
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_command_palette_describes_slash_commands(self) -> None:
        app = BatchAgentTui()
        lines = app.command_palette_lines("/")
        self.assertTrue(any("/history [run-id]" in line for line in lines))
        self.assertTrue(any("/run [manifest-path]" in line for line in lines))
        self.assertTrue(any("/show_run <run-id>" in line for line in lines))

    def test_command_split_preserves_windows_paths_and_quoted_values(self) -> None:
        app = BatchAgentTui()
        self.assertEqual(
            app.split_command(r'/show_batch E:\BatchAgent\tests\date_survey\BATCHAGENT.md'),
            ["/show_batch", r"E:\BatchAgent\tests\date_survey\BATCHAGENT.md"],
        )
        self.assertEqual(app.split_command('/run --var market="A share"'), ["/run", "--var", "market=A share"])

    def test_run_args_support_limit_focus_and_repeated_only(self) -> None:
        app = BatchAgentTui()
        token, only, retry_failed, run_vars, limit, focus, harness, resume_id = app.parse_run_args(
            [
                "BATCHAGENT.md",
                "--limit",
                "2",
                "--focus",
                "demo-1",
                "--only",
                "demo-1",
                "--only",
                "demo-2",
                "--retry-failed",
                "--harness",
                "opencode",
            ]
        )
        self.assertEqual(token, "BATCHAGENT.md")
        self.assertEqual(only, {"demo-1", "demo-2"})
        self.assertTrue(retry_failed)
        self.assertEqual(run_vars, {})
        self.assertEqual(limit, 2)
        self.assertEqual(focus, "demo-1")
        self.assertEqual(harness, "opencode")
        self.assertEqual(resume_id, "")

    def test_run_page_updates_from_progress_without_full_page_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")

                async def run() -> None:
                    app = BatchAgentTui("BATCHAGENT.md")
                    async with app.run_test(size=(100, 30)):
                        app.progress_state = ProgressState.from_manifest(app.require_manifest(), focus_task_id="demo-1")
                        app.active_run_id = "run-a"
                        app.selected_run_id = "run-a"
                        app.run_task = asyncio.create_task(asyncio.sleep(30))
                        app.page = "run"
                        app.render_run()
                        app.handle_progress_event(
                            {
                                "type": "task_started",
                                "task_id": "demo-1",
                                "run_id": "run-a",
                                "attempt_id": "attempt-a",
                                "attempt": 1,
                                "run_dir": "runs/demo-1",
                            }
                        )
                        app.flush_run_render()
                        app.handle_progress_event({"type": "model_delta", "task_id": "demo-1", "delta": "hello"})
                        app.flush_run_render()
                        self.assertTrue(app._detail_content_key.startswith("task-progress:demo-1"))
                        app.run_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await app.run_task
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_table_clicks_select_batch_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")

                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)) as pilot:
                        app.on_data_table_row_selected(SimpleNamespace(row_key=SimpleNamespace(value="manifest:BATCHAGENT.md")))
                        self.assertEqual(app.page, "batch")
                        self.assertIsNotNone(app.selected_manifest)

                        app.on_data_table_row_selected(SimpleNamespace(row_key=SimpleNamespace(value="task:demo-1")))
                        await pilot.pause()
                        self.assertIsInstance(app.screen, TaskDetailScreen)
                        await pilot.press("escape")
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_run_variables_screen_collects_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                text = Path("BATCHAGENT.md").read_text(encoding="utf-8")
                text = text.replace(
                    "tools = [\"write_file\", \"submit_artifact\"]",
                    "tools = [\"write_file\", \"submit_artifact\"]\nrun_variables = [{ name = \"market\", label = \"Market\", required = true }]",
                )
                Path("BATCHAGENT.md").write_text(text, encoding="utf-8")

                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)) as pilot:
                        await app.handle_command("/show_batch BATCHAGENT.md")
                        task = asyncio.create_task(app.collect_run_vars(app.require_manifest(), {}))
                        await pilot.pause()
                        self.assertIsInstance(app.screen, RunVariablesScreen)
                        input_widget = app.screen.query_one("#vars_input", Input)
                        input_widget.value = "A-share"
                        await pilot.press("enter")
                        values = await task
                        self.assertEqual(values, {"market": "A-share"})
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_history_rows_do_not_create_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                app = BatchAgentTui()
                app.discover_manifests()
                app.load_manifest_by_token("BATCHAGENT.md")

                self.assertEqual(app.history_rows(), [])
                self.assertFalse((Path(tmp) / ".batchagent").exists())
            finally:
                os.chdir(previous)

    def test_task_detail_reads_persisted_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            home = Path(tmp) / "home"
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                    app = BatchAgentTui()
                    app.discover_manifests()
                    app.load_manifest_by_token("BATCHAGENT.md")
                    manifest = app.require_manifest()

                    store = SessionStore(state_db_path(manifest))
                    try:
                        run_dir = Path(tmp) / ".batchagent" / "runs" / "demo-1-run-a"
                        store.start_batch_run(
                            "run-a",
                            manifest.path,
                            manifest.config.name,
                            manifest.tasks,
                            selected_task_ids=["demo-1"],
                        )
                        store.start_attempt("attempt-a", "run-a", "demo-1", 1, run_dir)
                        store.add_message(
                            "attempt-a",
                            1,
                            "assistant",
                            "agent output line\nsecond line",
                            {"role": "assistant"},
                        )
                        store.finish_attempt("attempt-a", "done")
                        store.finish_batch_run("run-a", "completed")
                    finally:
                        store.close()

                    app.selected_run_id = "run-a"
                    detail = app.task_detail_text_for("demo-1", "run-a")
                    self.assertIn("attempt_id: attempt-a", detail)
                    self.assertIn("agent output line\nsecond line", detail)
            finally:
                os.chdir(previous)

    def test_show_task_opens_modal_and_escape_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")

                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)) as pilot:
                        await app.handle_command("/show_batch BATCHAGENT.md")
                        await app.handle_command("/show_task demo-1")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, TaskDetailScreen)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertNotIsInstance(app.screen, TaskDetailScreen)
                        await app.handle_command("/quit")

                asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_batch_selection_lists_runs_then_run_lists_tasks_and_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            home = Path(tmp) / "home"
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                app = BatchAgentTui()
                app.discover_manifests()
                app.load_manifest_by_token("BATCHAGENT.md")
                manifest = app.require_manifest()
                with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                    store = SessionStore(state_db_path(manifest))
                    try:
                        store.start_batch_run(
                            "run-a",
                            manifest.path,
                            manifest.config.name,
                            manifest.tasks,
                            harness="opencode",
                            selected_task_ids=["demo-1"],
                        )
                        store.start_attempt("attempt-a1", "run-a", "demo-1", 1, Path(tmp) / "attempt-a1", harness="opencode")
                        store.finish_attempt("attempt-a1", "failed", "boom")
                        store.mark_run_task_retry("run-a", "demo-1")
                        store.start_attempt("attempt-a2", "run-a", "demo-1", 2, Path(tmp) / "attempt-a2", harness="opencode")
                        store.finish_attempt("attempt-a2", "failed", "boom again")
                        store.finish_batch_run("run-a", "failed")
                    finally:
                        store.close()

                    async def run() -> None:
                        tui = BatchAgentTui("BATCHAGENT.md")
                        async with tui.run_test(size=(140, 36)) as pilot:
                            tui.page = "batch"
                            tui.render_page()
                            self.assertEqual(tui.query_one("#table", DataTable).row_count, 1)
                            tui.on_data_table_row_selected(SimpleNamespace(row_key=SimpleNamespace(value="run:run-a")))
                            self.assertEqual(tui.page, "run")
                            self.assertEqual(tui.selected_run_id, "run-a")
                            self.assertEqual(tui.query_one("#table", DataTable).row_count, 2)
                            tui.on_data_table_row_selected(
                                SimpleNamespace(row_key=SimpleNamespace(value="run-task:run-a:demo-1"))
                            )
                            await pilot.pause()
                            self.assertIsInstance(tui.screen, TaskDetailScreen)
                            detail = tui.task_detail_text_for("demo-1", "run-a")
                            self.assertIn("attempt-a1", detail)
                            self.assertIn("attempt-a2", detail)
                            await pilot.press("escape")
                            await tui.handle_command("/quit")

                    asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_finish_run_notifies_from_persisted_paused_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            home = Path(tmp) / "home"
            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                    manifest = load_manifest(Path("BATCHAGENT.md"))
                    store = SessionStore(state_db_path(manifest))
                    try:
                        store.start_batch_run(
                            "run-paused",
                            manifest.path,
                            manifest.config.name,
                            manifest.tasks,
                            selected_task_ids=["demo-1"],
                        )
                        store.finish_batch_run("run-paused", "paused")
                    finally:
                        store.close()

                    async def run() -> None:
                        app = BatchAgentTui("BATCHAGENT.md")
                        async with app.run_test(size=(100, 30)):
                            app.active_run_id = "run-paused"
                            app.selected_run_id = "run-paused"
                            finished = asyncio.get_running_loop().create_future()
                            finished.set_result([])
                            with patch.object(app, "notify") as notify:
                                app.finish_run(finished)  # type: ignore[arg-type]
                            message = str(notify.call_args.args[0])
                            self.assertIn("Run run-paused paused", message)
                            self.assertIn("1 unfinished Task(s), 1 queued", message)
                            self.assertIn("/resume run-paused", message)
                            await app.handle_command("/quit")

                    asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_tui_resume_rejects_run_from_another_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            home = Path(tmp) / "home"
            try:
                os.chdir(tmp)
                first_path = Path("first") / "BATCHAGENT.md"
                second_path = Path("second") / "BATCHAGENT.md"
                first_path.parent.mkdir()
                second_path.parent.mkdir()
                create_sample_manifest(first_path)
                create_sample_manifest(second_path)
                with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                    first = load_manifest(first_path)
                    store = SessionStore(state_db_path(first))
                    try:
                        store.start_batch_run(
                            "run-first",
                            first.path,
                            first.config.name,
                            first.tasks,
                            selected_task_ids=["demo-1"],
                        )
                        store.finish_batch_run("run-first", "paused")
                    finally:
                        store.close()

                    async def run() -> None:
                        app = BatchAgentTui(str(second_path))
                        async with app.run_test(size=(100, 30)):
                            with self.assertRaisesRegex(RuntimeError, "belongs to another batch config"):
                                await app.command_resume(["run-first"])
                            await app.handle_command("/quit")

                    asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_theme_and_harness_settings_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)):
                        await app.handle_command("/theme textual-light")
                        await app.handle_command("/harness use native")
                        await app.handle_command("/quit")

                asyncio.run(run())
                settings = load_settings()
                self.assertEqual(settings["theme"], "textual-light")
                self.assertEqual(settings["harness"], "native")
                restarted = BatchAgentTui()
                self.assertEqual(restarted.theme, "textual-light")
                self.assertEqual(restarted.harness_name, "native")

    def test_harness_command_opens_selectable_list_and_marks_current_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"

            async def fake_probe(name: str, _config=None) -> HarnessProbe:
                return HarnessProbe(
                    name=name,
                    available=True,
                    executable=name,
                    version=f"{name} 1.0",
                )

            with patch.dict(os.environ, {"BAGENT_HOME": str(home)}), patch(
                "batchagent.tui.probe_harness",
                new=fake_probe,
            ):
                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(140, 36)):
                        await app.handle_command("/harness")
                        self.assertEqual(app.page, "harness")
                        table = app.query_one("#table", DataTable)
                        self.assertEqual(table.row_count, 4)
                        self.assertEqual(str(table.get_row("harness:native")[0]), "CURRENT")
                        self.assertEqual(str(table.get_row("harness:codex")[1]), "codex")
                        self.assertIn("harness: built-in", app.selection_text())

                        app.on_data_table_row_selected(
                            SimpleNamespace(row_key=SimpleNamespace(value="harness:codex"))
                        )
                        self.assertEqual(app.harness_name, "codex")
                        self.assertEqual(str(table.get_row("harness:codex")[0]), "CURRENT")
                        self.assertIn("harness: codex", app.selection_text())
                        await app.handle_command("/quit")

                asyncio.run(run())
                self.assertEqual(load_settings()["harness"], "codex")

    def test_selected_harness_is_used_for_the_next_tui_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            home = Path(tmp) / "home"
            call: dict[str, object] = {}

            async def fake_run_manifest(*_args, **kwargs):
                call.update(kwargs)
                return []

            try:
                os.chdir(tmp)
                create_sample_manifest("BATCHAGENT.md")
                with patch.dict(os.environ, {"BAGENT_HOME": str(home)}), patch(
                    "batchagent.tui.run_manifest",
                    new=fake_run_manifest,
                ):
                    async def run() -> None:
                        app = BatchAgentTui("BATCHAGENT.md")
                        async with app.run_test(size=(100, 30)) as pilot:
                            app.harness_name = "codex"
                            await app.command_run([])
                            assert app.run_task is not None
                            await app.run_task
                            await pilot.pause()
                            self.assertEqual(call["harness"], "codex")
                            self.assertEqual(app.page, "batch")
                            await app.handle_command("/quit")

                    asyncio.run(run())
            finally:
                os.chdir(previous)

    def test_theme_command_repairs_corrupt_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            (home / "settings.json").write_text("{broken", encoding="utf-8")
            with patch.dict(os.environ, {"BAGENT_HOME": str(home)}):
                async def run() -> None:
                    app = BatchAgentTui()
                    async with app.run_test(size=(100, 30)):
                        await app.handle_command("/theme textual-light")
                        await app.handle_command("/quit")

                asyncio.run(run())
                self.assertEqual(load_settings()["theme"], "textual-light")


if __name__ == "__main__":
    unittest.main()
