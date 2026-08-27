"""Raspberry Pi-side package for the UADY FPGA Lab controller.

The user-facing command remains ``python3 UADY_PI.py``.  The code behind that
command is organized here so setup, secrets, JTAG testing, and controller
startup are easier to maintain.
"""
from .config import PiSecureConfigManager, SetupStatus
from .jtag import QuartusJtagTester
from .runtime import PiControllerRuntime

__all__ = [
    "PiSecureConfigManager",
    "SetupStatus",
    "QuartusJtagTester",
    "PiControllerRuntime",
]
