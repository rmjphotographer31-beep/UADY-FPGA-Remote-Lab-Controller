"""Private Raspberry Pi configuration and secret management."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from uady_secure_store import (
    get_or_create_pi_api_key,
    get_or_create_pi_terminal_key,
    get_pi_config_section,
    get_quartus_ssh_key_path,
    pi_private_config_path,
    pi_secret_path,
    read_json,
    set_pi_config_section,
    set_quartus_ssh_key_path,
    write_json_secure,
)

from .paths import QUARTUS_FIELDS, TIMEOUT_DEFAULTS

SETUP_CACHE = pi_private_config_path("pi_setup_answers.json")


@dataclass
class SetupStatus:
    """Human-readable setup status for Quartus/JTAG on the Raspberry Pi."""

    server_info: Dict[str, str]
    ssh_key_path: str
    ssh_key_exists: bool
    missing: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


class PiSecureConfigManager:
    """Owns all Pi-side private settings and generated GUI keys.

    Nothing in this class writes lab IPs, users, SSH key paths, API keys, or
    terminal keys into the shipped source folder.  Deployment values are saved
    through ``uady_secure_store`` into protected user storage.
    """

    required_server_fields = ("host", "user")

    def load_setup_cache(self) -> Dict[str, Any]:
        data = read_json(SETUP_CACHE, {})
        return data if isinstance(data, dict) else {}

    def save_setup_cache(self, values: Dict[str, Any]) -> None:
        clean = {k: v for k, v in values.items() if str(v or "").strip()}
        write_json_secure(SETUP_CACHE, clean)

    @staticmethod
    def default_history_base_dir(server_user: str = "") -> str:
        """Default remote folder for lightweight job history records.

        The lab uses one shared field directory for lightweight job records.
        """
        return "/home/lab4p0/History_of_jobs"

    def load_saved_quartus_values(self) -> Dict[str, str]:
        cached = self.load_setup_cache()
        saved = get_pi_config_section("quartus_server", {})
        hist = get_pi_config_section("server_history", {})
        out: Dict[str, str] = {}
        for key in QUARTUS_FIELDS:
            out[key] = str(cached.get(key, "") or "")
        for key in QUARTUS_FIELDS:
            if not out.get(key):
                out[key] = str(saved.get(key, "") or "")
        if not out.get("history_base_dir"):
            out["history_base_dir"] = str(hist.get("base_dir", "") or "")
        # Normalize the old derived default (/home/<quartus_user>/History_of_jobs)
        # to the shared field directory requested for the lab.
        old_derived = f"/home/{out.get('user', '').strip()}/History_of_jobs" if out.get("user", "").strip() else ""
        current_history = str(out.get("history_base_dir", "")).rstrip("/")
        legacy_wrong_shared = "/home/History_of_jobs"
        # Migrate older saved values to the actual server folder used by the lab.
        if current_history == legacy_wrong_shared or (old_derived and current_history == old_derived):
            out["history_base_dir"] = self.default_history_base_dir(out.get("user", ""))
        if not out.get("history_base_dir"):
            out["history_base_dir"] = self.default_history_base_dir(out.get("user", ""))
        out["ssh_key_path"] = str(cached.get("ssh_key_path", "") or get_quartus_ssh_key_path("") or "")
        for key, default in TIMEOUT_DEFAULTS.items():
            out[key] = str(saved.get(key, cached.get(key, default)) or default)
        return out

    def candidate_ssh_keys(self) -> List[str]:
        ssh_dir = Path.home() / ".ssh"
        if not ssh_dir.exists():
            return []
        patterns = ["*granja*", "*.pem", "id_rsa", "id_ed25519", "id_ecdsa", "id_*"]
        found: List[str] = []
        for pattern in patterns:
            for path in sorted(ssh_dir.glob(pattern)):
                if path.is_file() and str(path) not in found:
                    found.append(str(path))
        return found

    def save_private_jtag_values(self, values: Dict[str, Any]) -> None:
        """Save Quartus/JTAG values without prompting.

        Used by the Pi wizard, remote Windows setup, and advanced helper scripts.
        """
        qs: Dict[str, Any] = {
            "host": str(values.get("host", "") or "").strip(),
            "user": str(values.get("user", "") or "").strip(),
            "quartus_standard": str(values.get("quartus_standard", "") or "").strip(),
            "quartus_pro": str(values.get("quartus_pro", "") or "").strip(),
            "standard_project_path": str(values.get("standard_project_path", "") or "").strip(),
            "pro_project_path": str(values.get("pro_project_path", "") or "").strip(),
            "standard_log_file": str(values.get("standard_log_file", "") or "").strip(),
            "pro_log_file": str(values.get("pro_log_file", "") or "").strip(),
        }
        missing = [k for k in self.required_server_fields if not qs.get(k)]
        if not (qs.get("quartus_standard") or qs.get("quartus_pro")):
            missing.append("quartus_standard_or_quartus_pro")
        if missing:
            raise ValueError("Missing required setup fields: " + ", ".join(missing))

        for key, default in TIMEOUT_DEFAULTS.items():
            try:
                qs[key] = int(values.get(key, default) or default)
            except Exception:
                qs[key] = int(default)
        set_pi_config_section("quartus_server", qs)

        history_base_dir = str(values.get("history_base_dir", "") or "").strip()
        if not history_base_dir:
            history_base_dir = self.default_history_base_dir(qs.get("user", ""))
        hist = get_pi_config_section("server_history", {})
        if history_base_dir:
            hist["base_dir"] = history_base_dir
        # Make the history folder update as soon as a job is accepted.  Because
        # one_record_per_job is true, later events update that same file rather
        # than creating duplicates.
        hist["enabled"] = True
        hist["record_format"] = hist.get("record_format", "txt")
        hist["one_record_per_job"] = True
        hist["record_on_queue_accept"] = True
        set_pi_config_section("server_history", hist)

        key_path = str(values.get("ssh_key_path", "") or "").strip()
        if key_path:
            self.save_quartus_ssh_key_path(key_path)

        cache = dict(values)
        if key_path:
            cache["ssh_key_path"] = key_path
        self.save_setup_cache(cache)

    def save_quartus_ssh_key_path(self, key_path: str) -> Path:
        clean_path = str(key_path or "").strip()
        if not clean_path:
            raise ValueError("SSH key path is required.")
        set_quartus_ssh_key_path(clean_path)
        key_file = Path(clean_path).expanduser()
        if key_file.exists():
            try:
                os.chmod(key_file, 0o600)
                os.chmod(key_file.parent, 0o700)
            except Exception:
                pass
        return key_file

    def current_server_info(self) -> Dict[str, str]:
        qs = get_pi_config_section("quartus_server", {})
        info = {k: str(qs.get(k, "") or "") for k in QUARTUS_FIELDS}
        info["ssh_key_path"] = get_quartus_ssh_key_path("")
        return info

    def get_setup_status(self) -> SetupStatus:
        info = self.current_server_info()
        key_path = info.get("ssh_key_path", "")
        key_exists = bool(key_path and Path(key_path).expanduser().exists())
        missing: List[str] = []
        if not info.get("host"):
            missing.append("quartus_server.host")
        if not info.get("user"):
            missing.append("quartus_server.user")
        if not (info.get("quartus_standard") or info.get("quartus_pro")):
            missing.append("quartus_server.quartus_standard_or_quartus_pro")
        if not key_path:
            missing.append("quartus_server.ssh_key_path")
        if key_path and not key_exists:
            missing.append("ssh_key_file_exists")
        return SetupStatus(info, key_path, key_exists, missing)

    def print_setup_status(self) -> bool:
        print("\nRaspberry Pi private JTAG setup check")
        print("--------------------------------------")
        print(f"Private config file: {pi_private_config_path()}")
        print(f"Secret file:         {pi_secret_path()}")
        status = self.get_setup_status()
        hist = get_pi_config_section("server_history", {})
        history_dir = str(hist.get("base_dir", "") or self.default_history_base_dir(status.server_info.get("user", "")))
        checks = {
            "Quartus server host configured": bool(status.server_info.get("host")),
            "Quartus server user configured": bool(status.server_info.get("user")),
            "Standard quartus_pgm configured": bool(status.server_info.get("quartus_standard")),
            "Pro quartus_pgm configured": bool(status.server_info.get("quartus_pro")),
            "SSH key path configured": bool(status.ssh_key_path),
            "SSH key file exists": status.ssh_key_exists,
            "Job history folder configured": bool(history_dir),
        }
        for label, ok in checks.items():
            print(f"{label + ':':38} {'yes' if ok else 'NO'}")
        if history_dir:
            print(f"Job history folder:                 {history_dir}")

        if status.missing:
            print("\n[FAIL] Missing required private setup:")
            for item in status.missing:
                print(f"  - {item}")
            print("\nFix: run python3 UADY_PI.py --setup")
            return False
        print("\n[OK] Private Quartus/JTAG setup exists.")
        return True

    def print_keys(self) -> None:
        print("\nRaspberry Pi keys for GUI pairing")
        print("---------------------------------")
        print("Pi API key:")
        print(get_or_create_pi_api_key())
        print("\nGUI Terminal key:")
        print(get_or_create_pi_terminal_key())
        print(f"\nStored outside source/config at: {pi_secret_path()}")
