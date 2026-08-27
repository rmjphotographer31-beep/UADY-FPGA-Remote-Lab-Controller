# -*- coding: utf-8 -*-
"""Class-based Raspberry Pi API client for future GUI cleanup."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from uady_gui_utils import ApiSession
from .models import PiConnectionSettings


@dataclass
class PiApiClient:
    """Small wrapper around the existing ApiSession utility.

    gui.py can gradually move Pi calls into this object instead of keeping many
    global api_get/api_post helper functions.
    """

    requests_module: Any
    settings_getter: Callable[[], PiConnectionSettings]
    headers_getter: Callable[[], Mapping[str, str]]
    validator: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self._session = ApiSession(
            self.requests_module,
            lambda: self.settings_getter().base_url(),
            self.headers_getter,
            validator=self.validator,
        )

    def close(self) -> None:
        self._session.close()

    def get(self, path: str, timeout: int = 15):
        return self._session.get_json(path, timeout=timeout)

    def post(self, path: str, payload: Mapping[str, Any], timeout: int = 20):
        return self._session.post_json(path, payload, timeout=timeout)

    def post_files(self, path: str, fields: Mapping[str, Any], files: Mapping[str, str], timeout: int = 900):
        return self._session.post_files(path, fields, files, timeout=timeout)
