#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility wrapper for the organized Pi setup checker."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from uady_fpga_pi.config import PiSecureConfigManager

if __name__ == "__main__":
    ok = PiSecureConfigManager().print_setup_status()
    if not ok:
        raise SystemExit(1)
