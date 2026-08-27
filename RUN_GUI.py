# -*- coding: utf-8 -*-
"""Simple source-layout launcher for the GUI."""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from uady_fpga_gui.launch import run_legacy_gui

if __name__ == "__main__":
    run_legacy_gui()
