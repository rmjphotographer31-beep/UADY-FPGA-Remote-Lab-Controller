#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

for name in (
    "pi_ai_hat_controller.py",
    "fpga_classifier_policy.py",
    "benchmark_classifier.py",
    "dynamic_toolchain_discovery.py",
    "job_store_sqlite.py",
    "TEST_FPGA_CLASSIFIER_POLICY.py",
):
    py_compile.compile(str(BASE / name), doraise=True)
    print(f"[OK] Python syntax: {name}")

for name in (
    "config_pi_hat.json",
    "board_profiles.json",
    "board_profiles_manual_grounded.json",
    "manual_reference_evidence.json",
    "dataset_manifest.json",
    "production_policy.json",
):
    json.loads((BASE / name).read_text(encoding="utf-8"))
    print(f"[OK] JSON: {name}")

prompt = (BASE / "ollama_fpga_classifier_prompt.txt").read_text(encoding="utf-8")
required_prompt_markers = (
    "CURRENT_INPUT_JSON",
    "AGFB014R24B2E2V",
    "5CSEMA5F31C6",
    "observed_qsf_device",
    "cited_signals",
)
for marker in required_prompt_markers:
    if marker not in prompt:
        raise RuntimeError(f"Grounded prompt is missing required marker: {marker}")
print("[OK] Manual-grounded Ollama prompt")

subprocess.run(
    [sys.executable, str(BASE / "TEST_FPGA_CLASSIFIER_POLICY.py")],
    cwd=BASE,
    check=True,
)
print("[OK] Authoritative identity and hallucination-guard regression tests")

subprocess.run(["bash", str(BASE / "BUILD_SIGNAL_EXTRACTOR.sh")], cwd=BASE, check=True)
print("[OK] C signal extractor")
print("[OK] UADY v5.4 manual-grounded AI validation completed")
