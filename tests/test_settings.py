from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from batchagent.settings import DEFAULT_SETTINGS, SETTINGS_VERSION, SettingsError, load_settings, save_settings, update_settings


class SettingsTests(unittest.TestCase):
    def test_missing_settings_return_independent_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            first = load_settings(path)
            first["theme"] = "changed"
            first["batch_configs"].append("/tmp/demo.md")
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)
            self.assertFalse(path.exists())

    def test_save_and_update_settings_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "settings.json"
            self.assertEqual(save_settings({"theme": "textual-light", "custom": {"compact": True}}, path), path)
            values = update_settings({"theme": "nord", "version": 999}, path)

            self.assertEqual(values["theme"], "nord")
            self.assertEqual(values["version"], SETTINGS_VERSION)
            self.assertEqual(load_settings(path)["custom"], {"compact": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], SETTINGS_VERSION)
            self.assertEqual(list(path.parent.glob("settings.json.*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_invalid_settings_raise_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(SettingsError, "JSON object"):
                load_settings(path)

            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(SettingsError, "cannot read settings"):
                load_settings(path)

    def test_non_serializable_settings_are_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with self.assertRaisesRegex(SettingsError, "not JSON serializable"):
                save_settings({"bad": object()}, path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
