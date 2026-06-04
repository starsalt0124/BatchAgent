from __future__ import annotations

import shutil
from pathlib import Path

from .models import BatchConfig, Task
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


def task_run_dir(config: BatchConfig, manifest_path: Path, task: Task, run_id: str) -> Path:
    run_root = Path(config.run_dir)
    if not run_root.is_absolute():
        run_root = manifest_path.parent / run_root
    return (run_root / f"{slugify(task.id)}-{run_id}").resolve()

