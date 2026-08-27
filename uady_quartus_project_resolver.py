# -*- coding: utf-8 -*-
"""Quartus .qpf project resolver for UADY GUI/laptop tester.

Given a Quartus Project File (.qpf), find the matching .qsf, top-level
Verilog/SystemVerilog source, and compiled .sof file.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERILOG_SUFFIXES = (".v", ".sv")
SOURCE_ASSIGNMENT_NAMES = {"VERILOG_FILE", "SYSTEMVERILOG_FILE"}
SKIP_DIR_NAMES = {
    "db", "incremental_db", "output_files", "simulation", "greybox_tmp",
    ".git", "__pycache__", "hps_isw_handoff", "software",
}


def _strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _strip_inline_comment(line: str) -> str:
    """Remove # comments unless inside double/single quotes."""
    out = []
    quote = ""
    escape = False
    for ch in line:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            out.append(ch)
            quote = ch
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _resolve_path(base: Path, raw: str) -> Path:
    raw = _strip_quotes(str(raw or "").strip())
    raw = raw.replace("\\", os.sep).replace("/", os.sep)
    # Ignore Quartus variable paths that cannot be resolved safely on a laptop.
    raw = re.sub(r"^\$::quartus\([^)]*\)[/\\]?", "", raw)
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return (base / p).resolve()


def parse_qpf(qpf_path: str | Path) -> Dict[str, Any]:
    qpf = Path(qpf_path).expanduser().resolve()
    text = _read_text(qpf)
    revisions: List[str] = []
    for line in text.splitlines():
        clean = _strip_inline_comment(line)
        m = re.match(r"\s*PROJECT_REVISION\s*=\s*(.+?)\s*$", clean, re.I)
        if m:
            value = _strip_quotes(m.group(1))
            if value and value not in revisions:
                revisions.append(value)
    if not revisions:
        revisions.append(qpf.stem)
    return {"qpf_path": str(qpf), "project_dir": str(qpf.parent), "project_name": qpf.stem, "revisions": revisions, "raw_text": text}


def parse_qsf(qsf_path: str | Path) -> Dict[str, Any]:
    qsf = Path(qsf_path).expanduser().resolve()
    text = _read_text(qsf)
    assignments: Dict[str, List[str]] = {}
    source_files: List[Path] = []
    top_level = ""
    output_dir = ""
    family = ""
    device = ""

    for line in text.splitlines():
        clean = _strip_inline_comment(line)
        if not clean:
            continue
        m = re.match(r"\s*set_global_assignment\s+-name\s+([^\s]+)\s+(.+?)\s*$", clean, re.I)
        if not m:
            continue
        name = m.group(1).strip().upper()
        value = _strip_quotes(m.group(2).strip())
        assignments.setdefault(name, []).append(value)
        if name in SOURCE_ASSIGNMENT_NAMES:
            p = _resolve_path(qsf.parent, value)
            if p.suffix.lower() in VERILOG_SUFFIXES and p not in source_files:
                source_files.append(p)
        elif name == "TOP_LEVEL_ENTITY" and value:
            top_level = value
        elif name == "PROJECT_OUTPUT_DIRECTORY" and value:
            output_dir = value
        elif name == "FAMILY" and value:
            family = value
        elif name == "DEVICE" and value:
            device = value

    return {
        "qsf_path": str(qsf),
        "qsf_text": text,
        "assignments": assignments,
        "source_files": [str(p) for p in source_files],
        "top_level_entity": top_level,
        "project_output_directory": output_dir,
        "family": family,
        "device": device,
    }


def _find_qsf(qpf: Path, revisions: List[str]) -> Tuple[Optional[Path], List[Path]]:
    project_dir = qpf.parent
    ordered: List[Path] = []
    for stem in revisions + [qpf.stem]:
        p = project_dir / f"{stem}.qsf"
        if p.exists() and p not in ordered:
            ordered.append(p.resolve())
    for p in sorted(project_dir.glob("*.qsf")):
        rp = p.resolve()
        if rp not in ordered:
            ordered.append(rp)
    return (ordered[0] if ordered else None), ordered


def _iter_verilog_project_files(project_dir: Path) -> List[Path]:
    found: List[Path] = []
    for p in project_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VERILOG_SUFFIXES:
            continue
        rel_parts = {part.lower() for part in p.relative_to(project_dir).parts[:-1]}
        if rel_parts & SKIP_DIR_NAMES:
            continue
        found.append(p.resolve())
    found.sort(key=lambda x: (len(x.parts), str(x).lower()))
    return found


def _module_declares_top(path: Path, top_level: str) -> bool:
    if not top_level:
        return False
    text = _read_text(path)
    pat = re.compile(r"\bmodule\s+" + re.escape(top_level) + r"\b", re.I)
    return bool(pat.search(text))


