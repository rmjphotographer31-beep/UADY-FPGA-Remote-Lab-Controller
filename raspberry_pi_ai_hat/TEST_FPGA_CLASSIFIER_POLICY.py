#!/usr/bin/env python3
from __future__ import annotations

import json

from fpga_classifier_policy import (
    authoritative_identity,
    build_prompt,
    enforce_grounding,
)

cases = [
    (
        {
            "qsf_device": "5CSEMA5F31C6",
            "qsf_family": "Cyclone V",
            "qsf_board": "DE1-SoC Board",
            "verilog_signals": ["SW", "LEDR"],
        },
        "DE1-SoC",
    ),
    (
        {
            "qsf_device": "5CSEMA5F31C6N",
            "qsf_family": "Cyclone V SoC",
            "qsf_board": "DE1-SoC",
            "verilog_signals": ["HEX0"],
        },
        "DE1-SoC",
    ),
    (
        {
            "qsf_device": "AGFB014R24B2E2V",
            "qsf_family": "Agilex 7",
            "qsf_board": "",
            "verilog_signals": ["SW0", "LED_BRACKET"],
        },
        "DE10-Agilex",
    ),
    (
        {
            "qsf_device": "AGFB014R24B1E1V",
            "qsf_family": "Intel Agilex 7",
            "qsf_board": "DE10-Agilex Board",
            "verilog_signals": ["BUTTON0"],
        },
        "DE10-Agilex",
    ),
]

for evidence, expected in cases:
    result = authoritative_identity(evidence)
    assert result is not None
    assert result["target_board"] == expected
    assert result["safe_to_program"] is True
    assert result["reason_code"] == "EXACT_DEVICE_MATCH"

# Reproduce the observed failure: an Agilex request receives invented DE1 data.
hallucinated = {
    "target_board": "DE1-SoC",
    "confidence_percent": 100,
    "safe_to_program": True,
    "decision_type": "match",
    "reason": "invented previous-job evidence",
    "reason_code": "EXACT_DEVICE_MATCH",
    "observed_qsf_device": "5CSEMA5F31C6",
    "observed_qsf_family": "Cyclone V",
    "observed_qsf_board": "",
    "cited_signals": ["HEX0"],
}
current = cases[2][0]
corrected = enforce_grounding(hallucinated, current)
assert corrected["target_board"] == "DE10-Agilex"
assert corrected["safe_to_program"] is True
assert corrected["raw_ai_target"] == "DE1-SoC"
assert corrected["guard_applied"] is True

conflict = authoritative_identity(
    {
        "qsf_device": "AGFB014R24B2E2V",
        "qsf_family": "Cyclone V",
        "qsf_board": "",
        "verilog_signals": [],
    }
)
assert conflict is not None
assert conflict["target_board"] == "Ambiguous"
assert conflict["decision_type"] == "conflict"
assert conflict["safe_to_program"] is False

# Without authoritative identity, invented copied fields/signals must fail closed.
unresolved = {
    "qsf_device": "",
    "qsf_family": "",
    "qsf_board": "",
    "verilog_signals": ["clk", "data_out"],
    "signal_widths": {"clk": 1, "data_out": 8},
    "verilog_ports": [],
}
invalid_signal_claim = {
    "target_board": "DE1-SoC",
    "confidence_percent": 80,
    "safe_to_program": True,
    "decision_type": "match",
    "reason": "HEX0 identifies DE1-SoC",
    "reason_code": "SIGNAL_FALLBACK",
    "observed_qsf_device": "",
    "observed_qsf_family": "",
    "observed_qsf_board": "",
    "cited_signals": ["HEX0"],
}
rejected = enforce_grounding(invalid_signal_claim, unresolved)
assert rejected["target_board"] == "Ambiguous"
assert rejected["safe_to_program"] is False
assert rejected["reason_code"] == "INVALID_OR_INVENTED_EVIDENCE"

# Prompt context must contain current identity/ports but not the QSF template target list.
prompt_evidence = {
    **current,
    "signal_widths": {"SW0": 1, "LED_BRACKET": 4},
    "verilog_ports": [
        {"name": "SW0", "dir": "input", "width": 1},
        {"name": "LED_BRACKET", "dir": "output", "width": 4},
    ],
    "qsf_targets": ["HEX0", "HPS_DDR3_DQ", "COMMENT"],
}
prompt = build_prompt(prompt_evidence)
assert "{{CURRENT_INPUT_JSON}}" not in prompt
current_json = prompt.split("CURRENT_INPUT_JSON:\n", 1)[1]
prompt_doc = json.loads(current_json)
assert prompt_doc["qsf_device"] == "AGFB014R24B2E2V"
assert "qsf_targets" not in prompt_doc
assert prompt_doc["verilog_signals"] == ["SW0", "LED_BRACKET"]

print("All policy tests passed.")
