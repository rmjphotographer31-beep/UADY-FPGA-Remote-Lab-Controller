# -*- coding: utf-8 -*-
"""Compatibility wrapper for the class-based Classic FPGA backend.

The real implementation now lives in src/uady_fpga_gui/classic_fpga.py.
Keeping this file means the existing gui.py can still do `import FPGA` while
new code can import the class directly:

    from uady_fpga_gui import ClassicFPGAProgrammer
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from uady_fpga_gui import classic_fpga as _backend

ClassicFPGAProgrammer = _backend.ClassicFPGAProgrammer
ClassicServerProfile = _backend.ClassicServerProfile
parse_quartus_cables = _backend.parse_quartus_cables
classic_config_storage_path = _backend.classic_config_storage_path
obtener_config_servidor = _backend.obtener_config_servidor
conectar_ssh = _backend.conectar_ssh
_run_remote = _backend._run_remote
_parse_quartus_cables = _backend._parse_quartus_cables
detectar_fpgas_disponibles = _backend.detectar_fpgas_disponibles
pgmlist = _backend.pgmlist
_quartus_for_board = _backend._quartus_for_board
_base_dir_for_board = _backend._base_dir_for_board
_jtag_index_for_board = _backend._jtag_index_for_board
_program_via_jtag = _backend._program_via_jtag
ssh_conection = _backend.ssh_conection
dse = _backend.dse
registrar_log = _backend.registrar_log
logs = _backend.logs

print("=" * 60)
print("[OK] FPGA.py - Motor OpenSSH/Quartus del servidor")
print("[OK] Backend class: uady_fpga_gui.ClassicFPGAProgrammer")
print("=" * 60)
