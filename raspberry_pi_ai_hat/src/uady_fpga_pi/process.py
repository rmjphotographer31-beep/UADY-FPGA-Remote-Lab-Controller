"""Small subprocess helpers used by the Pi manager."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Tuple

from .paths import PI_ROOT


def format_cmd(cmd: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


def run_command(cmd: Iterable[str], *, cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> int:
    print(f"[RUN] {format_cmd(cmd)}")
    try:
        p = subprocess.run(list(map(str, cmd)), cwd=str(cwd or PI_ROOT), timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[FAIL] Command timed out after {timeout} seconds.")
        if check:
            raise SystemExit(124)
        return 124
    if check and p.returncode != 0:
        raise SystemExit(int(p.returncode))
    return int(p.returncode)


def capture_command(cmd: Iterable[str], *, cwd: Optional[Path] = None, timeout: int = 90) -> Tuple[int, str]:
    print(f"[RUN] {format_cmd(cmd)}")
    try:
        p = subprocess.run(
            list(map(str, cmd)),
            cwd=str(cwd or PI_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        out = p.stdout or ""
        if out:
            print(out.rstrip())
        return int(p.returncode), out
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="ignore")
        if out:
            print(out.rstrip())
        print(f"[FAIL] Command timed out after {timeout} seconds.")
        return 124, out
