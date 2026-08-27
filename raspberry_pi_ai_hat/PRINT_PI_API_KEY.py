#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print the generated Pi API key for GUI pairing."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from uady_secure_store import get_or_create_pi_api_key, pi_secret_path

if __name__ == "__main__":
    print(get_or_create_pi_api_key())
    print(f"\nStored in protected Pi storage: {pi_secret_path()}")
