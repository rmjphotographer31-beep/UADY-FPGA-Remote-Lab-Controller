#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Setup helpers used by :mod:`UADY_SETUP`.

This module intentionally stores deployment-specific values outside the source
checkout.  It contains no lab addresses, usernames, API keys, terminal keys, or
private-key material.
"""
from __future__ import annotations

import configparser
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from uady_secure_store import (
    get_pi_config_section,
    set_pi_config_section,
    set_quartus_ssh_key_path,
    user_config_path,
)


def _value(args: Any, name: str, default: str = "") -> str:
    return str(getattr(args, name, default) or default).strip()


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def save_classic_profile(args: Any) -> Path:
    """Save a Classic Mode server profile in private per-user storage."""
    profile = _value(args, "profile", "CLASSIC_SERVER") or "CLASSIC_SERVER"
    path = Path(user_config_path("classic_servers.ini"))
    cfg = configparser.ConfigParser()
    if path.exists():
        try:
            cfg.read(path, encoding="utf-8")
        except Exception:
            cfg = configparser.ConfigParser()

    if not cfg.has_section("servers"):
        cfg.add_section("servers")
    cfg.set("servers", "default_profile", profile)

    if not cfg.has_section(profile):
        cfg.add_section(profile)
    mapping = {
        "ip_local": _value(args, "classic_ip_local"),
        "ip_netbird": _value(args, "classic_ip_netbird"),
        "user": _value(args, "quartus_server_user"),
        "quartus_path": _value(args, "quartus_standard"),
        "base_project_path": _value(args, "standard_project_path"),
        "log_file_path": _value(args, "standard_log_file"),
        "quartus_primepro_path": _value(args, "quartus_pro"),
        "primepro_project_path": _value(args, "pro_project_path"),
        "primepro_log_file_path": _value(args, "pro_log_file"),
    }
    for key, value in mapping.items():
        cfg.set(profile, key, value)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        cfg.write(handle)
    _secure_file(path)
    print(f"[OK] Classic Mode profile saved to private user storage: {path}")
    return path


def save_pi_setup_local(args: Any) -> None:
    """Save Raspberry Pi Quartus/JTAG settings to protected Pi storage."""
    values = {
        "host": _value(args, "quartus_server_host"),
        "user": _value(args, "quartus_server_user"),
        "quartus_standard": _value(args, "quartus_standard"),
        "quartus_pro": _value(args, "quartus_pro"),
        "standard_project_path": _value(args, "standard_project_path"),
        "pro_project_path": _value(args, "pro_project_path"),
        "standard_log_file": _value(args, "standard_log_file"),
        "pro_log_file": _value(args, "pro_log_file"),
    }
    required = ["host", "user", "standard_project_path", "pro_project_path", "standard_log_file", "pro_log_file"]
    missing = [name for name in required if not values.get(name)]
    if not (values.get("quartus_standard") or values.get("quartus_pro")):
        missing.append("quartus_standard_or_quartus_pro")
    if missing:
        raise ValueError("Missing required Raspberry Pi setup fields: " + ", ".join(missing))

    for key in (
        "ssh_timeout_seconds",
        "program_timeout_seconds",
        "sof_copy_timeout_seconds",
        "standard_program_timeout_seconds",
        "pro_program_timeout_seconds",
        "standard_sof_copy_timeout_seconds",
        "pro_sof_copy_timeout_seconds",
        "sof_copy_attempts",
    ):
        raw = _value(args, key)
        if raw:
            try:
                values[key] = int(raw)
            except ValueError:
                raise ValueError(f"{key} must be an integer") from None

    set_pi_config_section("quartus_server", values)

    history = get_pi_config_section("server_history", {})
    history_dir = _value(args, "history_base_dir")
    if history_dir:
        history["base_dir"] = history_dir
    history["enabled"] = True
    history["record_format"] = str(history.get("record_format", "txt") or "txt")
    history["one_record_per_job"] = True
    history["record_on_queue_accept"] = True
    set_pi_config_section("server_history", history)

    key_path = _value(args, "pi_quartus_ssh_key_path")
    if key_path:
        set_quartus_ssh_key_path(key_path)
        key = Path(key_path).expanduser()
        if key.exists():
            _secure_file(key)

    print("[OK] Raspberry Pi Quartus/JTAG settings saved to protected Pi storage.")


def save_pi_setup_remote(args: Any) -> None:
    """Configure the Raspberry Pi remotely through its supported ``UADY_PI.py --save`` CLI."""
    pi_host = _value(args, "remote_pi_host")
    pi_user = _value(args, "remote_pi_user")
    controller_dir = _value(args, "remote_pi_controller_path")
    if not pi_host or not pi_user or not controller_dir:
        raise ValueError("remote_pi_host, remote_pi_user, and remote_pi_controller_path are required")

    remote_args = [
        "python3", "UADY_PI.py", "--save",
        "--host", _value(args, "quartus_server_host"),
        "--user", _value(args, "quartus_server_user"),
        "--quartus-standard", _value(args, "quartus_standard"),
        "--quartus-pro", _value(args, "quartus_pro"),
        "--standard-project-path", _value(args, "standard_project_path"),
        "--pro-project-path", _value(args, "pro_project_path"),
        "--standard-log-file", _value(args, "standard_log_file"),
        "--pro-log-file", _value(args, "pro_log_file"),
        "--ssh-key-path", _value(args, "pi_quartus_ssh_key_path"),
        "--history-base-dir", _value(args, "history_base_dir"),
    ]
    for name, flag in (
        ("ssh_timeout_seconds", "--ssh-timeout-seconds"),
        ("program_timeout_seconds", "--program-timeout-seconds"),
        ("sof_copy_timeout_seconds", "--sof-copy-timeout-seconds"),
        ("standard_program_timeout_seconds", "--standard-program-timeout-seconds"),
        ("pro_program_timeout_seconds", "--pro-program-timeout-seconds"),
        ("standard_sof_copy_timeout_seconds", "--standard-sof-copy-timeout-seconds"),
        ("pro_sof_copy_timeout_seconds", "--pro-sof-copy-timeout-seconds"),
        ("sof_copy_attempts", "--sof-copy-attempts"),
    ):
        remote_args.extend([flag, _value(args, name)])

    remote_cmd = "cd " + shlex.quote(controller_dir) + " && " + " ".join(shlex.quote(x) for x in remote_args)
    print(f"[RUN] ssh {pi_user}@{pi_host} <remote UADY_PI.py --save>")
    result = subprocess.run(["ssh", f"{pi_user}@{pi_host}", remote_cmd])
    if result.returncode != 0:
        raise RuntimeError(f"Remote Raspberry Pi setup failed with exit code {result.returncode}")
    print("[OK] Raspberry Pi remote Quartus/JTAG setup saved.")
