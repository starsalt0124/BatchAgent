from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .paths import ensure_private_dir, ensure_private_file, settings_path
from .util import atomic_write_text


SETTINGS_VERSION = 1
DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SETTINGS_VERSION,
    "theme": "textual-dark",
    "harness": "native",
    "batch_configs": [],
}


class SettingsError(ValueError):
    pass


def load_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or settings_path()
    if not target.exists():
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"cannot read settings from {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError(f"settings must be a JSON object: {target}")
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings.update(raw)
    return settings


def save_settings(settings: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path or settings_path()
    values = copy.deepcopy(DEFAULT_SETTINGS)
    values.update(settings)
    values["version"] = SETTINGS_VERSION
    try:
        payload = json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"settings are not JSON serializable: {exc}") from exc
    ensure_private_dir(target.parent)
    atomic_write_text(target, payload)
    ensure_private_file(target)
    return target


def update_settings(changes: Mapping[str, Any], path: Path | None = None) -> dict[str, Any]:
    values = load_settings(path)
    values.update(changes)
    values["version"] = SETTINGS_VERSION
    save_settings(values, path)
    return values
