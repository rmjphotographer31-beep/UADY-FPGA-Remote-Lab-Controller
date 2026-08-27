# -*- coding: utf-8 -*-
"""Typed data models for the non-Raspberry GUI side."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class ClassicServerProfile:
    """Classic Mode Quartus server settings.

    This object contains *non-secret* connection/runtime details. The OpenSSH
    private key itself is not stored here; users select it at runtime or the
    Raspberry Pi stores its own key path in protected Pi storage.
    """

    name: str
    ip_local: str
    ip_netbird: str
    user: str
    quartus_path: str
    base_project_path: str
    log_file_path: str
    quartus_primepro_path: str = ""
    primepro_project_path: str = ""
    primepro_log_file_path: str = ""

    @classmethod
    def from_config_section(cls, name: str, section: Mapping[str, str]) -> "ClassicServerProfile":
        return cls(
            name=name,
            ip_local=str(section.get("ip_local", "")).strip(),
            ip_netbird=str(section.get("ip_netbird", "")).strip(),
            user=str(section.get("user", "")).strip(),
            quartus_path=str(section.get("quartus_path", "")).strip(),
            base_project_path=str(section.get("base_project_path", "")).strip(),
            log_file_path=str(section.get("log_file_path", "")).strip(),
            quartus_primepro_path=str(section.get("quartus_primepro_path", "")).strip(),
            primepro_project_path=str(section.get("primepro_project_path", "")).strip(),
            primepro_log_file_path=str(section.get("primepro_log_file_path", "")).strip(),
        )

    def host(self, use_netbird: bool = False) -> str:
        return self.ip_netbird if use_netbird else self.ip_local

    def quartus_for_board(self, board_or_cable: str) -> str:
        is_agilex = "agilex" in (board_or_cable or "").lower()
        if is_agilex and self.quartus_primepro_path:
            pro = self.quartus_primepro_path
            return pro if pro.endswith("quartus_pgm") else pro.rstrip("/") + "/quartus_pgm"
        return self.quartus_path

    def base_dir_for_board(self, board_or_cable: str) -> str:
        is_agilex = "agilex" in (board_or_cable or "").lower()
        if is_agilex and self.primepro_project_path:
            return self.primepro_project_path
        return self.base_project_path

    def log_path_for_board(self, board_or_cable: str) -> str:
        is_agilex = "agilex" in (board_or_cable or "").lower()
        if is_agilex and self.primepro_log_file_path:
            return self.primepro_log_file_path
        return self.log_file_path


@dataclass(frozen=True)
class LocalSofProject:
    """Resolved local Quartus project paths used by Classic Mode uploads."""

    qpf_path: Path
    project_dir: Path
    sof_path: Path

    @property
    def project_name(self) -> str:
        return self.qpf_path.stem


@dataclass(frozen=True)
class PiConnectionSettings:
    """Non-secret Raspberry Pi GUI connection settings."""

    scheme: str = "http"
    netbird_ip: str = ""
    port: int = 5050
    use_netbird: bool = True

    def base_url(self) -> str:
        host = self.netbird_ip.strip()
        return f"{self.scheme}://{host}:{int(self.port)}"
