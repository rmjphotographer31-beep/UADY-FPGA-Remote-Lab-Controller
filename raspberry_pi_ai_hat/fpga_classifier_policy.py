from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALLOWED_BOARDS = {"DE1-SoC", "DE10-Agilex", "Ambiguous"}

# Project-observed Quartus part names plus variants documented in the manuals.
DEVICE_TO_BOARD = {
    # DE1-SoC: Quartus QSF form and package-marking form in the manual.
    "5CSEMA5F31C6": "DE1-SoC",
    "5CSEMA5F31C6N": "DE1-SoC",
    # DE10-Agilex Rev.C device variants documented by Terasic.
    "AGFB014R24B2E2V": "DE10-Agilex",
    "AGFB014R24B1E1V": "DE10-Agilex",
}

FAMILY_TO_BOARD = {
    "CYCLONE V": "DE1-SoC",
    "CYCLONE V SOC": "DE1-SoC",
    "AGILEX 7": "DE10-Agilex",
    "INTEL AGILEX 7": "DE10-Agilex",
}

BOARD_NAME_TO_BOARD = {
    "DE1-SOC BOARD": "DE1-SoC",
    "DE1-SOC": "DE1-SoC",
    "TERASIC DE1-SOC": "DE1-SoC",
    "DE10-AGILEX": "DE10-Agilex",
    "DE10-AGILEX BOARD": "DE10-Agilex",
    "TERASIC DE10-AGILEX": "DE10-Agilex",
}

PROMPT_PATH = Path(__file__).with_name("ollama_fpga_classifier_prompt.txt")


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def build_prompt(evidence: dict[str, Any], profiles: Any = None) -> str:
    """Build a fresh, stateless prompt from only current, relevant evidence.

    QSF targets are deliberately omitted because a Quartus QSF can be a complete
    board template containing hundreds of assignments unused by the submitted
    top-level module.  Keeping only identity and actual Verilog port evidence
    also reduces Ollama context size and prevents template signals from biasing
    the model.
    """
    del profiles
    template = PROMPT_PATH.read_text(encoding="utf-8")
    compact_evidence = {
        "qsf_device": str(evidence.get("qsf_device", "") or ""),
        "qsf_family": str(evidence.get("qsf_family", "") or ""),
        "qsf_board": str(evidence.get("qsf_board", "") or ""),
        "verilog_signals": list(evidence.get("verilog_signals", []) or []),
        "signal_widths": dict(evidence.get("signal_widths", {}) or {}),
        "verilog_ports": list(evidence.get("verilog_ports", []) or []),
    }
    compact = json.dumps(compact_evidence, ensure_ascii=False, separators=(",", ":"))
    return template.replace("{{CURRENT_INPUT_JSON}}", compact)