def choose_top_verilog(project_dir: Path, qsf_info: Dict[str, Any], revisions: List[str]) -> Tuple[Optional[Path], List[Path], str]:
    qsf_sources = [Path(p).resolve() for p in qsf_info.get("source_files", []) if Path(p).exists()]
    all_sources: List[Path] = []
    for p in qsf_sources + _iter_verilog_project_files(project_dir):
        if p.exists() and p.suffix.lower() in VERILOG_SUFFIXES and p not in all_sources:
            all_sources.append(p)

    top = str(qsf_info.get("top_level_entity") or "").strip()
    if top:
        for p in all_sources:
            if _module_declares_top(p, top):
                return p, all_sources, f"matched TOP_LEVEL_ENTITY module {top}"
        for p in all_sources:
            if p.stem.lower() == top.lower():
                return p, all_sources, f"matched TOP_LEVEL_ENTITY filename {top}"

    preferred_stems = [s.lower() for s in revisions] + [project_dir.name.lower()]
    for p in all_sources:
        if p.stem.lower() in preferred_stems:
            return p, all_sources, "matched project/revision filename"

    if all_sources:
        return all_sources[0], all_sources, "first Verilog/SystemVerilog source found"
    return None, all_sources, "no Verilog/SystemVerilog source found"


def _sof_candidates(project_dir: Path, qsf_info: Dict[str, Any], revisions: List[str], qpf_stem: str) -> List[Path]:
    candidates: List[Path] = []
    output_raw = str(qsf_info.get("project_output_directory") or "output_files").strip() or "output_files"
    output_dir = _resolve_path(Path(str(qsf_info.get("qsf_path") or project_dir)).parent if qsf_info.get("qsf_path") else project_dir, output_raw)
    stems = []
    for s in revisions + [qpf_stem, str(qsf_info.get("top_level_entity") or "")]:
        if s and s not in stems:
            stems.append(s)
    for base in [output_dir, project_dir / "output_files", project_dir]:
        for stem in stems:
            p = base / f"{stem}.sof"
            if p.exists() and p not in candidates:
                candidates.append(p.resolve())
    for p in project_dir.rglob("*.sof"):
        rel_parts = {part.lower() for part in p.relative_to(project_dir).parts[:-1]}
        # output_files is allowed for SOF, but ignore database folders.
        if rel_parts & {"db", "incremental_db", ".git", "__pycache__"}:
            continue
        rp = p.resolve()
        if rp not in candidates:
            candidates.append(rp)
    candidates.sort(key=lambda p: (0 if p.parent.name.lower() == "output_files" else 1, -p.stat().st_mtime if p.exists() else 0, str(p).lower()))
    return candidates


def resolve_quartus_project(qpf_path: str | Path) -> Dict[str, Any]:
    qpf = Path(str(qpf_path).strip('"')).expanduser().resolve()
    if not qpf.exists():
        return {"success": False, "error": "QPF file not found", "qpf_path": str(qpf)}
    if qpf.suffix.lower() != ".qpf":
        return {"success": False, "error": "Selected file is not a .qpf Quartus project file", "qpf_path": str(qpf)}

    qpf_info = parse_qpf(qpf)
    revisions = qpf_info["revisions"]
    qsf_path, qsf_candidates = _find_qsf(qpf, revisions)
    qsf_info: Dict[str, Any] = {}
    warnings: List[str] = []
    if qsf_path:
        qsf_info = parse_qsf(qsf_path)
    else:
        warnings.append("No .qsf was found next to the .qpf. AI can still read Verilog, but board accuracy is lower.")

    top_v, all_sources, top_reason = choose_top_verilog(qpf.parent, qsf_info, revisions)
    if not top_v:
        warnings.append("No .v/.sv source file was found for this project.")

    sofs = _sof_candidates(qpf.parent, qsf_info, revisions, qpf.stem)
    sof_path = sofs[0] if sofs else None
    if not sof_path:
        warnings.append("No .sof was found. Compile the Quartus project first so output_files/<revision>.sof exists.")

    return {
        "success": bool(top_v and sof_path),
        "qpf_path": str(qpf),
        "project_dir": str(qpf.parent),
        "project_name": qpf.stem,
        "revisions": revisions,
        "selected_qsf": str(qsf_path) if qsf_path else "",
        "qsf_candidates": [str(p) for p in qsf_candidates],
        "selected_verilog": str(top_v) if top_v else "",
        "verilog_selection_reason": top_reason,
        "verilog_sources": [str(p) for p in all_sources],
        "selected_sof": str(sof_path) if sof_path else "",
        "sof_candidates": [str(p) for p in sofs],
        "qsf_top_level_entity": qsf_info.get("top_level_entity", ""),
        "qsf_family": qsf_info.get("family", ""),
        "qsf_device": qsf_info.get("device", ""),
        "qsf_project_output_directory": qsf_info.get("project_output_directory", ""),
        "warnings": warnings,
    }


def format_resolution_summary(info: Dict[str, Any]) -> str:
    if not info.get("success"):
        prefix = "QPF project resolved with missing files"
    else:
        prefix = "QPF project resolved"
    lines = [prefix]
    lines.append(f"QPF: {info.get('qpf_path', '')}")
    lines.append(f"QSF: {info.get('selected_qsf') or 'NOT FOUND'}")
    lines.append(f"Verilog: {info.get('selected_verilog') or 'NOT FOUND'}")
    lines.append(f"SOF: {info.get('selected_sof') or 'NOT FOUND'}")
    if info.get("qsf_top_level_entity"):
        lines.append(f"Top-level: {info.get('qsf_top_level_entity')}")
    for w in info.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Resolve Quartus .qpf to .qsf/.v/.sof")
    ap.add_argument("qpf")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()
    result = resolve_quartus_project(ns.qpf)
    if ns.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_resolution_summary(result))
