"""Command-line and menu interface for the one-file Raspberry Pi command."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from uady_secure_store import pi_private_config_path, pi_secret_path

from .config import PiSecureConfigManager
from .jtag import QuartusJtagTester
from .paths import APP_TITLE, TIMEOUT_DEFAULTS
from .runtime import PiControllerRuntime


class PiManagerCLI:
    """Small UI layer around the Pi setup/runtime classes."""

    def __init__(self) -> None:
        self.config = PiSecureConfigManager()
        self.jtag = QuartusJtagTester(self.config)
        self.runtime = PiControllerRuntime()

    def banner(self) -> None:
        print("=" * 64)
        print(f" {APP_TITLE}")
        print("=" * 64)
        print("One Raspberry Pi command for setup, keys, JTAG test, and controller start.")
        print(f"Private config: {pi_private_config_path()}")
        print(f"Secrets:        {pi_secret_path()}")
        print()

    @staticmethod
    def yes_no(prompt: str, default: bool = True) -> bool:
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

    @staticmethod
    def ask(prompt: str, default: str = "", required: bool = False) -> str:
        default = str(default or "")
        shown = f" [{default}]" if default else ""
        while True:
            value = input(f"{prompt}{shown}: ").strip()
            if not value and default:
                value = default
            if value or not required:
                return value
            print("This value is required.")

    def pick_ssh_key(self, default: str = "") -> str:
        keys = self.config.candidate_ssh_keys()
        default = str(default or "").strip()
        if default and default not in keys:
            keys.insert(0, default)
        if keys:
            print("\nPossible SSH private keys found:")
            for i, key in enumerate(keys, 1):
                marker = " (current)" if key == default else ""
                exists = "" if Path(key).exists() else " [missing]"
                print(f"  {i}) {key}{marker}{exists}")
            choice = self.ask("Choose key number, or type a full path", "1" if not default else str(keys.index(default) + 1), required=True)
            if choice.isdigit() and 1 <= int(choice) <= len(keys):
                return keys[int(choice) - 1]
            return choice
        return self.ask("Quartus SSH private key path on this Raspberry Pi", default, required=True)

    def setup_private_jtag(self) -> None:
        values = self.config.load_saved_quartus_values()
        print("\nPrivate Quartus/JTAG setup")
        print("These values are stored in protected Raspberry Pi storage, not in config_pi_hat.json.")
        values["host"] = self.ask("Quartus server IP/host reachable from this Pi", values.get("host", ""), required=True)
        values["user"] = self.ask("Quartus server SSH username", values.get("user", ""), required=True)
        values["quartus_standard"] = self.ask("Standard quartus_pgm path", values.get("quartus_standard", ""), required=True)
        values["quartus_pro"] = self.ask("Pro/Agilex quartus_pgm path", values.get("quartus_pro", ""), required=True)
        values["standard_project_path"] = self.ask("Standard projects folder on Quartus server", values.get("standard_project_path", ""), required=True)
        values["pro_project_path"] = self.ask("Pro/Agilex projects folder on Quartus server", values.get("pro_project_path", ""), required=True)
        values["standard_log_file"] = self.ask("Standard log file on Quartus server", values.get("standard_log_file", ""), required=True)
        values["pro_log_file"] = self.ask("Pro/Agilex log file on Quartus server", values.get("pro_log_file", ""), required=True)
        values["ssh_key_path"] = self.pick_ssh_key(values.get("ssh_key_path", ""))
        # v4.52: history is no longer an easy-to-miss optional prompt.
        # A default is shown and saved so the History_of_jobs folder updates
        # immediately when a job is accepted.
        values["history_base_dir"] = self.ask(
            "Remote History_of_jobs folder",
            values.get("history_base_dir", "") or self.config.default_history_base_dir(values.get("user", "")),
            required=True,
        )

        self.config.save_private_jtag_values(values)

        key_path = Path(values["ssh_key_path"]).expanduser()
        if not key_path.exists():
            print(f"[WARN] SSH key file does not exist yet: {key_path}")
            print("       Copy the key to that path before starting the controller.")

        print("\n[OK] Private Pi JTAG setup saved.")
        print(f"Private config file: {pi_private_config_path()}")
        print(f"Secret file:         {pi_secret_path()}")

    def show_menu(self) -> str:
        print("Choose action:")
        print("  1) Full Pi setup: save settings + check + test JTAG + print keys")
        print("  2) Setup/update private Quartus/JTAG settings")
        print("  3) Check private Pi setup")
        print("  4) Test JTAG from this Raspberry Pi")
        print("  5) Print Pi API key + GUI Terminal key")
        print("  6) Start controller")
        print("  7) Setup/check then start controller")
        print("  0) Exit")
        return self.ask("Selection", "1", required=True).strip()

    def interactive_menu(self) -> int:
        self.banner()
        while True:
            choice = self.show_menu()
            if choice == "1":
                self.setup_private_jtag()
                ok = self.config.print_setup_status()
                self.jtag.test_jtag()
                self.config.print_keys()
                if ok and self.yes_no("Start the controller now", default=True):
                    self.runtime.start_controller()
                return 0
            if choice == "2":
                self.setup_private_jtag()
                return 0
            if choice == "3":
                return 0 if self.config.print_setup_status() else 1
            if choice == "4":
                return 0 if self.jtag.test_jtag() else 1
            if choice == "5":
                self.config.print_keys()
                return 0
            if choice == "6":
                self.runtime.start_controller()
                return 0
            if choice == "7":
                if not self.config.print_setup_status():
                    self.setup_private_jtag()
                    self.config.print_setup_status()
                self.runtime.start_controller()
                return 0
            if choice == "0":
                print("Cancelled.")
                return 0
            print("Unknown selection. Choose 0-7.")

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(description="One Raspberry Pi-side command for UADY FPGA Lab.")
        p.add_argument("--setup", action="store_true", help="Setup/update private Pi Quartus/JTAG settings")
        p.add_argument("--check", action="store_true", help="Check private Pi setup")
        p.add_argument("--test-jtag", action="store_true", help="Test JTAG through the Quartus server")
        p.add_argument("--keys", action="store_true", help="Print Pi API key and GUI Terminal key")
        p.add_argument("--start", action="store_true", help="Start the Pi controller with dependency setup and watchdog")
        p.add_argument("--full", action="store_true", help="Run setup, check, JTAG test, print keys, then optionally start")
        p.add_argument("--save", action="store_true", help="Save Pi Quartus/JTAG setup from command-line arguments without prompting")
        p.add_argument("--host", default="", help="Quartus server IP/host")
        p.add_argument("--user", default="", help="Quartus server SSH username")
        p.add_argument("--quartus-standard", default="", help="Standard quartus_pgm path")
        p.add_argument("--quartus-pro", default="", help="Pro/Agilex quartus_pgm path")
        p.add_argument("--standard-project-path", default="", help="Standard projects folder on Quartus server")
        p.add_argument("--pro-project-path", default="", help="Pro/Agilex projects folder on Quartus server")
        p.add_argument("--standard-log-file", default="", help="Standard log file on Quartus server")
        p.add_argument("--pro-log-file", default="", help="Pro/Agilex log file on Quartus server")
        p.add_argument("--ssh-key-path", default="", help="Quartus SSH key path on this Raspberry Pi")
        p.add_argument("--history-base-dir", default="", help="Optional remote history folder")
        p.add_argument("--ssh-timeout-seconds", default=str(TIMEOUT_DEFAULTS["ssh_timeout_seconds"]))
        p.add_argument("--program-timeout-seconds", default=str(TIMEOUT_DEFAULTS["program_timeout_seconds"]))
        p.add_argument("--sof-copy-timeout-seconds", default=str(TIMEOUT_DEFAULTS["sof_copy_timeout_seconds"]))
        p.add_argument("--standard-program-timeout-seconds", default=str(TIMEOUT_DEFAULTS["standard_program_timeout_seconds"]))
        p.add_argument("--pro-program-timeout-seconds", default=str(TIMEOUT_DEFAULTS["pro_program_timeout_seconds"]))
        p.add_argument("--standard-sof-copy-timeout-seconds", default=str(TIMEOUT_DEFAULTS["standard_sof_copy_timeout_seconds"]))
        p.add_argument("--pro-sof-copy-timeout-seconds", default=str(TIMEOUT_DEFAULTS["pro_sof_copy_timeout_seconds"]))
        p.add_argument("--sof-copy-attempts", default=str(TIMEOUT_DEFAULTS["sof_copy_attempts"]))
        return p

    def save_from_args(self, args: argparse.Namespace) -> int:
        values: Dict[str, Any] = {
            "host": args.host,
            "user": args.user,
            "quartus_standard": args.quartus_standard,
            "quartus_pro": args.quartus_pro,
            "standard_project_path": args.standard_project_path,
            "pro_project_path": args.pro_project_path,
            "standard_log_file": args.standard_log_file,
            "pro_log_file": args.pro_log_file,
            "ssh_key_path": args.ssh_key_path,
            "history_base_dir": args.history_base_dir,
            "ssh_timeout_seconds": args.ssh_timeout_seconds,
            "program_timeout_seconds": args.program_timeout_seconds,
            "sof_copy_timeout_seconds": args.sof_copy_timeout_seconds,
            "standard_program_timeout_seconds": args.standard_program_timeout_seconds,
            "pro_program_timeout_seconds": args.pro_program_timeout_seconds,
            "standard_sof_copy_timeout_seconds": args.standard_sof_copy_timeout_seconds,
            "pro_sof_copy_timeout_seconds": args.pro_sof_copy_timeout_seconds,
            "sof_copy_attempts": args.sof_copy_attempts,
        }
        try:
            self.config.save_private_jtag_values(values)
        except Exception as exc:
            print(f"[FAIL] Could not save setup: {exc}")
            return 2
        print("[OK] Private Pi Quartus/JTAG setup saved from command-line arguments.")
        print(f"Private config file: {pi_private_config_path()}")
        print(f"Secret file:         {pi_secret_path()}")
        return 0

    def run(self, argv: list[str] | None = None) -> int:
        args = self.build_parser().parse_args(argv)

        # Only action flags should decide whether to skip the menu.
        # Some non-action options have default timeout values, so checking
        # any(vars(args).values()) makes `python3 UADY_PI.py` exit after
        # only printing the banner.
        action_requested = any([
            args.setup,
            args.check,
            args.test_jtag,
            args.keys,
            args.start,
            args.full,
            args.save,
        ])
        if not action_requested:
            return self.interactive_menu()

        self.banner()
        if args.save:
            return self.save_from_args(args)
        if args.full:
            self.setup_private_jtag()
            ok = self.config.print_setup_status()
            self.jtag.test_jtag()
            self.config.print_keys()
            if ok and self.yes_no("Start the controller now", default=True):
                self.runtime.start_controller()
            return 0
        if args.setup:
            self.setup_private_jtag()
        if args.check:
            if not self.config.print_setup_status():
                return 1
        if args.test_jtag:
            if not self.jtag.test_jtag():
                return 1
        if args.keys:
            self.config.print_keys()
        if args.start:
            self.runtime.start_controller()
        return 0


def main(argv: list[str] | None = None) -> int:
    return PiManagerCLI().run(argv)
