from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .models import ArtifactSubmission, BatchConfig, Task
from .util import safe_join, truncate


class ArtifactValidationError(RuntimeError):
    pass


def validate_artifact(
    config: BatchConfig,
    task: Task,
    workspace: Path,
    run_dir: Path,
    artifact: ArtifactSubmission | None,
) -> None:
    if config.artifact.require_submit and artifact is None:
        raise ArtifactValidationError("agent did not call submit_artifact")
    if artifact is None:
        return

    if config.artifact.require_artifact_path and not artifact.artifact_path:
        raise ArtifactValidationError("artifact_path is required")

    artifact_abs = ""
    if artifact.artifact_path:
        try:
            path = safe_join(workspace, artifact.artifact_path, must_exist=True)
        except Exception as exc:
            raise ArtifactValidationError(f"artifact_path is invalid: {artifact.artifact_path}") from exc
        artifact_abs = str(path)

    missing = [key for key in config.artifact.required_metadata_keys if key not in artifact.metadata]
    if missing:
        raise ArtifactValidationError(f"artifact metadata missing keys: {', '.join(missing)}")

    if config.artifact.validator_command:
        _run_validator(config, task, workspace, run_dir, artifact, artifact_abs)


def _run_validator(
    config: BatchConfig,
    task: Task,
    workspace: Path,
    run_dir: Path,
    artifact: ArtifactSubmission,
    artifact_abs: str,
) -> None:
    substitutions = {
        "task_id": task.id,
        "artifact_path": artifact_abs,
        "artifact_path_raw": artifact.artifact_path,
        "run_dir": str(run_dir),
        "workspace": str(workspace),
    }
    command = [item.format(**substitutions) for item in config.artifact.validator_command]
    env = os.environ.copy()
    env.update(
        {
            "BATCHAGENT_TASK_ID": task.id,
            "BATCHAGENT_RUN_DIR": str(run_dir),
            "BATCHAGENT_WORKSPACE": str(workspace),
            "BATCHAGENT_ARTIFACT_PATH": artifact_abs,
        }
    )
    completed = subprocess.run(
        command,
        cwd=str(workspace),
        env=env,
        text=True,
        capture_output=True,
        timeout=config.artifact.validator_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        stderr = truncate(completed.stderr or completed.stdout, 2000)
        raise ArtifactValidationError(f"artifact validator failed with code {completed.returncode}: {stderr}")

