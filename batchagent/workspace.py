from __future__ import annotations

import shutil
from pathlib import Path

from .models import BatchConfig, Task
from .paths import runs_dir
from .util import slugify


def prepare_workspace(config: BatchConfig, manifest_path: Path, task: Task, run_dir: Path) -> Path:
    root = Path(config.workspace)
    if not root.is_absolute():
        root = (manifest_path.parent / root).resolve()
    else:
        root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(f"workspace does not exist: {root}")
    mode = config.workspace_mode
    if mode in {"shared", "readonly"}:
        return root
    if mode in {"copy", "isolated-copy"}:
        target = run_dir / "workspace"
        if target.exists():
            return target

        exclude = set(config.copy_exclude)

        def ignore(_dir: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in exclude}
            ignored.update(name for name in names if name.endswith(".pyc"))
            return ignored

        shutil.copytree(root, target, ignore=ignore)
        return target
    raise ValueError(f"unsupported workspace_mode: {mode}")


def task_run_dir(
    config: BatchConfig,
    manifest_path: Path,
    task: Task,
    attempt_id: str,
    batch_run_id: str = "",
) -> Path:
    configured = config.run_dir.strip().replace("\\", "/").rstrip("/")
    legacy_expanded_default = str((Path.home() / ".bagent" / "runs").resolve(strict=False)).replace("\\", "/")
    if configured in {"", ".batchagent/runs", "~/.bagent/runs", legacy_expanded_default}:
        # The pre-v2 default is transparently redirected so existing manifests
        # gain the unified state layout and BAGENT_HOME remains authoritative.
        run_root = runs_dir(create=True)
    else:
        run_root = Path(config.run_dir).expanduser()
        if not run_root.is_absolute():
            run_root = manifest_path.parent / run_root
    if batch_run_id:
        return (run_root / slugify(batch_run_id) / slugify(task.id) / slugify(attempt_id)).resolve()
    return (run_root / f"{slugify(task.id)}-{attempt_id}").resolve()
