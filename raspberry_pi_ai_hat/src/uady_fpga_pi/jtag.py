"""Quartus/JTAG testing through the private Quartus server."""
from __future__ import annotations

import shlex
from typing import List

from .config import PiSecureConfigManager
from .process import capture_command


class QuartusJtagTester:
    """Runs live ``quartus_pgm -l`` checks through SSH from the Raspberry Pi."""

    def __init__(self, config: PiSecureConfigManager | None = None) -> None:
        self.config = config or PiSecureConfigManager()

    def remote_jtag_command(self, label: str, quartus_path: str, timeout: int = 90) -> int:
        info = self.config.current_server_info()
        host = info.get("host", "").strip()
        user = info.get("user", "").strip()
        key = info.get("ssh_key_path", "").strip()
        if not (host and user and key and quartus_path):
            print(f"[SKIP] {label}: missing host/user/key/path")
            return 2
        remote = f"hostname && {shlex.quote(quartus_path)} -l"
        cmd = [
            "ssh",
            "-i", key,
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={min(timeout, 20)}",
            f"{user}@{host}",
            remote,
        ]
        code, out = capture_command(cmd, timeout=timeout)
        if code == 0 and ("USB-Blaster" in out or "Blaster" in out or "DE" in out or "Agilex" in out):
            print(f"[OK] {label}: Quartus/JTAG command returned output.")
        elif code == 0:
            print(f"[WARN] {label}: command worked, but no obvious JTAG cable text was found.")
        else:
            print(f"[FAIL] {label}: command exited with code {code}.")
        return code

    def test_jtag(self) -> bool:
        print("\nTesting JTAG through the Quartus server")
        print("--------------------------------------")
        if not self.config.print_setup_status():
            return False
        info = self.config.current_server_info()
        codes: List[int] = []
        if info.get("quartus_standard"):
            print("\n[TEST] Standard Quartus")
            codes.append(self.remote_jtag_command("Standard Quartus", info["quartus_standard"], timeout=90))
        if info.get("quartus_pro"):
            print("\n[TEST] Pro/Agilex Quartus")
            codes.append(self.remote_jtag_command("Pro/Agilex Quartus", info["quartus_pro"], timeout=120))
        ok = any(code == 0 for code in codes)
        if ok:
            print("\n[OK] At least one Quartus JTAG command worked from the Raspberry Pi.")
        else:
            print("\n[FAIL] No Quartus JTAG command worked from the Raspberry Pi.")
        return ok
