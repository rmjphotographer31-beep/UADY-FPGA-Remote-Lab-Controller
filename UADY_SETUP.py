#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-command setup wizard for the UADY FPGA Lab GUI and Raspberry Pi controller.

Run this instead of running many individual helper scripts.

Windows / GUI computer:
    python UADY_SETUP.py

Raspberry Pi only:
    python3 UADY_SETUP.py --pi-local

Security model:
- This file contains NO lab IPs, usernames, SSH key paths, API keys, terminal
  keys, or queue tokens.
- Values you enter are saved into private OS/user storage, not beside gui.py and
  not inside config.ini/config_pi_hat.json.
- The SSH private key file itself is not copied unless you explicitly choose the
  optional controller/key copy step.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Optional

try:
    from uady_secure_store import (
        user_config_path,
        read_json,
        write_json_secure,
        get_pi_config_section,
        get_quartus_ssh_key_path,
        pi_private_config_path,
        pi_secret_path,
        get_or_create_pi_api_key,
        get_or_create_pi_terminal_key,
    )
    from SETUP_CLASSIC_AND_PI import (
        save_classic_profile,
        save_pi_setup_local,
        save_pi_setup_remote,
    )
except Exception as exc:
    print("[FAIL] Could not import the setup helpers from this folder.")
    print("       Run this script from the GUI folder or raspberry_pi_ai_hat folder.")
    print(f"       Import error: {exc}")
    raise SystemExit(2)

PROFILE_PATH = user_config_path("setup_profile.json")

# Fields intentionally have empty defaults. Do not hardcode deployment values here.
SETUP_FIELDS = [
    "remote_pi_host",
    "remote_pi_user",
    "remote_pi_controller_path",
    "profile",
    "classic_ip_local",
    "classic_ip_netbird",
    "quartus_server_host",
    "quartus_server_user",
    "quartus_standard",
    "quartus_pro",
    "standard_project_path",
    "pro_project_path",
    "standard_log_file",
    "pro_log_file",
    "pi_quartus_ssh_key_path",
    "history_base_dir",
]

TIMEOUT_DEFAULTS = {
    "ssh_timeout_seconds": "60",
    "program_timeout_seconds": "900",
    "sof_copy_timeout_seconds": "180",
    "standard_program_timeout_seconds": "75",
    "pro_program_timeout_seconds": "900",
    "standard_sof_copy_timeout_seconds": "60",
    "pro_sof_copy_timeout_seconds": "180",
    "sof_copy_attempts": "2",
}

BANNER = """
============================================================
 UADY FPGA Lab one-command secure setup
============================================================
This wizard replaces the separate setup commands for:
  - Classic Mode profile
  - Raspberry Pi Quartus/JTAG server settings
  - Raspberry Pi Quartus SSH key path
  - Setup checks
Nothing is saved inside the project folder.
""".strip()


def _print_banner() -> None:
    print(BANNER)
    print(f"Private setup profile cache: {PROFILE_PATH}")
    print()


def _default_pi_user() -> str:
    return (os.environ.get("USERNAME", "") or os.environ.get("USER", "") or "pi").strip() or "pi"


def _default_pi_folder(values: Dict[str, str]) -> str:
    user = str(values.get("remote_pi_user", "") or _default_pi_user()).strip()
    return f"/home/{user}/raspberry_pi_ai_hat"


def _is_windows() -> bool:
    return os.name == "nt"


def _yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{prompt} [{suffix}]: ").strip().lower()
        if not ans:
            return default
        if ans in {"y", "yes", "s", "si", "sí"}:
            return True
        if ans in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def _ask(prompt: str, default: str = "", required: bool = False) -> str:
    default = str(default or "")
    shown = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{shown}: ").strip()
        if not value and default:
            value = default
        if value or not required:
            return value
        print("This value is required.")


def _load_setup_profile() -> Dict[str, str]:
    data = read_json(PROFILE_PATH, {})
    out: Dict[str, str] = {}
    if isinstance(data, dict):
        for key in SETUP_FIELDS:
            out[key] = str(data.get(key, "") or "")
        for key, value in TIMEOUT_DEFAULTS.items():
            out[key] = str(data.get(key, value) or value)
    else:
        for key in SETUP_FIELDS:
            out[key] = ""
        out.update(TIMEOUT_DEFAULTS)
    return out


def _save_setup_profile(values: Dict[str, str]) -> None:
    cleaned: Dict[str, str] = {}
    for key in SETUP_FIELDS:
        if str(values.get(key, "") or "").strip():
            cleaned[key] = str(values.get(key, "")).strip()
    for key, default in TIMEOUT_DEFAULTS.items():
        cleaned[key] = str(values.get(key, default) or default).strip()
    write_json_secure(PROFILE_PATH, cleaned)
    print(f"[OK] Saved reusable setup answers in private user storage: {PROFILE_PATH}")


