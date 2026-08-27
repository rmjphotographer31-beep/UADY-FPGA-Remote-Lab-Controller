# -*- coding: utf-8 -*-
"""Small dependency-free secure storage helpers for the UADY FPGA GUI/Pi.

Nothing sensitive should be hardcoded in gui.py, config.ini, or config_pi_hat.json.
These helpers put runtime secrets in OS/user storage and write files with private
permissions where the operating system supports it.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "uady_fpga_lab"
APP_NAME_WIN = "UADY_FPGA_Lab"


def _safe_home() -> Path:
    try:
        return Path.home()
    except Exception:
        return Path(os.getcwd()).resolve()


def user_data_dir() -> Path:
    """Return a per-user app storage folder that is shared by every extracted GUI."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(_safe_home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME_WIN
    if sys.platform == "darwin":
        return _safe_home() / "Library" / "Application Support" / APP_NAME_WIN
    base = os.environ.get("XDG_DATA_HOME") or str(_safe_home() / ".local" / "share")
    return Path(base) / APP_NAME


def ensure_private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass
    return path


def secure_chmod_file(path: Path) -> None:
    """Best-effort chmod 600. Windows ignores most POSIX mode bits."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else (default or {})
    except Exception:
        return default or {}


def write_json_secure(path: Path, data: Dict[str, Any]) -> None:
    path = Path(path)
    ensure_private_dir(path.parent)
    payload = json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    secure_chmod_file(tmp)
    os.replace(tmp, path)
    secure_chmod_file(path)


def user_secret_path(filename: str = "secrets.json") -> Path:
    return ensure_private_dir(user_data_dir()) / filename


def queue_token_path() -> Path:
    return user_secret_path("queue_tokens.json")



def user_config_path(filename: str = "gui_settings.ini") -> Path:
    """Return private per-user GUI/config storage shared by all GUI copies.

    Runtime settings such as the user's Raspberry Pi NetBird IP are stored here,
    not beside gui.py and not inside the shipped project folder.
    """
    return ensure_private_dir(user_data_dir()) / filename


def pi_private_config_path(filename: str = "pi_private_config.json") -> Path:
    """Return private Raspberry Pi controller config storage.

    This is for deployment-specific server host/user/path settings that should
    not be exposed in the shipped config_pi_hat.json.
    """
    return pi_secret_dir() / filename


def get_pi_config_section(section: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = read_json(pi_private_config_path(), {})
    value = data.get(str(section), default or {})
    return value if isinstance(value, dict) else (default or {})


def set_pi_config_section(section: str, value: Dict[str, Any]) -> None:
    data = read_json(pi_private_config_path(), {})
    data[str(section)] = dict(value or {})
    write_json_secure(pi_private_config_path(), data)


def legacy_migrate_json(legacy_path: Path, new_path: Path, delete_legacy: bool = True) -> None:
    """Move a legacy JSON secret/cache into the shared user storage path."""
    try:
        legacy_path = Path(legacy_path)
        new_path = Path(new_path)
        if new_path.exists() or not legacy_path.exists():
            return
        data = read_json(legacy_path, {})
        if data:
            write_json_secure(new_path, data)
        if delete_legacy:
            try:
                legacy_path.unlink()
            except Exception:
                pass
    except Exception:
        pass


def get_user_secret(name: str, default: str = "") -> str:
    data = read_json(user_secret_path(), {})
    value = data.get(name, default)
    return str(value or "")


def set_user_secret(name: str, value: str) -> None:
    data = read_json(user_secret_path(), {})
    data[str(name)] = str(value or "")
    write_json_secure(user_secret_path(), data)


def get_or_create_user_secret(name: str, nbytes: int = 32) -> str:
    data = read_json(user_secret_path(), {})
    value = str(data.get(name) or "").strip()
    if not value:
        value = secrets.token_urlsafe(max(16, int(nbytes)))
        data[str(name)] = value
        write_json_secure(user_secret_path(), data)
    return value


def pi_secret_dir() -> Path:
    """Return Raspberry Pi secret directory.

    Preference order:
    1. UADY_PI_SECRET_DIR, when set.
    2. /var/lib/uady_fpga_lab, for a real Raspberry Pi/service install.
    3. Per-user storage fallback, so development runs still work without sudo.
    """
    env_dir = os.environ.get("UADY_PI_SECRET_DIR")
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir))
    if os.name != "nt":
        candidates.append(Path("/var/lib") / APP_NAME)
    candidates.append(user_data_dir() / "pi")
    for candidate in candidates:
        try:
            ensure_private_dir(candidate)
            probe = candidate / ".write_test"
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            secure_chmod_file(probe)
            try:
                probe.unlink()
            except Exception:
                pass
            return candidate
        except Exception:
            continue
    return ensure_private_dir(user_data_dir() / "pi")


def pi_secret_path(filename: str = "pi_secrets.json") -> Path:
    return pi_secret_dir() / filename


def get_pi_secret(name: str, default: str = "") -> str:
    data = read_json(pi_secret_path(), {})
    value = data.get(name, default)
    return str(value or "")


def set_pi_secret(name: str, value: str) -> None:
    data = read_json(pi_secret_path(), {})
    data[str(name)] = str(value or "")
    write_json_secure(pi_secret_path(), data)


def get_or_create_pi_secret(name: str, nbytes: int = 32) -> str:
    data = read_json(pi_secret_path(), {})
    value = str(data.get(name) or "").strip()
    if not value:
        value = secrets.token_urlsafe(max(16, int(nbytes)))
        data[str(name)] = value
        write_json_secure(pi_secret_path(), data)
    return value


def get_or_create_pi_api_key() -> str:
    env_key = str(os.environ.get("UADY_PI_API_KEY") or "").strip()
    if env_key:
        return env_key
    return get_or_create_pi_secret("pi_api_key", nbytes=36)


def set_pi_api_key(value: str) -> None:
    value = str(value or "").strip()
    if value:
        set_pi_secret("pi_api_key", value)

def get_or_create_pi_terminal_key() -> str:
    """Return the Raspberry-side GUI terminal access key.

    The GUI terminal key is shared by every GUI because it is generated and
    stored on the Raspberry Pi, not in gui.py, config.ini, or any bundled file.
    Set UADY_TERMINAL_KEY only for emergency/service-managed deployments.
    """
    env_key = str(os.environ.get("UADY_TERMINAL_KEY") or "").strip()
    if env_key:
        return env_key
    return get_or_create_pi_secret("terminal_access_key", nbytes=18)


def set_pi_terminal_key(value: str) -> None:
    """Set the Raspberry-side GUI terminal access key from a trusted Pi shell."""
    value = str(value or "").strip()
    if value:
        set_pi_secret("terminal_access_key", value)



def get_quartus_ssh_key_path(default: str = "") -> str:
    """Return the Raspberry-side Quartus SSH key path from env or private storage.

    This stores only the private key *path* on the Raspberry Pi, not the key
    content, and keeps it out of config_pi_hat.json.
    """
    env_key = str(os.environ.get("UADY_QUARTUS_SSH_KEY_PATH") or "").strip()
    if env_key:
        return env_key
    return get_pi_secret("quartus_server_ssh_key_path", default)


def set_quartus_ssh_key_path(value: str) -> None:
    value = str(value or "").strip()
    if value:
        set_pi_secret("quartus_server_ssh_key_path", value)
