# -*- coding: utf-8 -*-
"""
fpga_board_classifier.py

Deterministic top-level Verilog/SystemVerilog board classifier for the UADY FPGA farm.

This module intentionally runs before the Ollama/Qwen prompt. It does not call an LLM.
It extracts deterministic evidence that is then passed into the editable AI prompt file:
FPGA_HARDWARE_CLASSIFIER_PROMPT_v3_84.txt.

It uses board-specific top-level port tokens and bus widths to distinguish:
- Terasic DE1-SoC
- Terasic DE10-Agilex

If both boards' exclusive signatures are present, it reports a conflict and the
caller must block automatic programming.
"""
from __future__ import annotations

import re
import os
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Exact tokens that should only appear in a DE1-SoC top-level design.
DE1_EXCLUSIVE_TOKENS = {
    # User I/O / displays
    "LEDR", "HEX0", "HEX1", "HEX2", "HEX3", "HEX4", "HEX5",
    # VGA
    "VGA_R", "VGA_G", "VGA_B", "VGA_HS", "VGA_VS", "VGA_CLK", "VGA_BLANK_N", "VGA_SYNC_N",
    # Audio
    "AUD_ADCDAT", "AUD_ADCLRCK", "AUD_BCLK", "AUD_DACDAT", "AUD_DACLRCK", "AUD_XCK",
    # Legacy peripherals / memory
    "TD_DATA", "TD_CLK27", "PS2_CLK", "PS2_DAT", "IRDA_RXD", "IRDA_TXD",
    # DE1 clocks and GPIO
    "CLOCK_50", "CLOCK2_50", "CLOCK3_50", "CLOCK4_50", "GPIO_0", "GPIO_1",
}

DE1_PREFIXES = (
    "HPS_",
    "DRAM_",
)

# Exact tokens that should only appear in a DE10-Agilex top-level design.
DE10_EXCLUSIVE_TOKENS = {
    # PCIe
    "PCIE_TX_p", "PCIE_TX_n", "PCIE_RX_p", "PCIE_RX_n", "PCIE_REFCLK_p", "PCIE_REFCLK_n", "PCIE_PERST_n",
    # Clocks / timing
    "SI5340A_I2C_SCL", "SI5340A_I2C_SDA", "SI5340A_LOL", "SI5340A_LOS_XAXB",
    "CLK_100M", "CLK_50M", "CLK_50_B2C", "CLK_50_B3A",
    # MAX 10 / system management
    "M10_SYS_SCL", "M10_SYS_SDA",
    # GPIO-style DE10 pins. These are board-specific enough to keep strong.
    "GPIO_P0", "GPIO_P1", "GPIO_P2", "GPIO_P3", "GPIO_CLK0", "GPIO_CLK1",
}

# These look like DE10-Agilex simple user-I/O names, but they are NOT safe
# 100% evidence by themselves. A DE1-SoC student may intentionally expose only
# SW[1:0], KEY[1:0], or LED[3:0] in their top-level module while the QSF maps
# those signals to DE1 pins. Therefore these are weak/ambiguous unless a QSF or
# another exclusive token confirms the board.
DE10_WEAK_USER_IO_TOKENS = {
    "BUTTON0", "BUTTON1", "PB0", "PB1", "SW0", "SW1", "LED0", "LED1", "LED2", "LED3",
}

DE10_PREFIXES = (
    "PCIE_",
    "QSFPDDA_", "QSFPDDB_",
    "DDRA_", "DDRB_", "DDRC_", "DDRD_",
    "SI5340A_", "M10_",
)

DIRECTION_WORDS = {"input", "output", "inout", "wire", "reg", "logic", "signed", "tri", "bit"}
SV_KEYWORDS = DIRECTION_WORDS | {
    "module", "endmodule", "parameter", "localparam", "assign", "always", "begin", "end", "if", "else",
    "case", "endcase", "for", "generate", "endgenerate", "genvar", "integer", "initial",
}


def strip_comments_and_strings(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"//.*", "", code)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    return code