def _load_classic_defaults(values: Dict[str, str]) -> None:
    path = user_config_path("classic_servers.ini")
    cfg = configparser.ConfigParser()
    try:
        if path.exists() and cfg.read(path, encoding="utf-8"):
            profile = cfg.get("servers", "default_profile", fallback=values.get("profile", ""))
            if profile and cfg.has_section(profile):
                sec = cfg[profile]
                values.setdefault("profile", profile)
                values["profile"] = values.get("profile") or profile
                mapping = {
                    "classic_ip_local": "ip_local",
                    "classic_ip_netbird": "ip_netbird",
                    "quartus_server_user": "user",
                    "quartus_standard": "quartus_path",
                    "standard_project_path": "base_project_path",
                    "standard_log_file": "log_file_path",
                    "quartus_pro": "quartus_primepro_path",
                    "pro_project_path": "primepro_project_path",
                    "pro_log_file": "primepro_log_file_path",
                }
                for target, source in mapping.items():
                    if not values.get(target):
                        values[target] = sec.get(source, "")
    except Exception:
        pass


def _load_pi_local_defaults(values: Dict[str, str]) -> None:
    try:
        pi_cfg = get_pi_config_section("quartus_server", {})
        mapping = {
            "quartus_server_host": "host",
            "quartus_server_user": "user",
            "quartus_standard": "quartus_standard",
            "quartus_pro": "quartus_pro",
            "standard_project_path": "standard_project_path",
            "pro_project_path": "pro_project_path",
            "standard_log_file": "standard_log_file",
            "pro_log_file": "pro_log_file",
        }
        for target, source in mapping.items():
            if not values.get(target):
                values[target] = str(pi_cfg.get(source, "") or "")
        if not values.get("pi_quartus_ssh_key_path"):
            values["pi_quartus_ssh_key_path"] = get_quartus_ssh_key_path("")
    except Exception:
        pass


def _prompt_shared_lab_values(values: Dict[str, str], require_pi_host: bool) -> None:
    print("\nQuartus server / JTAG values")
    print("Press Enter to keep a shown default. Blank required values will be asked again.")
    values["quartus_server_host"] = _ask("Quartus server IP/host used by the Raspberry Pi", values.get("quartus_server_host", ""), required=require_pi_host)
    values["quartus_server_user"] = _ask("Quartus server SSH username", values.get("quartus_server_user", ""), required=True)
    values["quartus_standard"] = _ask("Standard quartus_pgm path", values.get("quartus_standard", ""), required=True)
    values["quartus_pro"] = _ask("Pro/Agilex quartus_pgm path", values.get("quartus_pro", ""), required=True)
    values["standard_project_path"] = _ask("Standard projects folder on Quartus server", values.get("standard_project_path", ""), required=True)
    values["pro_project_path"] = _ask("Pro/Agilex projects folder on Quartus server", values.get("pro_project_path", ""), required=True)
    values["standard_log_file"] = _ask("Standard log file on Quartus server", values.get("standard_log_file", ""), required=True)
    values["pro_log_file"] = _ask("Pro/Agilex log file on Quartus server", values.get("pro_log_file", ""), required=True)
    default_history = values.get("history_base_dir", "") or (f"/home/{values.get('quartus_server_user', '').strip()}/History_of_jobs" if values.get("quartus_server_user", "").strip() else "")
    values["history_base_dir"] = _ask("Remote History_of_jobs folder", default_history, required=True)
    values["pi_quartus_ssh_key_path"] = _ask("Quartus SSH key path as it exists on the Raspberry Pi", values.get("pi_quartus_ssh_key_path", ""), required=True)


def _prompt_classic_values(values: Dict[str, str]) -> None:
    print("\nClassic Mode values")
    values["profile"] = _ask("Classic profile name", values.get("profile", "") or "CLASSIC_SERVER", required=True)
    values["classic_ip_local"] = _ask("Quartus server local/LAN IP for Classic Mode", values.get("classic_ip_local", ""), required=False)
    values["classic_ip_netbird"] = _ask("Quartus server NetBird IP for Classic Mode", values.get("classic_ip_netbird", "") or values.get("quartus_server_host", ""), required=True)
    if not values.get("classic_ip_local"):
        values["classic_ip_local"] = values["classic_ip_netbird"]


def _prompt_remote_pi(values: Dict[str, str]) -> None:
    print("\nRaspberry Pi remote setup values")
    values["remote_pi_host"] = _ask("Raspberry Pi IP/host for SSH", values.get("remote_pi_host", ""), required=True)
    values["remote_pi_user"] = _ask("Raspberry Pi SSH username", values.get("remote_pi_user", "") or _default_pi_user(), required=True)
    values["remote_pi_controller_path"] = _ask("Raspberry Pi controller folder", values.get("remote_pi_controller_path", "") or _default_pi_folder(values), required=True)


