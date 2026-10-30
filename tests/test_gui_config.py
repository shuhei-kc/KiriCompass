"""Regression tests for reading the viewer's gui_config.json."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

try:
    import tkinter  # noqa: F401 - precedent_gui imports it at module level
except ImportError:  # pragma: no cover - headless Python without Tk
    tkinter = None


def _load_gui():
    path = Path(__file__).resolve().parents[1] / "tools" / "precedent_gui.py"
    spec = importlib.util.spec_from_file_location("precedent_gui", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(tkinter is None, "tkinter is not available")
class GuiConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gui = _load_gui()

    def test_utf8_config_with_japanese_path_is_read(self) -> None:
        config = {"db_path": r"C:\Users\将棋\KiriCompass\data\csa.db"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui_config.json"
            path.write_text(json.dumps(config, ensure_ascii=False),
                            encoding="utf-8")
            self.assertEqual(self.gui.read_config(path), config)

    def test_unreadable_config_does_not_stop_startup(self) -> None:
        # 旧版が Windows (cp932) で書いた設定・壊れた設定・dict以外
        legacy = json.dumps({"db_path": r"C:\Users\将棋\csa.db"},
                            ensure_ascii=False).encode("cp932")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui_config.json"
            for raw in (legacy, b"{broken", b"[1, 2]"):
                with self.subTest(raw=raw[:12]):
                    path.write_bytes(raw)
                    result = self.gui.read_config(path)
                    self.assertTrue(result is None or isinstance(result, dict))
            self.assertIsNone(self.gui.read_config(Path(tmp) / "missing.json"))


if __name__ == "__main__":
    unittest.main()
