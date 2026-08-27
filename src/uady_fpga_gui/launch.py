# -*- coding: utf-8 -*-
"""Launch helpers for the legacy Tkinter GUI."""
from __future__ import annotations

import runpy
from pathlib import Path


def run_legacy_gui() -> None:
    root = Path(__file__).resolve().parents[2]
    runpy.run_path(str(root / "gui.py"), run_name="__main__")