def _as_namespace(values: Dict[str, str]) -> SimpleNamespace:
    payload = dict(values)
    for key, default in TIMEOUT_DEFAULTS.items():
        payload[key] = str(payload.get(key, default) or default)
    return SimpleNamespace(**payload)


def _run(cmd: Iterable[str], *, cwd: Optional[Path] = None, check: bool = True) -> int:
    print("[RUN] " + " ".join(shlex.quote(str(x)) for x in cmd))
    p = subprocess.run(list(map(str, cmd)), cwd=str(cwd) if cwd else None)
    if check and p.returncode != 0:
        raise SystemExit(p.returncode)
    return int(p.returncode)


def copy_controller_to_pi(values: Dict[str, str]) -> None:
    controller = Path(__file__).resolve().parent / "raspberry_pi_ai_hat"
    if not controller.exists():
        print("[WARN] raspberry_pi_ai_hat folder is not beside this script, so it cannot be copied.")
        return
    host = values.get("remote_pi_host", "").strip()
    user = values.get("remote_pi_user", "").strip() or _default_pi_user()
    if not host:
        print("[WARN] Raspberry Pi host is blank; skipping controller copy.")
        return
    target = f"{user}@{host}:/home/{user}/"
    print("\n[INFO] Copying raspberry_pi_ai_hat folder to the Raspberry Pi with scp.")
    print("       You may be asked for the Raspberry Pi password.")
    _run(["scp", "-r", str(controller), target])


def remote_check_pi(values: Dict[str, str]) -> None:
    host = values.get("remote_pi_host", "").strip()
    user = values.get("remote_pi_user", "").strip() or _default_pi_user()
    folder = values.get("remote_pi_controller_path", "").strip() or _default_pi_folder(values)
    if not host:
        print("[WARN] Raspberry Pi host is blank; skipping remote check.")
        return
    cmd = f"cd {shlex.quote(folder)} && python3 CHECK_PI_JTAG_SETUP.py"
    _run(["ssh", f"{user}@{host}", cmd], check=False)


def remote_print_keys(values: Dict[str, str]) -> None:
    host = values.get("remote_pi_host", "").strip()
    user = values.get("remote_pi_user", "").strip() or _default_pi_user()
    folder = values.get("remote_pi_controller_path", "").strip() or _default_pi_folder(values)
    if not host:
        print("[WARN] Raspberry Pi host is blank; skipping key print.")
        return
    cmd = f"cd {shlex.quote(folder)} && python3 PRINT_PI_API_KEY.py && python3 PRINT_TERMINAL_KEY.py"
    _run(["ssh", f"{user}@{host}", cmd], check=False)


def local_check_pi() -> None:
    check_script = Path(__file__).resolve().parent / "CHECK_PI_JTAG_SETUP.py"
    if check_script.exists():
        _run([sys.executable, str(check_script)], check=False)
    else:
        # When running from GUI folder, CHECK_PI_JTAG_SETUP.py is in the Pi subfolder.
        check_script = Path(__file__).resolve().parent / "raspberry_pi_ai_hat" / "CHECK_PI_JTAG_SETUP.py"
        if check_script.exists():
            _run([sys.executable, str(check_script)], check=False)
        else:
            print("[WARN] CHECK_PI_JTAG_SETUP.py was not found beside this script.")


def local_print_keys() -> None:
    print("\nRaspberry Pi keys from protected storage on this machine")
    print("------------------------------------------------------")
    print("Pi API key:")
    print(get_or_create_pi_api_key())
    print("\nTerminal key:")
    print(get_or_create_pi_terminal_key())
    print(f"\nSecrets file: {pi_secret_path()}")


def full_windows_wizard(values: Dict[str, str], *, copy_first: Optional[bool] = None, print_keys: bool = True) -> None:
    _prompt_remote_pi(values)
    _prompt_shared_lab_values(values, require_pi_host=True)
    _prompt_classic_values(values)
    _save_setup_profile(values)

    if copy_first is None:
        copy_first = _yes_no("Copy/update raspberry_pi_ai_hat folder to the Pi first", default=False)
    if copy_first:
        copy_controller_to_pi(values)

    print("\n[STEP] Saving Classic Mode profile on this computer...")
    save_classic_profile(_as_namespace(values))

    print("\n[STEP] Saving Raspberry Pi/JTAG setup remotely...")
    save_pi_setup_remote(_as_namespace(values))

    if print_keys:
        print("\n[STEP] Printing Pi API key and Terminal key from the Raspberry Pi...")
        remote_print_keys(values)

    print("\n[DONE] Full setup finished.")
    print("Next: restart the Raspberry Pi controller, then open the GUI and click Test Pi / Refresh JTAG.")


