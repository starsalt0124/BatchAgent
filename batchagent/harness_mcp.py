from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .models import ArtifactSubmission
from .util import atomic_write_text, utc_now


MCP_PROTOCOL_VERSION = "2024-11-05"
SUBMISSION_FILE = "submission.json"
SUBMISSION_READY_FILE = "submission.ready"
SUBMISSION_CLAIM_FILE = ".submission.claim"


class HarnessMcpError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpoolIdentity:
    nonce: str
    run_id: str
    attempt_id: str
    task_id: str


class HarnessSpool:
    """Run-scoped, append-only IPC used by the injected MCP server.

    The MCP child never opens the application database.  It writes atomic files
    and the bagent parent is the only process that validates and persists them.
    """

    def __init__(self, spool_dir: Path, identity: SpoolIdentity, *, run_dir: Path | None = None):
        self.spool_dir = spool_dir.resolve(strict=False)
        if run_dir is not None:
            resolved_run_dir = run_dir.resolve(strict=False)
            if not self.spool_dir.is_relative_to(resolved_run_dir):
                raise HarnessMcpError("MCP spool directory must be inside the task attempt run directory")
        self.identity = identity
        if not identity.nonce:
            raise HarnessMcpError("MCP spool nonce is required")
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.spool_dir.chmod(0o700)
        except OSError:
            pass

    @classmethod
    def from_environment(cls) -> "HarnessSpool":
        required = {
            "BAGENT_MCP_SPOOL_DIR": os.environ.get("BAGENT_MCP_SPOOL_DIR", ""),
            "BAGENT_MCP_NONCE": os.environ.get("BAGENT_MCP_NONCE", ""),
            "BAGENT_RUN_ID": os.environ.get("BAGENT_RUN_ID", ""),
            "BAGENT_ATTEMPT_ID": os.environ.get("BAGENT_ATTEMPT_ID", ""),
            "BAGENT_TASK_ID": os.environ.get("BAGENT_TASK_ID", ""),
            "BAGENT_RUN_DIR": os.environ.get("BAGENT_RUN_DIR", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise HarnessMcpError("missing MCP environment variable(s): " + ", ".join(missing))
        return cls(
            Path(required["BAGENT_MCP_SPOOL_DIR"]),
            SpoolIdentity(
                nonce=required["BAGENT_MCP_NONCE"],
                run_id=required["BAGENT_RUN_ID"],
                attempt_id=required["BAGENT_ATTEMPT_ID"],
                task_id=required["BAGENT_TASK_ID"],
            ),
            run_dir=Path(required["BAGENT_RUN_DIR"]),
        )

    def submit_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            raise HarnessMcpError("summary is required")
        artifact_path = str(arguments.get("artifact_path") or "").strip()
        metadata = arguments.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise HarnessMcpError("metadata must be an object")

        claim_path = self.spool_dir / SUBMISSION_CLAIM_FILE
        try:
            fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise HarnessMcpError("submit_artifact may only be called once for this task attempt") from exc
        try:
            os.write(fd, (self.identity.attempt_id + "\n").encode("utf-8"))
        finally:
            os.close(fd)

        record = {
            "schema_version": 1,
            "nonce": self.identity.nonce,
            "run_id": self.identity.run_id,
            "attempt_id": self.identity.attempt_id,
            "task_id": self.identity.task_id,
            "summary": summary,
            "artifact_path": artifact_path,
            "metadata": metadata,
            "created_at": utc_now(),
        }
        atomic_write_text(
            self.spool_dir / SUBMISSION_FILE,
            json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
        )
        atomic_write_text(self.spool_dir / SUBMISSION_READY_FILE, self.identity.attempt_id + "\n")
        return {"accepted": True, "message": "artifact submission recorded"}

    def report_progress(self, arguments: dict[str, Any]) -> dict[str, Any]:
        message = str(arguments.get("message") or "").strip()
        if not message:
            raise HarnessMcpError("message is required")
        metadata = arguments.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise HarnessMcpError("metadata must be an object")
        percent_raw = arguments.get("percent")
        percent: float | None = None
        if percent_raw is not None:
            try:
                percent = float(percent_raw)
            except (TypeError, ValueError) as exc:
                raise HarnessMcpError("percent must be a number between 0 and 100") from exc
            if not 0 <= percent <= 100:
                raise HarnessMcpError("percent must be between 0 and 100")

        record = {
            "schema_version": 1,
            "nonce": self.identity.nonce,
            "run_id": self.identity.run_id,
            "attempt_id": self.identity.attempt_id,
            "task_id": self.identity.task_id,
            "message": message,
            "percent": percent,
            "metadata": metadata,
            "created_at": utc_now(),
        }
        events_dir = self.spool_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        event_name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
        atomic_write_text(events_dir / event_name, json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n")
        return {"accepted": True, "message": "progress recorded"}


def load_submission(
    spool_dir: Path,
    *,
    nonce: str,
    run_id: str,
    attempt_id: str,
    task_id: str,
) -> ArtifactSubmission | None:
    """Read a complete submission and reject stale/cross-run spool records."""

    ready_path = spool_dir / SUBMISSION_READY_FILE
    record_path = spool_dir / SUBMISSION_FILE
    if not ready_path.is_file() or not record_path.is_file():
        return None
    try:
        ready_attempt = ready_path.read_text(encoding="utf-8").strip()
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessMcpError(f"invalid harness artifact submission spool: {exc}") from exc
    expected = {
        "nonce": nonce,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "task_id": task_id,
    }
    if ready_attempt != attempt_id:
        raise HarnessMcpError("artifact submission ready marker belongs to another task attempt")
    for key, value in expected.items():
        if str(record.get(key) or "") != value:
            raise HarnessMcpError(f"artifact submission {key} does not match the active task attempt")
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise HarnessMcpError("artifact submission metadata must be an object")
    summary = str(record.get("summary") or "").strip()
    if not summary:
        raise HarnessMcpError("artifact submission summary is empty")
    return ArtifactSubmission(
        summary=summary,
        artifact_path=str(record.get("artifact_path") or ""),
        metadata=metadata,
    )


def load_progress_events(
    spool_dir: Path,
    *,
    nonce: str,
    run_id: str,
    attempt_id: str,
    task_id: str,
    seen: set[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return complete progress records not present in ``seen``.

    Event files are immutable and atomically renamed into place, so polling is
    safe while the MCP server is still running.
    """

    events_dir = spool_dir / "events"
    if not events_dir.is_dir():
        return []
    expected = {
        "nonce": nonce,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "task_id": task_id,
    }
    seen = seen if seen is not None else set()
    records: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(events_dir.glob("*.json"), key=lambda item: item.name):
        if path.name in seen:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessMcpError(f"invalid harness progress spool {path.name}: {exc}") from exc
        if not isinstance(record, dict):
            raise HarnessMcpError(f"invalid harness progress spool {path.name}: record must be an object")
        for key, value in expected.items():
            if str(record.get(key) or "") != value:
                raise HarnessMcpError(f"harness progress {key} does not match the active task attempt")
        seen.add(path.name)
        records.append((path.name, record))
    return records


TOOLS = [
    {
        "name": "submit_artifact",
        "description": "Submit the final structured result for the assigned bagent task attempt. Call exactly once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "artifact_path": {"type": "string", "default": ""},
                "metadata": {"type": "object"},
            },
            "required": ["summary", "metadata"],
            "additionalProperties": False,
        },
    },
    {
        "name": "report_progress",
        "description": "Report concise progress for the active bagent task attempt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "percent": {"type": "number", "minimum": 0, "maximum": 100},
                "metadata": {"type": "object"},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
]


class HarnessMcpServer:
    def __init__(self, spool: HarnessSpool):
        self.spool = spool

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = str(request.get("method") or "")
        if not method:
            return self._error(request_id, -32600, "invalid JSON-RPC request")
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            params = request.get("params") or {}
            version = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
            return self._result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "bagent", "version": __version__},
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return self._tool_result(request_id, "arguments must be an object", is_error=True)
            try:
                if name == "submit_artifact":
                    result = self.spool.submit_artifact(arguments)
                elif name == "report_progress":
                    result = self.spool.report_progress(arguments)
                else:
                    return self._tool_result(request_id, f"unknown tool: {name}", is_error=True)
            except Exception as exc:
                return self._tool_result(request_id, str(exc), is_error=True)
            return self._tool_result(request_id, json.dumps(result, ensure_ascii=False), is_error=False)
        return self._error(request_id, -32601, f"method not found: {method}")

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @classmethod
    def _tool_result(cls, request_id: Any, text: str, *, is_error: bool) -> dict[str, Any]:
        return cls._result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )


def serve(server: HarnessMcpServer, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = server.handle(request)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()


def main() -> int:
    try:
        spool = HarnessSpool.from_environment()
        serve(HarnessMcpServer(spool))
        return 0
    except Exception as exc:
        print(f"bagent harness MCP error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
