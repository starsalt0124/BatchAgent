from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from batchagent.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    HarnessError,
    HarnessRequest,
    OpenCodeHarness,
    _handle_json_event,
    _StreamState,
    available_harnesses,
    canonical_harness_name,
    get_harness,
    harness_display_names,
)
from batchagent.manifest import create_sample_manifest, load_manifest
from batchagent.models import BatchConfig, HarnessConfig, Task


REPO_ROOT = Path(__file__).resolve().parents[1]


FAKE_HARNESS = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("fake-harness 1.2.3")
    raise SystemExit(0)

prompt = sys.stdin.read()
capture = os.environ.get("PROMPT_CAPTURE")
if capture:
    Path(capture).write_text(prompt, encoding="utf-8")

mode = os.environ.get("FAKE_MODE", "events")
if mode == "sleep":
    Path(os.environ["PARENT_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["CHILD_PID_FILE"]).write_text(str(child.pid), encoding="utf-8")
    time.sleep(60)
    raise SystemExit(0)

if mode in {"submit", "codex_submit"}:
    if mode == "submit":
        inline = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        mcp = inline["mcp"]["bagent"]
        mcp_command = mcp["command"]
        mcp_environment = mcp["environment"]
    else:
        import tomllib
        overrides = {}
        for index, arg in enumerate(sys.argv[:-1]):
            if arg == "-c":
                key, value = sys.argv[index + 1].split("=", 1)
                overrides[key] = value
        mcp_command = [
            json.loads(overrides["mcp_servers.bagent.command"]),
            *json.loads(overrides["mcp_servers.bagent.args"]),
        ]
        mcp_environment = tomllib.loads("value=" + overrides["mcp_servers.bagent.env"])["value"]
    child_env = os.environ.copy()
    child_env.update(mcp_environment)
    server = subprocess.Popen(
        mcp_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
    )
    calls = [
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}}},
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"report_progress","arguments":{"message":"working","percent":25}}},
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"submit_artifact","arguments":{"summary":"fake done","artifact_path":"","metadata":{"source":"fake"}}}},
    ]
    for call in calls:
        server.stdin.write(json.dumps(call) + "\n")
        server.stdin.flush()
        response = json.loads(server.stdout.readline())
        if response.get("result", {}).get("isError"):
            print(response, file=sys.stderr)
            raise SystemExit(3)
    server.stdin.close()
    server.wait(timeout=10)

line_count = int(os.environ.get("FAKE_LINES", "1"))
print(json.dumps({"type":"system","session_id":"session-123"}), flush=True)
for index in range(line_count):
    print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":f"chunk-{index}"}],"usage":{"input_tokens":3}}}), flush=True)
    print(f"stderr-{index}", file=sys.stderr, flush=True)
