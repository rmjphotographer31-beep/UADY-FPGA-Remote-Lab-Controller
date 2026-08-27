# -*- coding: utf-8 -*-
"""UADY FPGA GUI package.

New non-Raspberry GUI logic lives here. The Raspberry Pi controller remains in
raspberry_pi_ai_hat/ and is intentionally kept separate.
"""
from .models import ClassicServerProfile, PiConnectionSettings, LocalSofProject
from .classic_fpga import ClassicFPGAProgrammer, parse_quartus_cables
from .pi_client import PiApiClient
from .background import GuiBackgroundExecutor
from .gui_paths import GuiRuntimePaths

__all__ = [
    "ClassicServerProfile",
    "PiConnectionSettings",
    "LocalSofProject",
    "ClassicFPGAProgrammer",
    "parse_quartus_cables",
    "PiApiClient",
    "GuiBackgroundExecutor",
    "GuiRuntimePaths",
]
