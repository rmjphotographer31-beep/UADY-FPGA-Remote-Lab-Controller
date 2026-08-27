#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One Raspberry Pi-side command for the UADY FPGA Lab controller.

Normal users still run only this file:

    cd ~/raspberry_pi_ai_hat
    python3 UADY_PI.py

The implementation is organized in ``src/uady_fpga_pi`` so setup, keys,
JTAG testing, and controller startup are easier to maintain.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from uady_fpga_pi.cli import main
except Exception as exc:  # pragma: no cover - startup diagnostic
    print("[FAIL] Could not load the Raspberry Pi manager package.")
    print("       Run this from inside raspberry_pi_ai_hat:")
    print("       cd ~/raspberry_pi_ai_hat && python3 UADY_PI.py")
    print(f"       Import error: {exc}")
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
