#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Set the remote History_of_jobs folder in protected Raspberry Pi storage."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from uady_secure_store import get_pi_config_section, pi_private_config_path, set_pi_config_section

DEFAULT_HISTORY_DIR = "/home/lab4p0/History_of_jobs"


def main() -> int:
    path = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_HISTORY_DIR
    if not path.startswith("/"):
        print("[FAIL] History folder must be an absolute Linux path, for example /home/lab4p0/History_of_jobs")
        return 2
    hist = get_pi_config_section("server_history", {})
    hist["base_dir"] = path.rstrip("/")
    hist["enabled"] = True
    hist["record_format"] = hist.get("record_format", "txt")
    hist["one_record_per_job"] = True
    hist["record_on_queue_accept"] = True
    set_pi_config_section("server_history", hist)
    print("[OK] Job history folder saved in protected Raspberry Pi storage.")
    print(f"History folder: {hist['base_dir']}")
    print(f"Storage file:    {pi_private_config_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