def find_primary_module(code: str) -> Tuple[str, str, str]:
    """Return module name, port header text, and module body for first non-testbench module."""
    clean = strip_comments_and_strings(code)
    modules = []
    for m in re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*?\)\s*)?\((.*?)\)\s*;", clean, flags=re.S):
        name = m.group(1)
        header = m.group(2)
        end = clean.find("endmodule", m.end())
        body = clean[m.end(): end if end != -1 else len(clean)]
        modules.append((name, header, body))
    if not modules:
        return "", "", clean
    # Prefer non-testbench-looking module, otherwise first module.
    for name, header, body in modules:
        lname = name.lower()
        if not any(x in lname for x in ("tb", "testbench", "sim")):
            return name, header, body
    return modules[0]


def split_ports(header: str) -> List[str]:
    parts, cur, depth = [], [], 0
    for ch in header:
        if ch in "([{" : depth += 1
        elif ch in ")]}": depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            part = "".join(cur).strip()
            if part: parts.append(part)
            cur = []
        else:
            cur.append(ch)
    part = "".join(cur).strip()
    if part: parts.append(part)
    return parts


def width_from_range(range_text: str) -> int:
    m = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_text or "")
    if not m: return 1
    return abs(int(m.group(1)) - int(m.group(2))) + 1