def authoritative_identity(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve exact QSF identity without an LLM; return None when unresolved."""
    raw_device = str(evidence.get("qsf_device", "") or "").strip()
    raw_family = str(evidence.get("qsf_family", "") or "").strip()
    raw_board = str(evidence.get("qsf_board", "") or "").strip()

    device_board = DEVICE_TO_BOARD.get(_norm(raw_device))
    family_board = FAMILY_TO_BOARD.get(_norm(raw_family))
    name_board = BOARD_NAME_TO_BOARD.get(_norm(raw_board)) if raw_board else None

    recognized = [x for x in (device_board, family_board, name_board) if x]
    unique = set(recognized)

    base = {
        "observed_qsf_device": raw_device,
        "observed_qsf_family": raw_family,
        "observed_qsf_board": raw_board,
        "cited_signals": [],
    }

    if len(unique) > 1:
        return {
            **base,
            "target_board": "Ambiguous",
            "confidence_percent": 0,
            "safe_to_program": False,
            "decision_type": "conflict",
            "reason_code": "AUTHORITATIVE_CONFLICT",
            "reason": "Recognized QSF identity fields point to different supported boards.",
        }

    if device_board:
        return {
            **base,
            "target_board": device_board,
            "confidence_percent": 100,
            "safe_to_program": True,
            "decision_type": "match",
            "reason_code": "EXACT_DEVICE_MATCH",
            "reason": f"Exact QSF device {raw_device} identifies {device_board}.",
        }

    if unique:
        selected = next(iter(unique))
        return {
            **base,
            "target_board": selected,
            "confidence_percent": 90,
            "safe_to_program": False,
            "decision_type": "match",
            "reason_code": "FAMILY_OR_BOARD_MATCH",
            "reason": "Recognized QSF family or board name identifies the supported board, but no exact supported device was found.",
        }

    return None


def _grounding_fields_valid(result: dict[str, Any], evidence: dict[str, Any]) -> bool:
    expected_device = str(evidence.get("qsf_device", "") or "")
    expected_family = str(evidence.get("qsf_family", "") or "")
    expected_board = str(evidence.get("qsf_board", "") or "")
    current_signals = {str(x) for x in evidence.get("verilog_signals", [])}

    if result.get("observed_qsf_device") != expected_device:
        return False
    if result.get("observed_qsf_family") != expected_family:
        return False
    if result.get("observed_qsf_board") != expected_board:
        return False

    cited = result.get("cited_signals", [])
    if not isinstance(cited, list):
        return False
    return all(isinstance(x, str) and x in current_signals for x in cited)


def enforce_grounding(
    ai_result: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """
    Enforce exact hardware identity and reject invented evidence.

    Exact device/family/board identity is deterministic. Ollama is used only
    for unresolved inputs, and its copied evidence must match the current JSON.
    """
    authoritative = authoritative_identity(evidence)
    if authoritative is not None:
        authoritative["raw_ai_target"] = str(ai_result.get("target_board", "") or "")
        authoritative["guard_applied"] = True
        return authoritative

    if not _grounding_fields_valid(ai_result, evidence):
        return {
            "target_board": "Ambiguous",
            "confidence_percent": 0,
            "safe_to_program": False,
            "decision_type": "conflict",
            "reason_code": "INVALID_OR_INVENTED_EVIDENCE",
            "reason": "The AI response did not copy current evidence exactly or cited a signal absent from the current Verilog input.",
            "observed_qsf_device": str(evidence.get("qsf_device", "") or ""),
            "observed_qsf_family": str(evidence.get("qsf_family", "") or ""),
            "observed_qsf_board": str(evidence.get("qsf_board", "") or ""),
            "cited_signals": [],
            "raw_ai_target": str(ai_result.get("target_board", "") or ""),
            "guard_applied": True,
        }

    cleaned = dict(ai_result)
    target = str(cleaned.get("target_board", "") or "").strip()
    if target not in ALLOWED_BOARDS:
        cleaned.update({
            "target_board": "Ambiguous",
            "confidence_percent": 0,
            "safe_to_program": False,
            "decision_type": "ambiguous",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "reason": "The AI returned an unsupported board label.",
        })

    # Signal-only classifications are useful for routing hints but are never
    # sufficient for automatic physical programming.
    cleaned["safe_to_program"] = False
    cleaned["guard_applied"] = False
    return cleaned


def ollama_json_schema() -> dict[str, Any]:
    """Schema usable as Ollama's `format` value on versions supporting JSON schema."""
    return {
        "type": "object",
        "properties": {
            "target_board": {"type": "string", "enum": sorted(ALLOWED_BOARDS)},
            "confidence_percent": {"type": "integer", "minimum": 0, "maximum": 100},
            "safe_to_program": {"type": "boolean"},
            "decision_type": {"type": "string", "enum": ["match", "ambiguous", "conflict"]},
            "reason": {"type": "string"},
            "reason_code": {
                "type": "string",
                "enum": [
                    "EXACT_DEVICE_MATCH",
                    "FAMILY_OR_BOARD_MATCH",
                    "SIGNAL_FALLBACK",
                    "AUTHORITATIVE_CONFLICT",
                    "INSUFFICIENT_EVIDENCE",
                    "INVALID_OR_INVENTED_EVIDENCE",
                ],
            },
            "observed_qsf_device": {"type": "string"},
            "observed_qsf_family": {"type": "string"},
            "observed_qsf_board": {"type": "string"},
            "cited_signals": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "target_board",
            "confidence_percent",
            "safe_to_program",
            "decision_type",
            "reason",
            "reason_code",
            "observed_qsf_device",
            "observed_qsf_family",
            "observed_qsf_board",
            "cited_signals",
        ],
        "additionalProperties": False,
    }
