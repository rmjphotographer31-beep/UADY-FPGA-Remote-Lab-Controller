#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility wrapper for saving private Pi Quartus server settings.

Normal users should run ``python3 UADY_PI.py``. This file remains for advanced
repair and scripts that already call it.
"""
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

from uady_secure_store import get_pi_config_section, pi_private_config_path, set_pi_config_section
from uady_fpga_pi.config import PiSecureConfigManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store Quartus server settings in protected Pi storage.")
    parser.add_argument("--host", required=True, help="Quartus server IP or hostname, usually the NetBird IP")
    parser.add_argument("--user", required=True, help="SSH username on the Quartus server")
    parser.add_argument("--quartus-standard", required=True)
    parser.add_argument("--quartus-pro", required=True)
    parser.add_argument("--standard-project-path", required=True)
    parser.add_argument("--pro-project-path", required=True)
    parser.add_argument("--standard-log-file", required=True)
    parser.add_argument("--pro-log-file", required=True)
    parser.add_argument("--ssh-timeout-seconds", default="60")
    parser.add_argument("--program-timeout-seconds", default="900")
    parser.add_argument("--sof-copy-timeout-seconds", default="180")
    parser.add_argument("--standard-program-timeout-seconds", default="75")
    parser.add_argument("--pro-program-timeout-seconds", default="900")
    parser.add_argument("--standard-sof-copy-timeout-seconds", default="60")
    parser.add_argument("--pro-sof-copy-timeout-seconds", default="180")
    parser.add_argument("--sof-copy-attempts", default="2")
    parser.add_argument("--history-base-dir", default="", help="Optional remote folder for job history records")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    PiSecureConfigManager().save_private_jtag_values({
        "host": args.host,
        "user": args.user,
        "quartus_standard": args.quartus_standard,
        "quartus_pro": args.quartus_pro,
        "standard_project_path": args.standard_project_path,
        "pro_project_path": args.pro_project_path,
        "standard_log_file": args.standard_log_file,
        "pro_log_file": args.pro_log_file,
        "ssh_timeout_seconds": args.ssh_timeout_seconds,
        "program_timeout_seconds": args.program_timeout_seconds,
        "sof_copy_timeout_seconds": args.sof_copy_timeout_seconds,
        "standard_program_timeout_seconds": args.standard_program_timeout_seconds,
        "pro_program_timeout_seconds": args.pro_program_timeout_seconds,
        "standard_sof_copy_timeout_seconds": args.standard_sof_copy_timeout_seconds,
        "pro_sof_copy_timeout_seconds": args.pro_sof_copy_timeout_seconds,
        "sof_copy_attempts": args.sof_copy_attempts,
    })
    if args.history_base_dir:
        hist = get_pi_config_section("server_history", {})
        hist["base_dir"] = args.history_base_dir
        set_pi_config_section("server_history", hist)
    print("Quartus server settings saved to protected Raspberry Pi storage:")
    print(pi_private_config_path())
    print("The SSH private key path is set separately with UADY_PI.py --setup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
