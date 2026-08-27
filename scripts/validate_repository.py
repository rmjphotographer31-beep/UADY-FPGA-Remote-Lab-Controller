#!/usr/bin/env python3
"""Fast packaging/release checks that do not require live FPGA hardware."""
from __future__ import annotations

import json
import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PI = ROOT / "raspberry_pi_ai_hat"

REQUIRED = [
    ROOT / "README.md",
    ROOT / "RUN_GUI.py",
    ROOT / "gui.py",
    ROOT / "UADY_SETUP.py",
    ROOT / "SETUP_CLASSIC_AND_PI.py",
    ROOT / "requirements_gui.txt",
    PI / "UADY_PI.py",
    PI / "pi_ai_hat_controller.py",
    PI / "config_pi_hat.json",
    PI / "board_profiles.json",
    PI / "fpga_signal_extractor.c",
    PI / "fpga_classifier_policy.py",
    PI / "ollama_fpga_classifier_prompt.txt",
    PI / "production_policy.json",
]

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(r"\b(?:netbird\s+up\s+--setup-key|setup-key)\s+[A-Za-z0-9-]{16,}", re.I),
]

# Known private deployment IP from an earlier lab note must never reappear.
FORBIDDEN_LITERALS = ["100.66." + "52.42"]


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(2)


def main() -> int:
    for path in REQUIRED:
        if not path.exists():
            fail(f"missing required file: {path.relative_to(ROOT)}")
    print("[OK] required repository files")

    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "venv", ".git"} for part in path.parts):
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            fail(f"Python compile failed for {path.relative_to(ROOT)}: {exc}")
    print("[OK] Python syntax")

    for path in list(ROOT.rglob("*.json")):
        if any(part in {".venv", "venv", ".git"} for part in path.parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"JSON parse failed for {path.relative_to(ROOT)}: {exc}")
    print("[OK] JSON syntax")

    config = json.loads((PI / "config_pi_hat.json").read_text())
    if int(config.get("fair_share", {}).get("max_active_jobs_per_student", 0)) != 1:
        fail("expected default one-active-job-per-student policy")
    if not bool(config.get("server_history", {}).get("one_record_per_job", False)):
        fail("expected one_record_per_job=true")
    if not bool(config.get("jtag_prewarm_daemon", {}).get("enabled", False)):
        fail("expected JTAG prewarm daemon enabled")
    if not bool(config.get("ephemeral_job_storage", {}).get("enabled", False)):
        fail("expected ephemeral job storage enabled")
    print("[OK] production policy invariants")

    profiles = json.loads((PI / "board_profiles.json").read_text())
    text = json.dumps(profiles)
    for device in ("5CSEMA5F31C6", "5CSEMA5F31C6N", "AGFB014R24B2E2V", "AGFB014R24B1E1V"):
        if device not in text:
            fail(f"missing authoritative device identity: {device}")
    print("[OK] authoritative FPGA identities")

    extensions = {".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".sh", ".bat", ".c", ".ini", ".example"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        if path.suffix.lower() not in extensions and path.name not in {".gitignore"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for literal in FORBIDDEN_LITERALS:
            if literal in content:
                fail(f"forbidden deployment literal found in {path.relative_to(ROOT)}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                fail(f"possible committed secret in {path.relative_to(ROOT)}")
    print("[OK] basic secret/deployment literal scan")

    helper = subprocess.run([sys.executable, str(ROOT / "UADY_SETUP.py"), "--help"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if helper.returncode != 0:
        fail("UADY_SETUP.py --help failed: " + helper.stderr.strip())
    print("[OK] root setup wizard packaging")

    policy = subprocess.run([sys.executable, str(PI / "TEST_FPGA_CLASSIFIER_POLICY.py")], cwd=PI, capture_output=True, text=True)
    if policy.returncode != 0:
        fail("classifier policy regression failed:\n" + policy.stdout + policy.stderr)
    print("[OK] classifier policy regression")

    print("[PASS] GitHub repository validation completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
