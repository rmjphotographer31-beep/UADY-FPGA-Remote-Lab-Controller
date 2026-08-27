# -*- coding: utf-8 -*-
"""Private Classic Mode configuration loading.

The GUI package should not ship lab IPs, usernames, paths, keys, or tokens.
Classic profiles are loaded from protected user storage or from an explicitly
provided private file through UADY_CLASSIC_CONFIG.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from uady_secure_store import user_config_path
from .models import ClassicServerProfile


SETUP_MESSAGE = (
    "No classic server profile is configured. Run UADY_SETUP.py on this computer "
    "and choose Classic/Full setup, or set UADY_CLASSIC_CONFIG to a private classic_servers.ini file."
)


@dataclass
class ClassicConfigStore:
    """Reads Classic Mode profile config with caching."""

    base_dir: Path
    env_name: str = "UADY_CLASSIC_CONFIG"

    def __post_init__(self) -> None:
        self.legacy_config_path = self.base_dir / "config.ini"
        self.user_config_path = Path(user_config_path("classic_servers.ini"))
        self._cache: Optional[configparser.ConfigParser] = None
        self._cache_mtime: Optional[float] = None
        self._cache_path: Optional[Path] = None

    def active_path(self) -> Path:
        env_path = os.environ.get(self.env_name, "").strip()
        if env_path:
            return Path(env_path)
        if self.user_config_path.exists():
            return self.user_config_path
        if self.legacy_config_path.exists():
            return self.legacy_config_path
        return self.user_config_path

    def load_parser(self) -> Optional[configparser.ConfigParser]:
        path = self.active_path()
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = None
        if self._cache is not None and self._cache_mtime == mtime and self._cache_path == path:
            return self._cache
        parser = configparser.ConfigParser()
        if not path.exists() or not parser.read(path, encoding="utf-8"):
            print("[FAIL] " + SETUP_MESSAGE)
            return None
        self._cache = parser
        self._cache_mtime = mtime
        self._cache_path = path
        return parser

    def profile_for_key(self, key_path: str) -> Optional[ClassicServerProfile]:
        parser = self.load_parser()
        if parser is None:
            return None

        profile_name = ""
        key_name = os.path.basename(key_path or "")
        if parser.has_section("servers"):
            if key_name and parser.has_option("servers", key_name):
                profile_name = parser.get("servers", key_name)
            elif parser.has_option("servers", "default_profile"):
                profile_name = parser.get("servers", "default_profile")
            elif parser.has_option("servers", "default"):
                profile_name = parser.get("servers", "default")

        if not profile_name:
            reserved = {"servers", "raspberry_pi", "security", "ui"}
            profiles = [section for section in parser.sections() if section not in reserved]
            if len(profiles) == 1:
                profile_name = profiles[0]

        if profile_name and parser.has_section(profile_name):
            return ClassicServerProfile.from_config_section(profile_name, parser[profile_name])

        print("[FAIL] " + SETUP_MESSAGE)
        return None
