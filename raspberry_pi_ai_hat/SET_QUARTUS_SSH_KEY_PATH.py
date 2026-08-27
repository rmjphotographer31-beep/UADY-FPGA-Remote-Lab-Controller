#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility wrapper for saving the Quartus SSH private-key path."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from uady_secure_store import pi_secret_path
from uady_fpga_pi.config import PiSecureConfigManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Save Quartus SSH private-key path in protected Pi storage.")
    parser.add_argument("key_path", help="Full path to the private SSH key on this Raspberry Pi")
    args = parser.parse_args()

    key_file = PiSecureConfigManager().save_quartus_ssh_key_path(args.key_path)
    if not key_file.exists():
        print(f"[WARN] File does not exist yet: {key_file}")
        print("       Saving it anyway. Make sure the key exists before starting the controller.")
    print("[OK] Quartus SSH key path saved in protected Raspberry Pi storage.")
    print(f"Storage file: {pi_secret_path()}")
    print(f"Current path: {key_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
