"""Shared Raspberry Pi-side filesystem paths."""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
PI_ROOT = SRC_DIR.parent

CONFIG_PATH = PI_ROOT / "config_pi_hat.json"
REQUIREMENTS = PI_ROOT / "requirements_pi.txt"
CONTROLLER = PI_ROOT / "pi_ai_hat_controller.py"
VENV_DIR = PI_ROOT / ".venv"
LOG_DIR = PI_ROOT / "controller_logs"

APP_TITLE = "UADY FPGA Lab Raspberry Pi Manager"

TIMEOUT_DEFAULTS = {
    "ssh_timeout_seconds": 60,
    "program_timeout_seconds": 900,
    "sof_copy_timeout_seconds": 180,
    "standard_program_timeout_seconds": 75,
    "pro_program_timeout_seconds": 900,
    "standard_sof_copy_timeout_seconds": 60,
    "pro_sof_copy_timeout_seconds": 180,
    "sof_copy_attempts": 2,
}

QUARTUS_FIELDS = [
    "host",
    "user",
    "quartus_standard",
    "quartus_pro",
    "standard_project_path",
    "pro_project_path",
    "standard_log_file",
    "pro_log_file",
    "history_base_dir",
]
