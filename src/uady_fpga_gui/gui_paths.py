# -*- coding: utf-8 -*-
"""Centralized GUI runtime paths and non-project storage."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from uady_secure_store import queue_token_path, user_config_path


@dataclass(frozen=True)
class GuiRuntimePaths:
    """Paths used by GUI files that must not be shipped in the project folder."""

    base_dir: Path

    @property
    def gui_settings(self) -> Path:
        return Path(user_config_path("gui_settings.ini"))

    @property
    def classic_servers(self) -> Path:
        return Path(user_config_path("classic_servers.ini"))

    @property
    def queue_tokens(self) -> Path:
        return Path(queue_token_path())

    @property
    def legacy_queue_tokens(self) -> Path:
        return self.base_dir / "my_queue_tokens.json"

    @property
    def legacy_config(self) -> Path:
        return self.base_dir / "config.ini"
