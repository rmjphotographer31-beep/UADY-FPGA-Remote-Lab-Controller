# -*- coding: utf-8 -*-
"""GUI background worker utilities."""
from __future__ import annotations

import atexit
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class GuiBackgroundExecutor:
    """Bounded background executor for Tkinter apps."""

    def __init__(self, max_workers: int = 6, thread_name_prefix: str = "gui_bg") -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        atexit.register(self.shutdown)

    def submit(self, fn: Callable[[], T]) -> Optional[Future[T]]:
        try:
            return self._executor.submit(fn)
        except RuntimeError:
            return None

    def shutdown(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python <3.9 compatibility.
            self._executor.shutdown(wait=False)