def extract_declared_ports(code: str) -> Dict[str, Dict[str, Any]]:
    # find_primary_module() already strips comments/strings and returns header/body
    # from that cleaned text.  Avoid doing the same full-file regex cleanup twice
    # for every classification request.
    module_name, header, body = find_primary_module(code)
    ports: Dict[str, Dict[str, Any]] = {}

    # ANSI-style ports inside module header.
    for part in split_ports(header):
        direction_match = re.search(r"\b(input|output|inout)\b", part)
        direction = direction_match.group(1) if direction_match else ""
        width = width_from_range(part)
        # Last identifier in the port part is normally the port name.
        ids = [x for x in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", part) if x not in SV_KEYWORDS]
        if ids:
            name = ids[-1]
            if name != module_name:
                ports[name] = {"name": name, "direction": direction, "width": width, "source": "module_port_list"}

    # Non-ANSI declarations in body. Only keep names that also appeared in raw header list when possible.
    raw_header_ids = set(x for x in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", header) if x not in SV_KEYWORDS)
    for m in re.finditer(r"\b(input|output|inout)\b\s+([^;]+);", body, flags=re.S):
        direction = m.group(1)
        decl = m.group(2)
        width = width_from_range(decl)
        decl_no_range = re.sub(r"\[[^\]]+\]", " ", decl)
        decl_no_range = re.sub(r"\b(?:wire|reg|logic|signed|tri|bit)\b", " ", decl_no_range)
        for chunk in decl_no_range.split(','):
            ids = [x for x in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", chunk) if x not in SV_KEYWORDS]
            if not ids: continue
            name = ids[-1]
            if raw_header_ids and name not in raw_header_ids:
                continue
            ports[name] = {"name": name, "direction": direction, "width": width, "source": "declaration"}

    # Fallback: if no declarations parsed, include identifier-like header names as unknown ports.
    if not ports:
        for name in raw_header_ids:
            if name != module_name:
                ports[name] = {"name": name, "direction": "", "width": 1, "source": "fallback_header"}

    return ports


def evidence(board: str, token: str, reason: str, confidence: int = 100) -> Dict[str, Any]:
    return {"board": board, "token": token, "reason": reason, "confidence": confidence}


def qsf_evidence(qsf_text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    q = qsf_text or ""
    de1, de10 = [], []
    if re.search(r'FAMILY\s+"?Cyclone\s*V"?', q, flags=re.I):
        de1.append(evidence("DE1-SoC", 'FAMILY "Cyclone V"', "QSF family matches DE1-SoC/Cyclone V"))
    if re.search(r"5CSEMA5F31C6", q, flags=re.I):
        de1.append(evidence("DE1-SoC", "5CSEMA5F31C6", "DE1-SoC FPGA device token found in QSF"))
    if re.search(r'FAMILY\s+"?Agilex', q, flags=re.I):
        de10.append(evidence("DE10-Agilex", 'FAMILY "Agilex"', "QSF family matches Agilex"))
    if re.search(r"AGFB014R24B", q, flags=re.I):
        de10.append(evidence("DE10-Agilex", "AGFB014R24B", "DE10-Agilex FPGA device token found in QSF"))
    for tok in ("HEX0", "HEX1", "LEDR", "SW[9]", "KEY[3]"):
        if tok in q:
            de1.append(evidence("DE1-SoC", tok, "QSF pin assignment uses DE1-SoC style signal"))
    for tok in ("PCIE", "QSFPDDA", "QSFPDDB", "DDRA", "DDRB", "DDRC", "DDRD", "SI5340A"):
        if re.search(r"\b" + re.escape(tok), q):
            de10.append(evidence("DE10-Agilex", tok, "QSF pin assignment uses DE10-Agilex style signal"))
    return de1, de10


def classify_fpga_board_python(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    ports = extract_declared_ports(verilog_code or "")
    port_names = set(ports.keys())
    de1: List[Dict[str, Any]] = []
    de10: List[Dict[str, Any]] = []
    weak: List[Dict[str, Any]] = []

    # Exact and prefix top-level port evidence.
    for name in sorted(port_names):
        if name in DE1_EXCLUSIVE_TOKENS:
            de1.append(evidence("DE1-SoC", name, "exclusive DE1-SoC top-level port token"))
        if any(name.startswith(p) for p in DE1_PREFIXES):
            de1.append(evidence("DE1-SoC", name, "exclusive DE1-SoC top-level port prefix"))

        if name in DE10_EXCLUSIVE_TOKENS:
            de10.append(evidence("DE10-Agilex", name, "exclusive DE10-Agilex top-level port token"))
        if any(name.startswith(p) for p in DE10_PREFIXES):
            de10.append(evidence("DE10-Agilex", name, "exclusive DE10-Agilex top-level port prefix"))

        if name in DE10_WEAK_USER_IO_TOKENS:
            weak.append(evidence(
                "DE10-Agilex",
                name,
                "weak DE10-style simple user-I/O token; not safe by itself because users can rename partial DE1-SoC signals",
                55
            ))

    # Width rules.
    def pwidth(name: str) -> int:
        return int(ports.get(name, {}).get("width", 0) or 0)

    # Strong DE1 widths remain safe because these match the real DE1-SoC public I/O width/name.
    if pwidth("SW") == 10:
        de1.append(evidence("DE1-SoC", "SW[9:0]", "DE1-SoC full switch bus width"))
    if pwidth("LEDR") == 10:
        de1.append(evidence("DE1-SoC", "LEDR[9:0]", "DE1-SoC full LEDR bus width"))
    if pwidth("KEY") == 4:
        de1.append(evidence("DE1-SoC", "KEY[3:0]", "DE1-SoC full KEY bus width"))
    for h in ("HEX0", "HEX1", "HEX2", "HEX3", "HEX4", "HEX5"):
        if pwidth(h) == 7:
            de1.append(evidence("DE1-SoC", f"{h}[6:0]", "DE1-SoC seven-segment display width"))
    if pwidth("GPIO_0") == 36:
        de1.append(evidence("DE1-SoC", "GPIO_0[35:0]", "DE1-SoC GPIO header width"))
    if pwidth("GPIO_1") == 36:
        de1.append(evidence("DE1-SoC", "GPIO_1[35:0]", "DE1-SoC GPIO header width"))

    # Important safety change:
    # These widths are not hard DE10 evidence. They can also be intentional
    # partial DE1-SoC usage, for example input [1:0] SW on a DE1 design.
    if pwidth("SW") == 2:
        weak.append(evidence(
            "Unknown",
            "SW[1:0]",
            "ambiguous subset width: could be DE10-Agilex full switch bus or partial DE1-SoC switch usage",
            50
        ))
    if pwidth("LED") == 4:
        weak.append(evidence(
            "Unknown",
            "LED[3:0]",
            "ambiguous simple LED width: could be DE10-Agilex user LEDs or user-renamed DE1-SoC LEDs",
            50
        ))
    if pwidth("KEY") == 2:
        weak.append(evidence(
            "Unknown",
            "KEY[1:0]",
            "ambiguous subset width: could be DE10-Agilex or partial DE1-SoC key usage",
            50
        ))
    if pwidth("BUTTON") == 2:
        weak.append(evidence(
            "Unknown",
            "BUTTON[1:0]",
            "ambiguous button width/name: not safe to select DE10-Agilex without QSF or exclusive token",
            50
        ))

    qsf_de1, qsf_de10 = qsf_evidence(qsf_text or "")
    de1.extend(qsf_de1)
    de10.extend(qsf_de10)

    # De-duplicate evidence by token+board.
    def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for item in items:
            key = (item.get("board"), item.get("token"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    de1, de10, weak = dedupe(de1), dedupe(de10), dedupe(weak)
    conflicts: List[str] = []

    detected_ports = [
        {"name": p["name"], "direction": p.get("direction", ""), "width": p.get("width", 1), "source": p.get("source", "")}
        for p in sorted(ports.values(), key=lambda x: x.get("name", ""))
    ]

    # Strong evidence only can create a conflict. Weak subset evidence should not
    # conflict with a strong DE1 or strong DE10 result.
    if de1 and de10:
        conflicts = ["DE1-SoC evidence and DE10-Agilex evidence found in the same design/QSF"]
        return {
            "target_board": "Conflict - unsafe to program",
            "confidence_score": 100,
            "decision_type": "conflict",
            "safe_to_program": False,
            "core_evidence": de1 + de10,
            "weak_evidence": weak,
            "detected_ports": detected_ports,
            "conflicts": conflicts,
            "recommended_action": "Do not program automatically. Fix the top-level ports/QSF or choose a compatible design.",
        }

    if de1:
        return {
            "target_board": "DE1-SoC",
            "confidence_score": 100,
            "decision_type": "exclusive_token_or_strong_width_rule",
            "safe_to_program": True,
            "core_evidence": de1,
            "weak_evidence": weak,
            "detected_ports": detected_ports,
            "conflicts": [],
            "recommended_action": "Program a DE1-SoC board instance.",
        }

    if de10:
        return {
            "target_board": "DE10-Agilex",
            "confidence_score": 100,
            "decision_type": "exclusive_token_or_qsf_rule",
            "safe_to_program": True,
            "core_evidence": de10,
            "weak_evidence": weak,
            "detected_ports": detected_ports,
            "conflicts": [],
            "recommended_action": "Program a DE10-Agilex board instance.",
        }

    # Filename/module-name hint is weak only.
    fname = (filename or "").lower()
    if any(x in fname for x in ("de1", "cyclone")):
        weak.append(evidence("DE1-SoC", filename, "weak filename hint only", 55))
    if any(x in fname for x in ("de10", "agilex")):
        weak.append(evidence("DE10-Agilex", filename, "weak filename hint only", 55))
    weak = dedupe(weak)

    if weak:
        return {
            "target_board": "Unknown",
            "confidence_score": max(int(x.get("confidence", 0) or 0) for x in weak),
            "decision_type": "ambiguous_subset_width",
            "safe_to_program": False,
            "core_evidence": [],
            "weak_evidence": weak,
            "detected_ports": detected_ports,
            "conflicts": [],
            "recommended_action": "Ambiguous simple/subset I/O. Select DE1-SoC or DE10-Agilex manually, or provide a QSF with board pin assignments.",
        }

    return {
        "target_board": "Unknown",
        "confidence_score": 0,
        "decision_type": "unknown",
        "safe_to_program": False,
        "core_evidence": [],
        "weak_evidence": [],
        "detected_ports": detected_ports,
        "conflicts": [],
        "recommended_action": "Ask user to select the board or provide a QSF/top-level pin list.",
    }


# v4.02 C accelerator wrapper -------------------------------------------------
def _c_classifier_binary_path() -> Path:
    return Path(__file__).resolve().with_name("fpga_board_classifier_c")


def _classify_fpga_board_c(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    """Run the optional compiled C classifier. Falls back cleanly if unavailable."""
    binary = _c_classifier_binary_path()
    if os.environ.get("UADY_DISABLE_C_CLASSIFIER", "0") in ("1", "true", "yes"):
        raise RuntimeError("C classifier disabled by UADY_DISABLE_C_CLASSIFIER")
    if not binary.exists() or not os.access(str(binary), os.X_OK):
        raise RuntimeError("C classifier binary is not built")
    with tempfile.TemporaryDirectory(prefix="uady_c_classifier_") as td:
        v_path = Path(td) / "design.v"
        q_path = Path(td) / "design.qsf"
        v_path.write_text(verilog_code or "", encoding="utf-8", errors="ignore")
        q_path.write_text(qsf_text or "", encoding="utf-8", errors="ignore")
        out = subprocess.check_output(
            [str(binary), str(v_path), str(q_path), filename or "uploaded.v"],
            timeout=1.0,
            stderr=subprocess.STDOUT,
        )
    result = json.loads(out.decode("utf-8", errors="replace"))
    if not isinstance(result, dict) or not result.get("target_board"):
        raise RuntimeError("C classifier returned invalid JSON")
    result["classifier_engine"] = "c_accelerated"
    return result


def classify_fpga_board(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    """Classify board using the fast C scanner when available, with Python fallback.

    The C program is only a deterministic token scanner. It does not replace the
    queue engine, Quartus programming, or AI fallback. If the C binary is missing
    or fails, the previous Python classifier is used automatically.
    """
    try:
        return _classify_fpga_board_c(verilog_code, qsf_text=qsf_text, filename=filename)
    except Exception as e:
        result = classify_fpga_board_python(verilog_code, qsf_text=qsf_text, filename=filename)
        result["classifier_engine"] = "python_fallback"
        result["c_classifier_fallback_reason"] = str(e)
        return result


def classifier_features(classification: Dict[str, Any]) -> List[str]:
    """Translate deterministic classification into the existing feature vocabulary."""
    board = classification.get("target_board")
    if board == "DE1-SoC":
        feats = {"simple_io"}
        for ev in classification.get("core_evidence", []) or []:
            tok = str(ev.get("token", ""))
            if tok.startswith("HEX"): feats.add("hex")
            if tok.startswith("SW"): feats.add("switches")
            if tok.startswith("KEY"): feats.add("keys")
            if tok.startswith("LEDR"): feats.add("leds")
            if tok.startswith("GPIO_"): feats.add("gpio")
        return sorted(feats)
    if board == "DE10-Agilex":
        feats = {"de10_user_io"}
        for ev in classification.get("core_evidence", []) or []:
            tok = str(ev.get("token", ""))
            if tok.startswith("PCIE"): feats.add("pcie"); feats.add("high_speed"); feats.add("transceivers")
            if tok.startswith("QSFP"): feats.add("qsfp"); feats.add("high_speed"); feats.add("transceivers")
            if tok.startswith(("DDRA", "DDRB", "DDRC", "DDRD")): feats.add("ddr4")
            if tok.startswith("SI5340A") or tok.startswith("CLK_"): feats.add("agilex_clocks")
            if tok.startswith("M10"): feats.add("info_spi")
            if tok.startswith("SW"): feats.add("de10_switches")
            if tok.startswith(("LED", "LED[")): feats.add("de10_leds")
            if tok.startswith(("BUTTON", "PB", "KEY")): feats.add("de10_buttons")
            if tok.startswith("GPIO_P") or tok.startswith("GPIO_CLK"): feats.add("de10_gpio")
        return sorted(feats)
    return []



def build_ai_prompt_context(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    """Return the scanner context that the Qwen prompt should read.

    This is useful for debugging: the LLM prompt receives this JSON plus the
    raw Verilog/QSF. The Python/C classifier is therefore the fast evidence
    extractor, while FPGA_HARDWARE_CLASSIFIER_PROMPT_v3_84.txt is the AI prompt.
    """
    classification = classify_fpga_board(verilog_code, qsf_text=qsf_text, filename=filename)
    return {
        "filename": filename,
        "local_classifier": classification,
        "features": classifier_features(classification),
        "prompt_file": "FPGA_HARDWARE_CLASSIFIER_PROMPT_v3_84.txt",
        "prompt_role": "Qwen reads this context and returns the final JSON board classification unless the deterministic safety guard blocks it.",
    }