def classic_only_wizard(values: Dict[str, str]) -> None:
    _prompt_shared_lab_values(values, require_pi_host=False)
    _prompt_classic_values(values)
    _save_setup_profile(values)
    save_classic_profile(_as_namespace(values))
    print("[DONE] Classic Mode profile saved.")


def pi_local_wizard(values: Dict[str, str]) -> None:
    _prompt_shared_lab_values(values, require_pi_host=True)
    _save_setup_profile(values)
    save_pi_setup_local(_as_namespace(values))
    local_check_pi()
    print("[DONE] Raspberry Pi local setup saved.")


def pi_remote_wizard(values: Dict[str, str]) -> None:
    _prompt_remote_pi(values)
    _prompt_shared_lab_values(values, require_pi_host=True)
    _save_setup_profile(values)
    save_pi_setup_remote(_as_namespace(values))
    print("[DONE] Raspberry Pi remote setup saved.")


def show_menu(default_choice: str) -> str:
    print("Choose setup action:")
    print("  1) Full setup from this GUI computer: Classic + remote Raspberry Pi")
    print("  2) Classic Mode only on this computer")
    print("  3) Raspberry Pi setup only on this Raspberry Pi")
    print("  4) Raspberry Pi setup remotely over SSH")
    print("  5) Check Raspberry Pi setup")
    print("  6) Print Pi API key + Terminal key")
    print("  7) Copy/update raspberry_pi_ai_hat folder to Pi")
    print("  0) Exit")
    return _ask("Selection", default_choice, required=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="One-command secure setup wizard for UADY FPGA Lab.")
    p.add_argument("--wizard", action="store_true", help="Open interactive menu, same as no arguments")
    p.add_argument("--full", action="store_true", help="Interactive full setup: Classic + remote Pi")
    p.add_argument("--classic", action="store_true", help="Interactive Classic-only setup")
    p.add_argument("--pi-local", action="store_true", help="Interactive local Raspberry Pi setup")
    p.add_argument("--pi-remote", action="store_true", help="Interactive remote Raspberry Pi setup")
    p.add_argument("--check", action="store_true", help="Run setup check")
    p.add_argument("--print-keys", action="store_true", help="Print Pi API key and Terminal key")
    p.add_argument("--copy-controller", action="store_true", help="Copy raspberry_pi_ai_hat folder to Pi using saved/prompted host")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _print_banner()

    values = _load_setup_profile()
    _load_classic_defaults(values)
    _load_pi_local_defaults(values)

    if args.full:
        full_windows_wizard(values)
        return 0
    if args.classic:
        classic_only_wizard(values)
        return 0
    if args.pi_local:
        pi_local_wizard(values)
        return 0
    if args.pi_remote:
        pi_remote_wizard(values)
        return 0
    if args.copy_controller:
        _prompt_remote_pi(values)
        _save_setup_profile(values)
        copy_controller_to_pi(values)
        return 0
    if args.check:
        if _is_windows():
            if _yes_no("Check the Raspberry Pi remotely over SSH", default=True):
                _prompt_remote_pi(values)
                _save_setup_profile(values)
                remote_check_pi(values)
            else:
                local_check_pi()
        else:
            local_check_pi()
        return 0
    if args.print_keys:
        if _is_windows():
            _prompt_remote_pi(values)
            _save_setup_profile(values)
            remote_print_keys(values)
        else:
            local_print_keys()
        return 0

    # Default interactive menu.
    default_choice = "1" if _is_windows() else "3"
    while True:
        choice = show_menu(default_choice).strip()
        if choice == "1":
            full_windows_wizard(values)
            return 0
        if choice == "2":
            classic_only_wizard(values)
            return 0
        if choice == "3":
            pi_local_wizard(values)
            return 0
        if choice == "4":
            pi_remote_wizard(values)
            return 0
        if choice == "5":
            if _is_windows():
                _prompt_remote_pi(values)
                _save_setup_profile(values)
                remote_check_pi(values)
            else:
                local_check_pi()
            return 0
        if choice == "6":
            if _is_windows():
                _prompt_remote_pi(values)
                _save_setup_profile(values)
                remote_print_keys(values)
            else:
                local_print_keys()
            return 0
        if choice == "7":
            _prompt_remote_pi(values)
            _save_setup_profile(values)
            copy_controller_to_pi(values)
            return 0
        if choice == "0":
            print("Cancelled.")
            return 0
        print("Unknown selection. Choose 0-7.")


if __name__ == "__main__":
    raise SystemExit(main())
