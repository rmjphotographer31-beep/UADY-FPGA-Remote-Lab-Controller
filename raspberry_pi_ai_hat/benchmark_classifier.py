#!/usr/bin/env python3
"""
UADY FPGA classifier benchmark.

Measures:
1) C extractor latency over many iterations.
2) Warm AI selector latency over many iterations.
3) End-to-end C -> AI latency.
4) Classification correctness and response validity.
5) Saves every sample to CSV plus a JSON summary.

Usage:
  python3 benchmark_classifier.py \
    --verilog /tmp/TEST_SWLED.v \
    --qsf /tmp/TEST_SWLED.qsf \
    --expected-board DE10-Agilex \
    --c-runs 10000 \
    --ai-runs 100 \
    --pipeline-runs 50
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fpga_classifier_policy import (
    build_prompt as build_grounded_prompt,
    enforce_grounding,
    ollama_json_schema,
)


ALLOWED_BOARDS = {"DE1-SoC", "DE10-Agilex", "Ambiguous"}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean_ms": statistics.mean(values),
        "median_ms": statistics.median(values),
        "stdev_ms": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min_ms": min(values),
        "p90_ms": percentile(values, 0.90),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }


def load_profile_catalog(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [
        {
            "display_name": p.get("display_name"),
            "ai_selection_guidance": p.get("ai_selection_guidance", {}),
        }
        for p in doc.get("profiles", [])
        if isinstance(p, dict) and p.get("enabled", True)
    ]


def run_extractor(extractor: Path, verilog: Path, qsf: Path) -> tuple[dict[str, Any], float]:
    started = time.perf_counter_ns()
    proc = subprocess.run(
        [str(extractor), str(verilog), str(qsf)],
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    evidence = json.loads(proc.stdout)
    return evidence, elapsed_ms


def build_prompt(evidence: dict[str, Any], profiles: list[dict[str, Any]]) -> str:
    """Build the same stateless, manual-grounded prompt used by production."""
    return build_grounded_prompt(evidence, profiles)


def call_ollama(
    url: str,
    model: str,
    prompt: str,
    timeout: int,
    keep_alive: str,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
            "format": ollama_json_schema(),
            "options": {
                "temperature": 0,
                "num_predict": 220,
                "num_ctx": 4096,
                "seed": 42,
            },
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if int(getattr(exc, "code", 0) or 0) != 400:
            raise
        # Compatibility fallback for Ollama versions that support JSON mode
        # but not a JSON-schema object in the `format` field.
        fallback_doc = json.loads(payload.decode("utf-8"))
        fallback_doc["format"] = "json"
        fallback_request = urllib.request.Request(
            url,
            data=json.dumps(fallback_doc).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(fallback_request, timeout=timeout) as response:
            body = json.load(response)
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000

    raw = body.get("response", "")
    parsed: dict[str, Any]
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {"_parse_error": True, "_raw": raw}

    return body, parsed, wall_ms


def validate_ai_result(result: dict[str, Any], expected_board: str) -> dict[str, bool]:
    target = str(result.get("target_board", "")).strip()
    schema_valid = (
        target in ALLOWED_BOARDS
        and isinstance(result.get("confidence_percent"), int)
        and isinstance(result.get("safe_to_program"), bool)
        and result.get("decision_type") in {"match", "ambiguous", "conflict"}
        and isinstance(result.get("reason"), str)
    )
    correct = target == expected_board
    safe_match = (
        correct
        and result.get("safe_to_program") is True
        and result.get("decision_type") == "match"
    )
    return {
        "schema_valid": schema_valid,
        "correct": correct,
        "safe_match": safe_match,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verilog", required=True, type=Path)
    parser.add_argument("--qsf", required=True, type=Path)
    parser.add_argument("--expected-board", required=True, choices=sorted(ALLOWED_BOARDS))
    parser.add_argument(
        "--extractor",
        type=Path,
        default=(Path(__file__).resolve().parent / "fpga_signal_extractor"),
    )
    parser.add_argument("--profiles", type=Path, default=(Path(__file__).resolve().parent / "board_profiles.json"))
    parser.add_argument("--model", default="qwen2.5-coder:1.5b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/generate")
    parser.add_argument("--c-warmup", type=int, default=200)
    parser.add_argument("--c-runs", type=int, default=10000)
    parser.add_argument("--ai-warmup", type=int, default=3)
    parser.add_argument("--ai-runs", type=int, default=100)
    parser.add_argument("--pipeline-runs", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--keep-alive", default="30m")
    parser.add_argument("--output-prefix", default="benchmark_results")
    args = parser.parse_args()

    for path in (args.verilog, args.qsf, args.extractor, args.profiles):
        if not path.exists():
            print(f"ERROR: missing file: {path}", file=sys.stderr)
            return 2

    profiles = load_profile_catalog(args.profiles)

    csv_path = Path(f"{args.output_prefix}.csv")
    json_path = Path(f"{args.output_prefix}_summary.json")

    rows: list[dict[str, Any]] = []
    c_samples: list[float] = []
    ai_samples: list[float] = []
    pipeline_samples: list[float] = []
    ai_prompt_eval: list[float] = []
    ai_generation: list[float] = []
    ai_load: list[float] = []
    correctness = {"schema_valid": 0, "raw_correct": 0, "correct": 0, "safe_match": 0, "failures": 0}

    print(f"C warmup: {args.c_warmup} runs")
    for _ in range(args.c_warmup):
        run_extractor(args.extractor, args.verilog, args.qsf)

    print(f"C benchmark: {args.c_runs} runs")
    evidence: dict[str, Any] | None = None
    for i in range(1, args.c_runs + 1):
        evidence, elapsed_ms = run_extractor(args.extractor, args.verilog, args.qsf)
        c_samples.append(elapsed_ms)
        rows.append({
            "stage": "c",
            "iteration": i,
            "wall_ms": elapsed_ms,
            "raw_ai_target": "",
            "target_board": "",
            "guard_applied": "",
            "reason_code": "",
            "schema_valid": "",
            "correct": "",
            "safe_match": "",
            "load_ms": "",
            "prompt_eval_ms": "",
            "generation_ms": "",
            "prompt_tokens": "",
            "output_tokens": "",
            "error": "",
        })
        if i % max(1, args.c_runs // 10) == 0:
            print(f"  C progress: {i}/{args.c_runs}")

    assert evidence is not None
    prompt = build_prompt(evidence, profiles)

    print(f"AI warmup: {args.ai_warmup} runs")
    for _ in range(args.ai_warmup):
        call_ollama(args.ollama_url, args.model, prompt, args.timeout, args.keep_alive)

    print(f"AI benchmark: {args.ai_runs} runs")
    for i in range(1, args.ai_runs + 1):
        try:
            body, parsed, elapsed_ms = call_ollama(
                args.ollama_url, args.model, prompt, args.timeout, args.keep_alive
            )
            raw_ai_target = str(parsed.get("target_board", "") or "").strip()
            guarded = enforce_grounding(parsed, evidence)
            checks = validate_ai_result(guarded, args.expected_board)
            correctness["raw_correct"] += int(raw_ai_target == args.expected_board)
            for key, value in checks.items():
                correctness[key] += int(value)

            load_ms = body.get("load_duration", 0) / 1_000_000
            prompt_eval_ms = body.get("prompt_eval_duration", 0) / 1_000_000
            generation_ms = body.get("eval_duration", 0) / 1_000_000

            ai_samples.append(elapsed_ms)
            ai_load.append(load_ms)
            ai_prompt_eval.append(prompt_eval_ms)
            ai_generation.append(generation_ms)

            rows.append({
                "stage": "ai",
                "iteration": i,
                "wall_ms": elapsed_ms,
                "raw_ai_target": raw_ai_target,
                "target_board": guarded.get("target_board", ""),
                "guard_applied": guarded.get("guard_applied", False),
                "reason_code": guarded.get("reason_code", ""),
                "schema_valid": checks["schema_valid"],
                "correct": checks["correct"],
                "safe_match": checks["safe_match"],
                "load_ms": load_ms,
                "prompt_eval_ms": prompt_eval_ms,
                "generation_ms": generation_ms,
                "prompt_tokens": body.get("prompt_eval_count", ""),
                "output_tokens": body.get("eval_count", ""),
                "error": "",
            })
        except Exception as exc:
            correctness["failures"] += 1
            rows.append({
                "stage": "ai",
                "iteration": i,
                "wall_ms": "",
                "raw_ai_target": "",
                "target_board": "",
                "guard_applied": False,
                "reason_code": "",
                "schema_valid": False,
                "correct": False,
                "safe_match": False,
                "load_ms": "",
                "prompt_eval_ms": "",
                "generation_ms": "",
                "prompt_tokens": "",
                "output_tokens": "",
                "error": repr(exc),
            })

        if i % max(1, args.ai_runs // 10) == 0:
            print(f"  AI progress: {i}/{args.ai_runs}")

    print(f"End-to-end pipeline benchmark: {args.pipeline_runs} runs")
    for i in range(1, args.pipeline_runs + 1):
        started = time.perf_counter_ns()
        try:
            current_evidence, c_ms = run_extractor(
                args.extractor, args.verilog, args.qsf
            )
            current_prompt = build_prompt(current_evidence, profiles)
            body, parsed, ai_ms = call_ollama(
                args.ollama_url,
                args.model,
                current_prompt,
                args.timeout,
                args.keep_alive,
            )
            total_ms = (time.perf_counter_ns() - started) / 1_000_000
            raw_ai_target = str(parsed.get("target_board", "") or "").strip()
            guarded = enforce_grounding(parsed, current_evidence)
            checks = validate_ai_result(guarded, args.expected_board)
            pipeline_samples.append(total_ms)

            rows.append({
                "stage": "pipeline",
                "iteration": i,
                "wall_ms": total_ms,
                "raw_ai_target": raw_ai_target,
                "target_board": guarded.get("target_board", ""),
                "guard_applied": guarded.get("guard_applied", False),
                "reason_code": guarded.get("reason_code", ""),
                "schema_valid": checks["schema_valid"],
                "correct": checks["correct"],
                "safe_match": checks["safe_match"],
                "load_ms": body.get("load_duration", 0) / 1_000_000,
                "prompt_eval_ms": body.get("prompt_eval_duration", 0) / 1_000_000,
                "generation_ms": body.get("eval_duration", 0) / 1_000_000,
                "prompt_tokens": body.get("prompt_eval_count", ""),
                "output_tokens": body.get("eval_count", ""),
                "error": f"c_ms={c_ms:.6f};ai_ms={ai_ms:.6f}",
            })
        except Exception as exc:
            rows.append({
                "stage": "pipeline",
                "iteration": i,
                "wall_ms": "",
                "raw_ai_target": "",
                "target_board": "",
                "guard_applied": False,
                "reason_code": "",
                "schema_valid": False,
                "correct": False,
                "safe_match": False,
                "load_ms": "",
                "prompt_eval_ms": "",
                "generation_ms": "",
                "prompt_tokens": "",
                "output_tokens": "",
                "error": repr(exc),
            })

        if i % max(1, args.pipeline_runs // 10) == 0:
            print(f"  Pipeline progress: {i}/{args.pipeline_runs}")

    fieldnames = [
        "stage",
        "iteration",
        "wall_ms",
        "raw_ai_target",
        "target_board",
        "guard_applied",
        "reason_code",
        "schema_valid",
        "correct",
        "safe_match",
        "load_ms",
        "prompt_eval_ms",
        "generation_ms",
        "prompt_tokens",
        "output_tokens",
        "error",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "inputs": {
            "verilog": str(args.verilog),
            "qsf": str(args.qsf),
            "expected_board": args.expected_board,
            "model": args.model,
            "c_runs": args.c_runs,
            "ai_runs": args.ai_runs,
            "pipeline_runs": args.pipeline_runs,
        },
        "c_extractor": stats(c_samples),
        "ai_wall": stats(ai_samples),
        "ai_load": stats(ai_load),
        "ai_prompt_eval": stats(ai_prompt_eval),
        "ai_generation": stats(ai_generation),
        "pipeline_wall": stats(pipeline_samples),
        "ai_correctness": {
            **correctness,
            "attempted": args.ai_runs,
            "schema_valid_rate_percent": (
                100 * correctness["schema_valid"] / args.ai_runs
                if args.ai_runs else 0
            ),
            "raw_ai_accuracy_percent": (
                100 * correctness["raw_correct"] / args.ai_runs
                if args.ai_runs else 0
            ),
            "guarded_accuracy_percent": (
                100 * correctness["correct"] / args.ai_runs
                if args.ai_runs else 0
            ),
            "accuracy_percent": (
                100 * correctness["correct"] / args.ai_runs
                if args.ai_runs else 0
            ),
            "safe_match_rate_percent": (
                100 * correctness["safe_match"] / args.ai_runs
                if args.ai_runs else 0
            ),
        },
    }

    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nCSV samples:  {csv_path.resolve()}")
    print(f"JSON summary: {json_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
