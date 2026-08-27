#!/usr/bin/env python3
"""Dynamic Quartus toolchain and JTAG discovery for UADY v5.0.

Precedence:
1. Environment variables
2. Protected Pi configuration
3. PATH
4. Filesystem discovery

This module does not select the target board for a job. It only discovers
available programming tools and physical JTAG cables.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Toolchain:
    toolchain_id: str
    executable: str
    source: str


@dataclass(frozen=True)
class Cable:
    cable: str
    toolchain_id: str
    executable: str


def _candidate_roots() -> list[Path]:
    roots = []
    for raw in (
        os.environ.get("UADY_QUARTUS_SEARCH_ROOTS", ""),
        "/opt",
        "/home",
    ):
        for item in str(raw).split(os.pathsep):
            item = item.strip()
            if item:
                roots.append(Path(item).expanduser())
    return roots


def discover_toolchains() -> list[Toolchain]:
    found: dict[str, Toolchain] = {}

    env_map = {
        "standard": "UADY_QUARTUS_STANDARD_PGM",
        "pro": "UADY_QUARTUS_PRO_PGM",
    }
    for toolchain_id, env_name in env_map.items():
        raw = os.environ.get(env_name, "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                raise RuntimeError(f"{env_name} points to missing file: {path}")
            found[str(path)] = Toolchain(toolchain_id, str(path), f"env:{env_name}")

    from_path = shutil.which("quartus_pgm")
    if from_path:
        path = str(Path(from_path).resolve())
        found.setdefault(path, Toolchain("path", path, "PATH"))

    patterns = (
        "**/quartus/bin/quartus_pgm",
        "**/quartus/bin/quartus_pgm.exe",
    )
    for root in _candidate_roots():
        if not root.exists():
            continue
        for pattern in patterns:
            for candidate in root.glob(pattern):
                if candidate.is_file():
                    resolved = str(candidate.resolve())
                    name = resolved.lower()
                    kind = "pro" if "pro" in name else "standard"
                    found.setdefault(resolved, Toolchain(kind, resolved, "filesystem"))

    return sorted(found.values(), key=lambda x: (x.toolchain_id, x.executable))


def list_cables(toolchain: Toolchain, timeout: float = 20.0) -> list[Cable]:
    completed = subprocess.run(
        [toolchain.executable, "-l"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{toolchain.executable} -l failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )

    cables: list[Cable] = []
    for line in completed.stdout.splitlines():
        match = re.match(r"\s*\d+\)\s+(.+?)\s*$", line)
        if match:
            cables.append(Cable(
                cable=match.group(1).strip(),
                toolchain_id=toolchain.toolchain_id,
                executable=toolchain.executable,
            ))
    return cables


def scan_all() -> dict[str, Any]:
    toolchains = discover_toolchains()
    cables: dict[str, Cable] = {}
    errors: list[str] = []

    for toolchain in toolchains:
        try:
            for cable in list_cables(toolchain):
                cables.setdefault(cable.cable, cable)
        except Exception as exc:
            errors.append(str(exc))

    return {
        "success": bool(toolchains),
        "toolchains": [asdict(x) for x in toolchains],
        "cables": [asdict(x) for x in cables.values()],
        "errors": errors,
    }


if __name__ == "__main__":
    print(json.dumps(scan_all(), indent=2))