print(json.dumps({"type":"result","session_id":"session-123","usage":{"output_tokens":4},"total_cost_usd":0.125,"result":"done"}), flush=True)
'''


def _write_fake(path: Path) -> Path:
    path.write_text(textwrap.dedent(FAKE_HARNESS), encoding="utf-8")
    return path


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        stat = Path(f"/proc/{pid}/stat")
        if stat.is_file():
            try:
                if stat.read_text(encoding="utf-8").split()[2] == "Z":
                    return False
            except (OSError, IndexError):
                pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class HarnessTests(unittest.TestCase):
    def make_request(
        self,
        root: Path,
        script: Path,
        *,
        name: str = "opencode",
        inject_tools: bool = False,
        require_submit: bool = False,
        timeout: float = 5,
        environment: dict[str, str] | None = None,
    ) -> HarnessRequest:
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        config = BatchConfig()
        config.artifact.require_submit = require_submit
        config.harness = HarnessConfig(
            name=name,
            command=[sys.executable, str(script)],
            inject_tools=inject_tools,
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            **(environment or {}),
        }
        return HarnessRequest(
            run_id="run-1",
            attempt_id="attempt-1",
            manifest_path=root / "BATCHAGENT.md",
            config=config,
            task=Task(status="running", id="task-1"),
            workspace=workspace,
            run_dir=root / "runs" / "attempt-1",
            prompt="secret prompt text",
            timeout_seconds=timeout,
            environment=env,
        )

    def test_registry_and_probe(self) -> None:
        self.assertEqual(available_harnesses(), ["native", "opencode", "claude", "codex"])
        self.assertEqual(harness_display_names(), ["built-in", "opencode", "claudecode", "codex"])
        self.assertEqual(canonical_harness_name("built-in"), "native")
        self.assertEqual(canonical_harness_name("claudecode"), "claude")
        self.assertIs(get_harness("claude-code"), get_harness("claude"))
        self.assertIs(get_harness("codex-cli"), get_harness("codex"))
        self.assertTrue(asyncio.run(get_harness("native").probe()).available)
        with tempfile.TemporaryDirectory() as tmp:
            script = _write_fake(Path(tmp) / "fake.py")
            probe = asyncio.run(OpenCodeHarness().probe(HarnessConfig(command=[sys.executable, str(script)])))
            self.assertTrue(probe.available)
            self.assertEqual(probe.version, "fake-harness 1.2.3")

    def test_external_prompt_can_omit_bagent_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            request = self.make_request(root, script)
            request.config.inject_batchagent_protocol = False

            invocation = asyncio.run(OpenCodeHarness().build_invocation(request))
            self.assertEqual(invocation.prompt, "secret prompt text\n")
            self.assertNotIn("Bagent external harness protocol", invocation.prompt)

    def test_codex_builds_sandboxed_json_invocation_with_scoped_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            request = self.make_request(root, script, name="codex", inject_tools=True, require_submit=True)
            invocation = asyncio.run(CodexHarness().build_invocation(request))

            self.assertNotIn("secret prompt text", " ".join(invocation.command))
            self.assertIn("exec", invocation.command)
            self.assertIn("--json", invocation.command)
            self.assertIn("workspace-write", invocation.command)
            self.assertNotIn("danger-full-access", invocation.command)
            self.assertEqual(invocation.command[-1], "-")
            overrides = [
                invocation.command[index + 1]
                for index, item in enumerate(invocation.command[:-1])
                if item == "-c"
            ]
            self.assertTrue(any(value.startswith("mcp_servers.bagent.command=") for value in overrides))
            self.assertTrue(any("BAGENT_ATTEMPT_ID" in value and "attempt-1" in value for value in overrides))
            self.assertIn(
                'mcp_servers.bagent.enabled_tools=["submit_artifact","report_progress"]',
                overrides,
            )

    def test_codex_json_events_preserve_thread_usage_text_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            request = self.make_request(root, script, name="codex")
            events: list[dict] = []
            request.progress_callback = events.append
            state = _StreamState()

            _handle_json_event(request, "codex", state, {"type": "thread.started", "thread_id": "thread-123"})
            _handle_json_event(
                request,
                "codex",
                state,
                {"type": "item.completed", "item": {"type": "agent_message", "text": "finished"}},
            )
            _handle_json_event(
                request,
                "codex",
                state,
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "bagent",
                        "tool": "submit_artifact",
                        "status": "completed",
                        "arguments": {"summary": "done"},
                        "result": {"accepted": True},
                    },
                },
            )
            _handle_json_event(
                request,
                "codex",
                state,
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 5, "cached_input_tokens": 2, "output_tokens": 3},
                },
            )

            self.assertEqual(state.session_id, "thread-123")
            self.assertEqual(state.usage["cached_input_tokens"], 2)
            self.assertTrue(any(event["type"] == "model_delta" and event["delta"] == "finished" for event in events))
            self.assertTrue(
                any(event["type"] == "tool_finished" and event["tool"] == "bagent.submit_artifact" for event in events)
            )

    def test_codex_runs_with_injected_mcp_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            request = self.make_request(
                root,
                script,
                name="codex",
                inject_tools=True,
                require_submit=True,
                environment={"FAKE_MODE": "codex_submit"},
            )

            result = asyncio.run(CodexHarness().run(request))

            self.assertTrue(result.success, result.error)
            self.assertEqual(result.submission.summary, "fake done")
            self.assertEqual(result.submission.metadata, {"source": "fake"})

    def test_manifest_parses_harness_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BATCHAGENT.md"
            create_sample_manifest(path)
            text = path.read_text(encoding="utf-8").replace(
                "[artifact]",
                '[harness]\nname = "opencode"\ncommand = ["custom-opencode"]\nextra_args = ["--thinking"]\nenv_allowlist = ["OPENAI_API_KEY"]\n\n[artifact]',
            )
            path.write_text(text, encoding="utf-8")
            config = load_manifest(path).config.harness
            self.assertEqual(config.name, "opencode")
            self.assertEqual(config.command, ["custom-opencode"])
            self.assertEqual(config.extra_args, ["--thinking"])
            self.assertEqual(config.env_allowlist, ["OPENAI_API_KEY"])

    def test_opencode_runs_jsonl_injects_mcp_and_parses_session_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            prompt_capture = root / "prompt.txt"
            events: list[dict] = []
            request = self.make_request(
                root,
                script,
                inject_tools=True,
                require_submit=True,
                environment={"FAKE_MODE": "submit", "FAKE_LINES": "250", "PROMPT_CAPTURE": str(prompt_capture)},
            )
            request.progress_callback = events.append

            adapter = OpenCodeHarness()
            invocation = asyncio.run(adapter.build_invocation(request))
            self.assertNotIn("secret prompt text", " ".join(invocation.command))
            self.assertNotIn("--auto", invocation.command)
            inline = json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(inline["mcp"]["bagent"]["environment"]["BAGENT_ATTEMPT_ID"], "attempt-1")

            result = asyncio.run(adapter.run(request))

            self.assertTrue(result.success, result.error)
            self.assertEqual(result.session_id, "session-123")
            self.assertEqual(result.usage["input_tokens"], 3)
            self.assertEqual(result.usage["output_tokens"], 4)
            self.assertEqual(result.usage["total_cost_usd"], 0.125)
            self.assertEqual(result.submission.summary, "fake done")
            self.assertLessEqual(len(result.stdout_tail), 20_000)
            self.assertLessEqual(len(result.stderr_tail), 20_000)
            self.assertIn("secret prompt text", prompt_capture.read_text(encoding="utf-8"))
            self.assertTrue(any(event["type"] == "harness_progress" for event in events))
            self.assertTrue(any(event["type"] == "artifact_submitted" for event in events))

    def test_claude_builds_run_scoped_config_without_permission_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            request = self.make_request(root, script, name="claude", inject_tools=True, require_submit=True)
            invocation = asyncio.run(ClaudeCodeHarness().build_invocation(request))

            command_text = " ".join(invocation.command)
            self.assertIn("--output-format stream-json", command_text)
            self.assertIn("--strict-mcp-config", invocation.command)
            self.assertNotIn("secret prompt text", command_text)
            self.assertNotIn("bypass", command_text.lower())
            config = json.loads(invocation.mcp_config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"]["bagent"]["env"]["BAGENT_MCP_NONCE"], request.nonce)

    def test_external_require_submit_rejects_disabled_injection_and_unsafe_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            request = self.make_request(root, script, inject_tools=False, require_submit=True)
            with self.assertRaises(HarnessError):
                asyncio.run(OpenCodeHarness().build_invocation(request))

            request.config.harness.inject_tools = True
            request.config.harness.extra_args = ["--auto"]
            with self.assertRaises(HarnessError):
                asyncio.run(OpenCodeHarness().build_invocation(request))

            request.config.harness.name = "claude"
            request.config.harness.extra_args = ["--permission-mode", "bypassPermissions"]
            with self.assertRaises(HarnessError):
                asyncio.run(ClaudeCodeHarness().build_invocation(request))

            request.config.harness.extra_args = ["--dangerously-skip-permissions=true"]
            with self.assertRaises(HarnessError):
                asyncio.run(ClaudeCodeHarness().build_invocation(request))

            request.config.harness.name = "codex"
            request.config.harness.extra_args = ["--sandbox=danger-full-access"]
            with self.assertRaises(HarnessError):
                asyncio.run(CodexHarness().build_invocation(request))

            unsafe = HarnessConfig(
                name="opencode",
                command=[sys.executable, "-c", "print('must not execute')"],
            )
            with self.assertRaises(HarnessError):
                asyncio.run(OpenCodeHarness().probe(unsafe))

    def test_probe_uses_neutral_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "PROJECT_MARKER"
            marker.write_text("project-local", encoding="utf-8")
            script = root / "probe.py"
            script.write_text(
                "import pathlib\nprint('leaked' if pathlib.Path('PROJECT_MARKER').exists() else 'clean')\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                probe = asyncio.run(OpenCodeHarness().probe(HarnessConfig(command=[sys.executable, str(script)])))
            finally:
                os.chdir(previous)
            self.assertTrue(probe.available)
            self.assertEqual(probe.version, "clean")

    @unittest.skipIf(os.name == "nt", "POSIX launcher symlink assertion")
    def test_probe_rejects_shell_launcher_hidden_behind_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alias = root / "trusted-runner"
            alias.symlink_to("/bin/sh")
            sentinel = root / "sentinel"
            config = HarnessConfig(
                name="opencode",
                command=[str(alias), "-c", f"printf bypass > {sentinel}"],
            )
            with self.assertRaises(HarnessError):
                asyncio.run(OpenCodeHarness().probe(config))
            self.assertFalse(sentinel.exists())

    def test_external_stream_deep_merges_usage_and_preserves_result_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "nested_usage.py"
            script.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "if '--version' in sys.argv:",
                        "    print('nested 1.0')",
                        "    raise SystemExit(0)",
                        "_prompt = sys.stdin.read()",
                        "print(json.dumps({'type':'message','tokens':{'input_tokens':5},'text':'hello'}), flush=True)",
                        "print(json.dumps({'type':'result','tokens':{'output_tokens':2},'result':{'answer':42}}), flush=True)",
                        "print('diagnostic', file=sys.stderr, flush=True)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            request = self.make_request(root, script)
            result = asyncio.run(OpenCodeHarness().run(request))

            self.assertTrue(result.success, result.error)
            self.assertEqual(result.usage["tokens"], {"input_tokens": 5, "output_tokens": 2})
            self.assertEqual(result.output, {"answer": 42})
            self.assertIn('"answer": 42', (request.run_dir / "stdout.jsonl").read_text(encoding="utf-8"))
            self.assertIn("diagnostic", (request.run_dir / "stderr.log").read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_timeout_kills_harness_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            parent_pid_file = root / "parent.pid"
            child_pid_file = root / "child.pid"
            request = self.make_request(
                root,
                script,
                inject_tools=False,
                require_submit=False,
                timeout=0.5,
                environment={
                    "FAKE_MODE": "sleep",
                    "PARENT_PID_FILE": str(parent_pid_file),
                    "CHILD_PID_FILE": str(child_pid_file),
                },
            )

            result = asyncio.run(OpenCodeHarness().run(request))

            self.assertTrue(result.timed_out)
            self.assertFalse(result.success)
            parent_pid = int(parent_pid_file.read_text(encoding="utf-8"))
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            self.assertFalse(_process_alive(parent_pid))
            self.assertFalse(_process_alive(child_pid))

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_cancellation_kills_harness_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = _write_fake(root / "fake.py")
            parent_pid_file = root / "cancel-parent.pid"
            child_pid_file = root / "cancel-child.pid"
            request = self.make_request(
                root,
                script,
                inject_tools=False,
                require_submit=False,
                timeout=30,
                environment={
                    "FAKE_MODE": "sleep",
                    "PARENT_PID_FILE": str(parent_pid_file),
                    "CHILD_PID_FILE": str(child_pid_file),
                },
            )

            async def cancel_running_harness() -> None:
                future = asyncio.create_task(OpenCodeHarness().run(request))
                for _ in range(100):
                    if parent_pid_file.is_file() and child_pid_file.is_file():
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(parent_pid_file.is_file())
                self.assertTrue(child_pid_file.is_file())
                future.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await future

            asyncio.run(cancel_running_harness())
            parent_pid = int(parent_pid_file.read_text(encoding="utf-8"))
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            self.assertFalse(_process_alive(parent_pid))
            self.assertFalse(_process_alive(child_pid))


if __name__ == "__main__":
    unittest.main()
