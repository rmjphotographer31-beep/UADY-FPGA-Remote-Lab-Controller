#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test that the Raspberry Pi can write job records to the Quartus server history folder."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
venv_python = HERE / ".venv" / "bin" / "python"
python = venv_python if venv_python.exists() else Path(sys.executable)
cmd = [str(python), str(HERE / "pi_ai_hat_controller.py"), "--test-history-write"]
raise SystemExit(subprocess.call(cmd, cwd=str(HERE)))
