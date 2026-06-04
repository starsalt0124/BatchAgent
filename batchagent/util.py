from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    value = value.strip(".-")
    return value or "task"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def write_json(path: Path, item: Any) -> None:
    atomic_write_text(path, json.dumps(item, ensure_ascii=False, indent=2, default=str) + "\n")


def safe_join(root: Path, user_path: str, *, must_exist: bool = False) -> Path:
    root = root.resolve()
    candidate = Path(user_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if must_exist:
        candidate = candidate.resolve(strict=True)
    else:
        candidate = candidate.resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {user_path}")
    return candidate


def truncate(value: str, limit: int = 500) -> str:
    value = value.replace("\r", " ").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


class ManifestLock:
    def __init__(self, manifest_path: Path):
        self.path = manifest_path.with_suffix(manifest_path.suffix + ".lock")
        self.fd: int | None = None

    def acquire(self) -> None:
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = f"pid={os.getpid()}\n"
            os.write(self.fd, payload.encode("utf-8"))
        except FileExistsError as exc:
            raise RuntimeError(f"manifest lock exists: {self.path}") from exc

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "ManifestLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

