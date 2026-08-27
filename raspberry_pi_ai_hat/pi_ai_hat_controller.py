#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UADY Raspberry Pi AI/HAT Dynamic JTAG Controller v5.4 + Manual-Grounded AI Guard

Final workflow implemented here:
1. GUI sends .v/.sv and .sof through the Raspberry Pi API.
2. Raspberry Pi temporarily stages queued uploads inside upload_spool/queued_job_stage/. Verilog/SystemVerilog, QSF, and SOF are kept as the smallest safe temporary cache format while waiting. SOF is compressed as .sof.tmp.gz; .v/.sv and .qsf are gzip-compressed only when gzip is smaller. Finished/cancelled temp folders are deleted automatically.
3. Raspberry Pi runs a JTAG prewarm daemon so quartus_pgm -l / jtagconfig keeps the server ready before jobs are queued.
4. Raspberry Pi maps detected JTAG cables to board_catalog in config_pi_hat.json.
5. The C extractor emits signal/QSF evidence; Ollama Qwen analyzes the current job, and a manual-grounded deterministic QSF identity guard prevents hallucinated evidence from selecting the wrong board before live-slot filtering.
6. Raspberry Pi manages queue/state/status and automatic student test timers.
7. When a slot opens, Raspberry Pi activates the oldest staged SOF cache by renaming .sof.tmp or decompressing .sof.tmp.gz into an execution .sof, copies it to the Quartus server, then Quartus programs it.
8. Quartus server programs through its existing JTAG cables using quartus_pgm.
9. All programming jobs go through the smart FIFO queue.

The Raspberry Pi DOES NOT route JTAG. JTAG stays connected to the Quartus server.
"""
from __future__ import annotations

import datetime as _dt
import atexit
import gzip
import json
import os
import hashlib
import math
import re
import shlex
import socket
import secrets
import shutil
import time
import uuid
import threading
import traceback
import subprocess
import tempfile
import stat
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import paramiko
from flask import Flask, jsonify, request, Response, stream_with_context, g
from werkzeug.exceptions import RequestEntityTooLarge

try:
    from flask_cors import CORS
except Exception:
    CORS = None

try:
    from werkzeug.utils import secure_filename
except Exception:
    def secure_filename(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", name)

from fpga_board_classifier import classify_fpga_board, classifier_features
from fpga_classifier_policy import (
    build_prompt as build_grounded_fpga_prompt,
    enforce_grounding as enforce_fpga_grounding,
    ollama_json_schema as grounded_ollama_json_schema,
)
from uady_secure_store import (
    get_or_create_pi_api_key,
    set_pi_api_key,
    pi_secret_path,
    get_or_create_pi_terminal_key,
    get_quartus_ssh_key_path,
    set_quartus_ssh_key_path,
    get_pi_config_section,
    set_pi_config_section,
    pi_private_config_path,
)

try:
    from gpiozero import OutputDevice, InputDevice
except Exception:
    OutputDevice = None
    InputDevice = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config_pi_hat.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
if CORS:
    CORS(app)

CONFIG: Dict[str, Any] = {}
JTAG_CACHE: Dict[str, Any] = {"time": 0, "data": None}
JTAG_DISCOVERY_LOCK = threading.Lock()
JTAG_DISCOVERY_IN_PROGRESS = False
# AI-only inference is serialized because this 4 GB Pi runs one CPU model.
# Multiple simultaneous Ollama generations otherwise queue internally and can all time out.
OLLAMA_INFERENCE_LOCK = threading.Lock()
OLLAMA_PRELOAD_STATUS: Dict[str, Any] = {"attempted": False, "success": False}
# v4.29: GUI live JTAG/boards polling must never block Flask for SSH reads.
# Non-force reads return cache immediately and start at most one background refresh.
JTAG_ASYNC_REFRESH_THREAD = None
JTAG_ASYNC_REFRESH_LAST_TS = 0.0
JTAG_ASYNC_REFRESH_LOCK = threading.RLock()
GPIO_OUTPUTS: Dict[int, Any] = {}
GPIO_INPUTS: Dict[int, Any] = {}
QUEUE_WORKER_STARTED = False
QUEUE_WORKER_THREAD = None
AUTO_REPAIR_WORKER_STARTED = False
AUTO_REPAIR_WORKER_THREAD = None
AUTO_REPAIR_HEARTBEAT_TS = 0.0
# Parallel queue runner state. The queue worker only schedules jobs; each
# programming job runs in its own daemon thread so one stuck SSH/log command
# cannot freeze the whole queue.
QUEUE_JOB_THREADS: Dict[str, threading.Thread] = {}
QUEUE_JOB_THREADS_LOCK = threading.RLock()
# v5.06: internal dispatch lock. This is not a user repair feature; it prevents
# live /queue polling, upload finalization, and the background scheduler from
# trying to promote the same queued job at the same time.
QUEUE_DISPATCH_ONCE_LOCK = threading.RLock()
QUEUE_WORKER_HEARTBEAT_TS = 0.0
QUEUE_WORKER_WAKE_EVENT = threading.Event()
AUTO_REPAIR_WAKE_EVENT = threading.Event()
STATE_FILE_LOCK = threading.RLock()
STATE_LAST_PAYLOAD: Optional[str] = None
STATE_LAST_PRETTY: Optional[bool] = None

# v4.06: serialize direct-to-Quartus archive uploads.
# v4.05 used SERVER_ARCHIVE_LOCK in archive_submission_to_quartus_server()
# but did not define it, causing instant 500 errors on /upload_files.
SERVER_ARCHIVE_LOCK = threading.RLock()
# Prevent upload retry/cleanup races from deleting a queued stage directory while
# the controller is writing .v.tmp/.sof.tmp files into it.
QUEUE_STAGE_WRITE_LOCK = threading.RLock()

# v4.07: upload request should never wait forever on SSH/SFTP archive.
# The HTTP request only receives/stages the files, then a background archiver
# moves them to the Quartus server history folder and deletes the temporary Pi copy.
ARCHIVE_JOB_THREADS: Dict[str, threading.Thread] = {}
ARCHIVE_JOB_THREADS_LOCK = threading.RLock()

# v4.20: temp/state cleanup plus immediate queue kick.  The Pi keeps staged .v.tmp/.sof.tmp
# files only as an operational cache.  A background worker removes stale inactive
# cache folders every hour while preserving active receiving/uploading/queued/running jobs.
TEMP_STAGE_CLEANUP_WORKER_STARTED = False
TEMP_STAGE_CLEANUP_WORKER_THREAD = None

STATE_TMP_CLEANUP_WORKER_STARTED = False
STATE_TMP_CLEANUP_WORKER_THREAD = None
STATE_TMP_CLEANUP_HEARTBEAT_TS = 0.0
LAST_STATE_TMP_CLEANUP_TS = 0.0
STATE_TMP_CLEANUP_LOCK = threading.RLock()
TEMP_STAGE_CLEANUP_HEARTBEAT_TS = 0.0

# v4.45: prequeue admission should not scan/repair every active job for every
# burst request. The background queue maintenance worker already handles global repair;
# prequeue only needs an occasional quick recovery sweep so classroom bursts stay
# sub-second instead of fighting on the state-file lock.
PREQUEUE_RECOVERY_LOCK = threading.RLock()
LAST_PREQUEUE_RECOVERY_TS = 0.0


# v4.22: small server job-record writes can run in the background so the
# GUI upload path stays fast.  The large .v/.sof files remain temporary only.
SERVER_HISTORY_THREADS: Dict[str, Any] = {}
SERVER_HISTORY_THREADS_LOCK = threading.RLock()
SERVER_HISTORY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="history_record")
SERVER_HISTORY_PENDING_LIMIT = threading.BoundedSemaphore(128)
atexit.register(lambda: SERVER_HISTORY_EXECUTOR.shutdown(wait=False, cancel_futures=True))

# v4.27: background JTAG prewarm service plus blocking bash-startup prewarm.  It keeps the Quartus/JTAG server
# awake while the system is idle so the first real programming command does not
# fail once and only succeed after a requeue/retry.  It pauses during active
# programming to avoid competing with quartus_pgm.
JTAG_PREWARM_WORKER_STARTED = False
JTAG_PREWARM_WORKER_THREAD = None
JTAG_PREWARM_HEARTBEAT_TS = 0.0
JTAG_PREWARM_LAST_RESULT: Dict[str, Any] = {}
JTAG_PREWARM_LOCK = threading.RLock()


# v3.92 load-shedding / throttling state.
# These counters protect the Raspberry Pi Flask server from being flooded by
# dozens of GUI clients, upload requests, and stress-test clients at once.
TRAFFIC_LOCK = threading.RLock()
TRAFFIC_CONDITION = threading.Condition(TRAFFIC_LOCK)
ACTIVE_HTTP_REQUESTS: Dict[str, int] = {"total": 0, "read": 0, "write": 0, "upload": 0, "ai": 0}
ACTIVE_STREAM_CLIENTS = 0
REQUEST_STATS: Dict[str, Any] = {
    "started": 0,
    "finished": 0,
    "rejected": 0,
    "waited": 0,
    "max_active_total": 0,
    "max_active_streams": 0,
    "last_rejection_at": "",
    "last_rejection_reason": "",
}

# v4.30 classroom scaling: many students can keep the queue window open at once.
# A single broadcaster thread computes /queue snapshots, then every SSE client
# reuses the same payload instead of each student forcing a state-file read.
QUEUE_STREAM_BROADCAST_LOCK = threading.RLock()
QUEUE_STREAM_BROADCAST_THREAD = None
QUEUE_STREAM_BROADCAST_STARTED = False
QUEUE_STREAM_BROADCAST = {
    "payload": "",
    "updated_ts": 0.0,
    "updated_at": "",
    "hash": "",
    "sequence": 0,
    "error": "",
}

# v4.44 low-latency sync: state changes wake the queue broadcaster immediately.
# This avoids waiting for the next polling/cache tick after programming finishes.
QUEUE_STREAM_WAKE_EVENT = threading.Event()
QUEUE_STATE_CHANGE_SEQUENCE = 0


def notify_realtime_sync(reason: str = "state_changed") -> None:
    """Wake queue SSE broadcaster/clients after a real queue state change.

    The goal is millisecond-level GUI notification after important transitions
    such as uploading -> queued -> running -> testing. Nanosecond synchronization
    is not physically possible over Flask/SSH/Windows/Tkinter/JTAG, but this
    removes the avoidable software polling delay.
    """
    global QUEUE_STATE_CHANGE_SEQUENCE
    try:
        now_ns = time.time_ns()
        with QUEUE_STREAM_BROADCAST_LOCK:
            QUEUE_STATE_CHANGE_SEQUENCE += 1
            QUEUE_STREAM_BROADCAST["last_wake_reason"] = str(reason or "state_changed")[:120]
            QUEUE_STREAM_BROADCAST["last_wake_ts"] = time.time()
            QUEUE_STREAM_BROADCAST["last_wake_ns"] = now_ns
            QUEUE_STREAM_BROADCAST["state_change_sequence"] = QUEUE_STATE_CHANGE_SEQUENCE
        QUEUE_STREAM_WAKE_EVENT.set()
    except Exception:
        pass

# v4.03 dynamic scalability state.
# These values are discovered at startup and refreshed from live JTAG/system
# readings. They replace fixed traffic caps with limits derived from detected
# board slots, CPU count, RAM, and current queue pressure.
RUNTIME_CONFIG_LOCK = threading.RLock()
RUNTIME_CONFIG: Dict[str, Any] = {
    "initialized": False,
    "last_updated_at": "",
    "last_updated_ts": 0.0,
    "source": "not_initialized",
    "resources": {},
    "slot_summary": {},
    "limits": {},
    "warnings": [],
}


# =========================
# Config / state helpers
# =========================
def load_config() -> Dict[str, Any]:
    global CONFIG
    if not CONFIG:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            CONFIG = json.load(f)
        # Security migration: never keep the Pi API key in config_pi_hat.json.
        # Existing deployments are migrated once into Raspberry-side secret storage.
        legacy_key = str(CONFIG.get("api_key", "") or "").strip()
        dirty_config = False
        if legacy_key:
            try:
                set_pi_api_key(legacy_key)
                CONFIG.pop("api_key", None)
                dirty_config = True
                print(f"[SECURITY] Migrated Pi API key out of config_pi_hat.json into {pi_secret_path()}")
            except Exception as e:
                print(f"[WARN] Could not migrate Pi API key out of config_pi_hat.json: {e}")
        # Security migration: deployment-specific Quartus server host/user/path
        # settings and the SSH key path belong in protected Pi storage, not in
        # the shipped config_pi_hat.json. Existing deployments migrate once.
        try:
            qs = CONFIG.get("quartus_server", {}) or {}
            if qs:
                legacy_ssh_key = str(qs.get("ssh_key", "") or "").strip()
                if legacy_ssh_key:
                    set_quartus_ssh_key_path(legacy_ssh_key)
                    qs.pop("ssh_key", None)
                stored_qs = get_pi_config_section("quartus_server", {})
                merged_qs = dict(stored_qs or {})
                merged_qs.update({k: v for k, v in qs.items() if v is not None})
                if merged_qs:
                    set_pi_config_section("quartus_server", merged_qs)
                    print(f"[SECURITY] Migrated Quartus server settings out of config_pi_hat.json into {pi_private_config_path()}")
                CONFIG.pop("quartus_server", None)
                dirty_config = True
        except Exception as e:
            print(f"[WARN] Could not migrate Quartus server settings out of config_pi_hat.json: {e}")
        private_qs = get_pi_config_section("quartus_server", {})
        if private_qs:
            CONFIG["quartus_server"] = dict(private_qs)
        # Keep deployment-specific server history base path out of shipped config.
        try:
            hist = CONFIG.get("server_history", {}) or {}
            legacy_base_dir = str(hist.get("base_dir", "") or "").strip()
            if legacy_base_dir:
                private_hist = get_pi_config_section("server_history", {})
                private_hist["base_dir"] = legacy_base_dir
                set_pi_config_section("server_history", private_hist)
                hist.pop("base_dir", None)
                CONFIG["server_history"] = hist
                dirty_config = True
                print(f"[SECURITY] Migrated server history base_dir out of config_pi_hat.json into {pi_private_config_path()}")
            private_hist = get_pi_config_section("server_history", {})
            merged_hist = dict(CONFIG.get("server_history", {}) or {})
            if private_hist:
                merged_hist.update(private_hist)
            # v4.53: the lab history directory is /home/lab4p0/History_of_jobs.
            # Older secure builds defaulted to /home/<quartus_user>/History_of_jobs;
            # migrate that old derived value automatically so existing Pis start
            # writing records to the shared field directory.
            try:
                qs_user = str((get_pi_config_section("quartus_server", {}) or {}).get("user") or "").strip()
                current_base = str(merged_hist.get("base_dir", "") or "").strip().rstrip("/")
                old_derived = f"/home/{qs_user}/History_of_jobs" if qs_user else ""
                legacy_wrong_shared = "/home/History_of_jobs"
                # Migrate the accidental old default (/home/History_of_jobs) and
                # older derived defaults to the actual server folder used by the lab.
                if (not current_base) or current_base == legacy_wrong_shared or (old_derived and current_base == old_derived):
                    merged_hist["base_dir"] = "/home/lab4p0/History_of_jobs"
                    private_hist = dict(private_hist or {})
                    private_hist["base_dir"] = merged_hist["base_dir"]
                    set_pi_config_section("server_history", private_hist)
                    print(f"[INFO] Server history base_dir set in private Pi storage: {merged_hist['base_dir']}")
            except Exception as e:
                print(f"[WARN] Could not set server history base_dir: {e}")
            merged_hist.setdefault("enabled", True)
            merged_hist.setdefault("record_format", "txt")
            merged_hist.setdefault("one_record_per_job", True)
            # With one stable file per Job ID, queue-accept can safely create the
            # first record and later lifecycle events update the same file.
            # This makes the History_of_jobs folder update immediately without
            # creating duplicate records.
            merged_hist.setdefault("record_on_queue_accept", True)
            CONFIG["server_history"] = merged_hist
        except Exception as e:
            print(f"[WARN] Could not migrate server history settings out of config_pi_hat.json: {e}")
        # v5.0: load declarative board profiles from an external JSON file.
        # This removes board-family definitions from controller code while
        # preserving the existing board_catalog interface used by the GUI/API.
        try:
            profile_cfg = CONFIG.get("hardware_profiles", {}) or {}
            profile_name = str(profile_cfg.get("source") or "board_profiles.json")
            profile_path = (BASE_DIR / profile_name).resolve()
            if profile_path.is_file():
                with open(profile_path, "r", encoding="utf-8") as pf:
                    profile_doc = json.load(pf)
                generated_catalog = {}
                for profile in profile_doc.get("profiles", []):
                    if not isinstance(profile, dict):
                        continue
                    display_name = str(profile.get("display_name") or "").strip()
                    if not display_name:
                        continue
                    gpio = profile.get("gpio", {}) or {}
                    generated_catalog[display_name] = {
                        "enabled": bool(profile.get("enabled", True)),
                        "quartus_family": str(profile.get("quartus_family") or "standard"),
                        "jtag_cable": "",
                        "jtag_device_index": str(profile.get("default_device_index") or ""),
                        "features": list(profile.get("features") or []),
                        "jtag_aliases": list(profile.get("cable_patterns") or []),
                        "device_patterns": list(profile.get("device_patterns") or []),
                        "power_relay_pin": gpio.get("power_relay_pin"),
                        "reset_pin": gpio.get("reset_pin"),
                        "status_pin": gpio.get("status_pin"),
                        "busy_led_pin": gpio.get("busy_led_pin"),
                        "ready_led_pin": gpio.get("ready_led_pin"),
                        "active_high": bool(gpio.get("active_high", True)),
                    }
                if generated_catalog:
                    CONFIG["board_catalog"] = generated_catalog
                    CONFIG["board_profiles_loaded_from"] = str(profile_path)
        except Exception as e:
            print(f"[WARN] Could not load external board profiles: {e}")
        if dirty_config:
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(CONFIG, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[WARN] Could not write sanitized config_pi_hat.json: {e}")
    return CONFIG


def controller_runtime_limits() -> Dict[str, int]:
    """Memory/disk-aware limits for Raspberry Pi classroom stability."""
    cfg = load_config()
    upload_cfg = cfg.get("upload_limits", {}) or {}
    mem_total_mb = int(system_resource_snapshot().get("memory_total_mb", 0) or 0)
    default_upload_mb = 64 if mem_total_mb and mem_total_mb <= 4608 else 256

    def _as_int(value: Any, default: int) -> int:
        try:
            return max(1, int(value or default))
        except Exception:
            return default

    return {
        "max_upload_bytes": _as_int(upload_cfg.get("max_upload_mb"), default_upload_mb) * 1024 * 1024,
        "max_inline_verilog_bytes": _as_int(upload_cfg.get("max_inline_verilog_kb"), 256) * 1024,
        "max_inline_qsf_bytes": _as_int(upload_cfg.get("max_inline_qsf_kb"), 128) * 1024,
        "max_classifier_verilog_bytes": _as_int(upload_cfg.get("max_classifier_verilog_kb"), 1024) * 1024,
        "max_classifier_qsf_bytes": _as_int(upload_cfg.get("max_classifier_qsf_kb"), 256) * 1024,
    }


def limited_text_file_read(path: Path, max_bytes: int) -> str:
    """Read at most ``max_bytes`` bytes from a text-like file."""
    try:
        limit = max(1, int(max_bytes or 0))
    except Exception:
        limit = 1024 * 1024
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
        clipped = len(raw) > limit
        raw = raw[:limit]
        text = raw.decode("utf-8", errors="ignore")
        if clipped:
            text += "\n/* truncated_for_pi_memory_safety */\n"
        return text
    except Exception:
        return ""


def save_config(cfg: Dict[str, Any]) -> None:
    global CONFIG
    CONFIG = cfg
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _read_meminfo() -> Dict[str, int]:
    """Return Linux memory info in bytes without requiring psutil."""
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    out[parts[0].rstrip(":")] = int(parts[1]) * 1024
    except Exception:
        pass
    return out


def system_resource_snapshot() -> Dict[str, Any]:
    """Detect CPU/RAM/disk at runtime so queue and traffic limits are not static."""
    mem = _read_meminfo()
    disk = shutil.disk_usage(str(BASE_DIR))
    cpu_count = int(os.cpu_count() or 1)
    total_mem = int(mem.get("MemTotal", 0) or 0)
    available_mem = int(mem.get("MemAvailable", mem.get("MemFree", 0)) or 0)
    return {
        "hostname": socket.gethostname(),
        "cpu_count": cpu_count,
        "memory_total_bytes": total_mem,
        "memory_available_bytes": available_mem,
        "memory_total_mb": int(total_mem / (1024 * 1024)) if total_mem else 0,
        "memory_available_mb": int(available_mem / (1024 * 1024)) if available_mem else 0,
        "disk_total_bytes": int(disk.total),
        "disk_free_bytes": int(disk.free),
        "disk_free_mb": int(disk.free / (1024 * 1024)),
    }


def summarize_jtag_topology(jtag_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Map live quartus_pgm -l output to board families configured in board_catalog."""
    cfg = load_config()
    catalog = cfg.get("board_catalog", {}) or {}
    if not jtag_data:
        jtag_data = JTAG_CACHE.get("data") or {}
    cables = list((jtag_data or {}).get("cables", []) or [])
    per_family: Dict[str, int] = {}
    unknown_cables: List[str] = []
    for cable in cables:
        try:
            board = infer_board_type_from_jtag_cable(cable) if "infer_board_type_from_jtag_cable" in globals() else "Unknown"
        except Exception:
            board = "Unknown"
        if board and board != "Unknown":
            per_family[board] = per_family.get(board, 0) + 1
        else:
            unknown_cables.append(cable)
    enabled_families = [name for name, b in catalog.items() if bool((b or {}).get("enabled", True))]
    return {
        "detected_slot_count": len(cables),
        "detected_known_slot_count": sum(per_family.values()),
        "detected_board_families": sorted(per_family.keys()),
        "detected_slots_by_family": per_family,
        "unknown_cables": unknown_cables,
        "unknown_cable_count": len(unknown_cables),
        "catalog_board_family_count": len(catalog),
        "catalog_enabled_board_family_count": len(enabled_families),
        "catalog_enabled_board_families": sorted(enabled_families),
        "jtag_source_timestamp": (jtag_data or {}).get("timestamp", ""),
        "jtag_cache_used": bool((jtag_data or {}).get("cache_used", False)),
    }


def compute_dynamic_runtime_limits(cfg: Optional[Dict[str, Any]] = None, jtag_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute request/queue capacities from real hardware and system resources.

    The values are not hardcoded caps. Admins tune only multipliers/minimums in
    config_pi_hat.json; adding detected JTAG slots automatically changes the
    effective runtime limits.
    """
    cfg = cfg or load_config()
    ds = cfg.get("dynamic_scaling", {}) or {}
    resources = system_resource_snapshot()
    slots = summarize_jtag_topology(jtag_data)
    cpu = max(1, int(resources.get("cpu_count", 1) or 1))
    mem_avail_mb = max(1, int(resources.get("memory_available_mb", 0) or 0))
    live_slots = int(slots.get("detected_known_slot_count") or slots.get("detected_slot_count") or 0)
    enabled_families = int(slots.get("catalog_enabled_board_family_count") or 1)
    scale_slots = max(1, live_slots, enabled_families)

    def dint(name: str, default: int) -> int:
        return max(0, _auto_int(ds.get(name, default), default))

    total = max(
        dint("min_total_http_requests", 16),
        scale_slots * dint("http_requests_per_slot", 8) + cpu * dint("http_requests_per_cpu", 4),
    )
    read = max(dint("min_read_requests", 8), scale_slots * dint("read_requests_per_slot", 4) + cpu * dint("read_requests_per_cpu", 2))
    write = max(dint("min_write_requests", 4), scale_slots * dint("write_requests_per_slot", 3) + cpu * dint("write_requests_per_cpu", 1))
    upload = max(dint("min_upload_requests", 1), scale_slots * dint("upload_requests_per_slot", 1))
    ai = max(dint("min_ai_requests", 1), scale_slots * dint("ai_requests_per_slot", 1))
    streams = max(dint("min_stream_clients", 8), scale_slots * dint("stream_clients_per_slot", 4) + cpu * dint("stream_clients_per_cpu", 1))

    max_total_cap = dint("max_total_http_requests_cap", 0)
    if max_total_cap > 0:
        total = min(total, max_total_cap)
    memory_queue_jobs_per_gb = max(1, dint("queue_jobs_per_available_gb", 2000))
    queue_soft_capacity = max(dint("min_queue_soft_capacity", 500), int((mem_avail_mb / 1024.0) * memory_queue_jobs_per_gb))
    return {
        "enabled": _truthy(ds.get("enabled"), False),
        "scale_basis": "live_jtag_slots_cpu_ram",
        "detected_slot_count": int(slots.get("detected_slot_count", 0) or 0),
        "detected_known_slot_count": int(slots.get("detected_known_slot_count", 0) or 0),
        "scale_slot_count": scale_slots,
        "cpu_count": cpu,
        "memory_available_mb": mem_avail_mb,
        "max_total_http_requests": int(total),
        "max_read_requests": int(read),
        "max_write_requests": int(write),
        "max_upload_requests": int(upload),
        "max_ai_requests": int(ai),
        "max_stream_clients": int(streams),
        "request_wait_seconds": float(ds.get("request_wait_seconds", (cfg.get("traffic_control", {}) or {}).get("request_wait_seconds", 0.25)) or 0.25),
        "queue_soft_capacity_jobs": int(queue_soft_capacity),
        "queue_capacity_mode": "fluid_dynamic_array_soft_warning_only",
    }


def adaptive_runtime_config(refresh: bool = False, jtag_data: Optional[Dict[str, Any]] = None, source: str = "runtime") -> Dict[str, Any]:
    """Return cached dynamic config, refreshing when requested or expired."""
    cfg = load_config()
    ds = cfg.get("dynamic_scaling", {}) or {}
    if not _truthy(ds.get("enabled"), False):
        return {"initialized": False, "enabled": False, "limits": {}, "resources": {}, "slot_summary": {}, "warnings": []}
    ttl = max(1, _auto_int(ds.get("refresh_seconds", 10), 10))
    now_ts = time.time()
    with RUNTIME_CONFIG_LOCK:
        age = now_ts - float(RUNTIME_CONFIG.get("last_updated_ts", 0) or 0)
        if (not refresh) and RUNTIME_CONFIG.get("initialized") and age < ttl:
            return dict(RUNTIME_CONFIG)
        resources = system_resource_snapshot()
        slots = summarize_jtag_topology(jtag_data)
        limits = compute_dynamic_runtime_limits(cfg, jtag_data)
        warnings = []
        if slots.get("unknown_cable_count"):
            warnings.append("Unknown JTAG cables detected. Add aliases to board_catalog before allowing programming on those boards.")
        if resources.get("memory_available_mb", 0) and resources.get("memory_available_mb", 0) < _auto_int(ds.get("low_memory_warning_mb", 256), 256):
            warnings.append("Low available memory; queue remains dynamic but admin should reduce traffic or close other services.")
        RUNTIME_CONFIG.update({
            "initialized": True,
            "enabled": True,
            "last_updated_at": now_iso() if "now_iso" in globals() else "",
            "last_updated_ts": now_ts,
            "source": source,
            "resources": resources,
            "slot_summary": slots,
            "limits": limits,
            "warnings": warnings,
        })
        return dict(RUNTIME_CONFIG)


def initialize_dynamic_runtime_config(force_jtag: bool = True) -> Dict[str, Any]:
    """Startup sweep: scan JTAG and derive all adaptive limits once."""
    cfg = load_config()
    if not _truthy((cfg.get("dynamic_scaling", {}) or {}).get("enabled"), False):
        return adaptive_runtime_config(refresh=True, source="disabled")
    jtag_data: Dict[str, Any] = {}
    if force_jtag:
        try:
            jtag_data = discover_jtag(force=True)
        except Exception as e:
            jtag_data = {"success": False, "cables": [], "errors": [{"error": str(e)}], "timestamp": now_iso() if "now_iso" in globals() else ""}
    data = adaptive_runtime_config(refresh=True, jtag_data=jtag_data, source="startup_environment_sweep")
    return data


def api_auth_required() -> bool:
    return bool(current_pi_api_key())


def request_api_key() -> str:
    return (
        request.headers.get("X-PI-KEY")
        or request.headers.get("X-API-Key")
        or request.args.get("api_key")
        or ""
    ).strip()


def current_pi_api_key() -> str:
    """Return the Raspberry-side API key from env or protected Pi storage.

    The key is auto-generated on first controller start and is intentionally not
    stored in config_pi_hat.json or this Python source.
    """
    return get_or_create_pi_api_key().strip()


def current_terminal_access_key() -> str:
    """Return the Raspberry-side GUI terminal access key.

    This key is auto-generated on the Pi and shared by all GUI copies. It is not
    embedded in gui.py and is not stored in config.ini/config_pi_hat.json.
    """
    return get_or_create_pi_terminal_key().strip()


def verify_terminal_access_key(provided: Any) -> bool:
    expected = current_terminal_access_key()
    provided = str(provided or "").strip()
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


def hash_client_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled", "auto", "dynamic")


def _auto_int(value: Any, fallback: int) -> int:
    """Parse integer config values while accepting auto/dynamic placeholders."""
    if isinstance(value, str) and value.strip().lower() in ("auto", "dynamic", "adaptive", ""):
        return int(fallback)
    try:
        return int(value)
    except Exception:
        return int(fallback)


def traffic_cfg() -> Dict[str, Any]:
    cfg = load_config()
    tc = cfg.get("traffic_control", {}) or {}
    dynamic_enabled = _truthy((cfg.get("dynamic_scaling", {}) or {}).get("enabled"), False) and _truthy(tc.get("use_dynamic_limits", True), True)
    dynamic_limits = adaptive_runtime_config(refresh=False).get("limits", {}) if dynamic_enabled and "adaptive_runtime_config" in globals() else {}

    def pick(name: str, fallback: int) -> int:
        return max(1, _auto_int(tc.get(name, "auto" if dynamic_enabled else fallback), int(dynamic_limits.get(name, fallback))))

    return {
        "enabled": bool(tc.get("enabled", True)),
        "dynamic_limits_enabled": bool(dynamic_enabled),
        "request_wait_seconds": float(tc.get("request_wait_seconds", dynamic_limits.get("request_wait_seconds", 2.0)) or 0.0),
        "max_total_http_requests": pick("max_total_http_requests", 16),
        "max_read_requests": pick("max_read_requests", 8),
        "max_write_requests": pick("max_write_requests", 4),
        "max_upload_requests": pick("max_upload_requests", 2),
        "max_ai_requests": pick("max_ai_requests", 2),
        "max_stream_clients": pick("max_stream_clients", 12),
        "slow_request_log_seconds": float(tc.get("slow_request_log_seconds", 3.0) or 3.0),
        "rate_limit_response_code": int(tc.get("rate_limit_response_code", 429) or 429),
        "dynamic_runtime": dynamic_limits,
    }

def route_limit_class(path: str, method: str) -> str:
    path = str(path or "")
    method = str(method or "GET").upper()
    if path.startswith("/stream/"):
        return "stream"
    if "upload_files" in path or path.endswith("/queue/deploy"):
        return "upload"
    if path.endswith("/queue/prequeue_upload"):
        return "write"
    if path.startswith("/ai/"):
        return "ai"
    if method == "GET":
        return "read"
    return "write"


def request_limit_for_class(class_name: str, tc: Optional[Dict[str, Any]] = None) -> int:
    tc = tc or traffic_cfg()
    if class_name == "upload":
        return int(tc.get("max_upload_requests", 2))
    if class_name == "ai":
        return int(tc.get("max_ai_requests", 2))
    if class_name == "write":
        return int(tc.get("max_write_requests", 4))
    return int(tc.get("max_read_requests", 8))


def traffic_snapshot() -> Dict[str, Any]:
    with TRAFFIC_LOCK:
        return {
            "active_http_requests": dict(ACTIVE_HTTP_REQUESTS),
            "active_stream_clients": int(ACTIVE_STREAM_CLIENTS),
            "stats": dict(REQUEST_STATS),
        }


def acquire_http_request_slot(class_name: str) -> Tuple[bool, Dict[str, Any]]:
    tc = traffic_cfg()
    if not tc.get("enabled", True):
        return True, {"traffic_control_enabled": False}

    deadline = time.time() + max(0.0, float(tc.get("request_wait_seconds", 2.0)))
    waited = False
    with TRAFFIC_CONDITION:
        while True:
            total = int(ACTIVE_HTTP_REQUESTS.get("total", 0))
            class_count = int(ACTIVE_HTTP_REQUESTS.get(class_name, 0))
            total_limit = max(1, int(tc.get("max_total_http_requests", 16)))
            class_limit = max(1, request_limit_for_class(class_name, tc))
            if total < total_limit and class_count < class_limit:
                ACTIVE_HTTP_REQUESTS["total"] = total + 1
                ACTIVE_HTTP_REQUESTS[class_name] = class_count + 1
                REQUEST_STATS["started"] = int(REQUEST_STATS.get("started", 0)) + 1
                if waited:
                    REQUEST_STATS["waited"] = int(REQUEST_STATS.get("waited", 0)) + 1
                REQUEST_STATS["max_active_total"] = max(int(REQUEST_STATS.get("max_active_total", 0)), int(ACTIVE_HTTP_REQUESTS.get("total", 0)))
                return True, traffic_snapshot()

            remaining = deadline - time.time()
            if remaining <= 0:
                REQUEST_STATS["rejected"] = int(REQUEST_STATS.get("rejected", 0)) + 1
                REQUEST_STATS["last_rejection_at"] = now_iso() if "now_iso" in globals() else ""
                REQUEST_STATS["last_rejection_reason"] = f"{class_name} concurrency limit reached"
                return False, traffic_snapshot()

            waited = True
            TRAFFIC_CONDITION.wait(timeout=min(remaining, 0.5))


def release_http_request_slot(class_name: str) -> None:
    if not class_name or class_name == "stream":
        return
    with TRAFFIC_CONDITION:
        ACTIVE_HTTP_REQUESTS["total"] = max(0, int(ACTIVE_HTTP_REQUESTS.get("total", 0)) - 1)
        ACTIVE_HTTP_REQUESTS[class_name] = max(0, int(ACTIVE_HTTP_REQUESTS.get(class_name, 0)) - 1)
        REQUEST_STATS["finished"] = int(REQUEST_STATS.get("finished", 0)) + 1
        TRAFFIC_CONDITION.notify_all()


def acquire_stream_client_slot() -> Tuple[bool, Dict[str, Any]]:
    global ACTIVE_STREAM_CLIENTS
    tc = traffic_cfg()
    limit = max(1, int(tc.get("max_stream_clients", 12)))
    with TRAFFIC_LOCK:
        if ACTIVE_STREAM_CLIENTS >= limit:
            REQUEST_STATS["rejected"] = int(REQUEST_STATS.get("rejected", 0)) + 1
            REQUEST_STATS["last_rejection_at"] = now_iso() if "now_iso" in globals() else ""
            REQUEST_STATS["last_rejection_reason"] = "stream client limit reached"
            return False, traffic_snapshot()
        ACTIVE_STREAM_CLIENTS += 1
        REQUEST_STATS["max_active_streams"] = max(int(REQUEST_STATS.get("max_active_streams", 0)), ACTIVE_STREAM_CLIENTS)
        return True, traffic_snapshot()


def release_stream_client_slot() -> None:
    global ACTIVE_STREAM_CLIENTS
    with TRAFFIC_LOCK:
        ACTIVE_STREAM_CLIENTS = max(0, ACTIVE_STREAM_CLIENTS - 1)


@app.before_request
def require_pi_api_key_and_throttle():
    # If api_key is blank in config_pi_hat.json, auth is disabled.
    g.uady_request_started_ts = time.time()
    g.uady_request_class = ""
    g.uady_request_slot_acquired = False

    expected = current_pi_api_key()
    if request.method == "OPTIONS":
        return None
    if expected:
        provided = request_api_key()
        if provided != expected:
            return response(fail("Unauthorized: invalid or missing Pi API key"), 401)

    class_name = route_limit_class(request.path, request.method)
    g.uady_request_class = class_name
    if class_name == "stream":
        # Streams are limited inside the generator so the slot remains held until
        # the SSE connection actually disconnects.
        return None

    ok_slot, snap = acquire_http_request_slot(class_name)
    if not ok_slot:
        code = int(traffic_cfg().get("rate_limit_response_code", 429))
        resp, status = response(fail(
            "Server busy: request was safely throttled instead of overloading the Pi. Retry shortly.",
            retry_after_seconds=1,
            request_class=class_name,
            traffic=traffic_snapshot(),
        ), code)
        try:
            resp.headers["Retry-After"] = "1"
        except Exception:
            pass
        return resp, status
    g.uady_request_slot_acquired = True
    return None


@app.after_request
def release_request_slot_after_response(resp):
    try:
        class_name = getattr(g, "uady_request_class", "")
        acquired = bool(getattr(g, "uady_request_slot_acquired", False))
        if acquired:
            release_http_request_slot(class_name)
            g.uady_request_slot_acquired = False
        try:
            elapsed = time.time() - float(getattr(g, "uady_request_started_ts", time.time()) or time.time())
            slow_limit = float(traffic_cfg().get("slow_request_log_seconds", 3.0) or 3.0)
            if elapsed >= slow_limit:
                add_history("slow_http_request", "", {"path": request.path, "method": request.method, "seconds": round(elapsed, 3), "class": class_name})
            # v4.44-hotfix: Werkzeug access logs only show "POST ... 400".
            # For upload/prequeue failures, also print the JSON body so the next
            # terminal screenshot shows the exact reason (missing sof_file, empty
            # file, wrong extension, wrong job state, etc.).
            if int(getattr(resp, "status_code", 200) or 200) >= 400 and ("upload_files" in request.path or "prequeue_upload" in request.path or request.path.endswith("/queue/deploy")):
                try:
                    body = resp.get_data(as_text=True)[:2000]
                except Exception:
                    body = ""
                try:
                    print("[HTTP ERROR]", request.method, request.path, "status=", resp.status_code,
                          "files=", list(request.files.keys()), "form_keys=", list(request.form.keys()),
                          "response=", body, flush=True)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass
    return resp


@app.teardown_request
def release_request_slot_on_exception(exc):
    try:
        if bool(getattr(g, "uady_request_slot_acquired", False)):
            release_http_request_slot(getattr(g, "uady_request_class", ""))
            g.uady_request_slot_acquired = False
    except Exception:
        pass


def state_path() -> Path:
    return BASE_DIR / "board_state.json"


def state_temp_dir() -> Path:
    """Folder for temporary board_state writes.

    v4.21 keeps board_state.json.*.tmp files out of the main Raspberry Pi
    controller folder.  Temporary state files are written under upload_spool/state_tmp
    and atomically moved into board_state.json when complete.  This keeps the main
    folder clean even during heavy queue traffic.
    """
    try:
        storage = load_config().get("state_storage", {}) or {}
        raw = str(storage.get("temp_dir", "upload_spool/state_tmp") or "upload_spool/state_tmp").strip()
    except Exception:
        raw = "upload_spool/state_tmp"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate



def cleanup_orphan_board_state_temp_files(max_age_seconds: int = 300, reason: str = "state_tmp_cleanup") -> Dict[str, Any]:
    """Delete leftover board_state.json.*.tmp files.

    v4.20 fix:
    A burst of queue updates can leave visible board_state.json.<pid>.<thread>.<time>.tmp
    files if the controller is killed/restarted or if a previous writer died mid-save.
    Those files are not real queue state. Keeping hundreds of them slows file browsing
    and can make the Pi look full. This cleanup never touches board_state.json itself
    and only removes old orphan temp files.
    """
    deleted = []
    errors = []
    try:
        base = state_path()
        now_ts = time.time()
        age = max(0, int(max_age_seconds or 0))
        patterns = [f"{base.name}.*.tmp", f".{base.name}.*.tmp"]
        # v4.21: clean both the old main folder location and the new upload_spool/state_tmp location.
        folders = []
        for folder in (base.parent, state_temp_dir()):
            try:
                resolved = folder.resolve()
                if resolved not in [f.resolve() for f in folders]:
                    folders.append(folder)
            except Exception:
                folders.append(folder)
        seen = set()
        for folder in folders:
            for pattern in patterns:
                for p in folder.glob(pattern):
                    try:
                        if not p.is_file():
                            continue
                        if p.resolve() == base.resolve():
                            continue
                        sp = str(p)
                        if sp in seen:
                            continue
                        seen.add(sp)
                        # Do not delete a temp file another thread may still be writing.
                        mtime = p.stat().st_mtime
                        if (now_ts - mtime) < age:
                            continue
                        p.unlink()
                        deleted.append(str(p.relative_to(BASE_DIR)) if _safe_inside(BASE_DIR, p) else p.name)
                    except Exception as e:
                        errors.append({"path": str(p), "error": str(e)})
    except Exception as e:
        errors.append({"cleanup_error": str(e)})
    return ok(reason=reason, deleted_count=len(deleted), deleted=deleted[-50:], errors=errors[-20:])


def maybe_cleanup_orphan_state_tmp_files(reason: str = "rate_limited_state_tmp_cleanup") -> None:
    """Rate-limited state tmp cleanup used by save/queue workers."""
    global LAST_STATE_TMP_CLEANUP_TS
    try:
        cfg = load_config().get("state_storage", {}) or {}
        if not bool(cfg.get("cleanup_orphan_temp_files", True)):
            return
        interval = int(cfg.get("temp_cleanup_interval_seconds", 60) or 60)
        max_age = int(cfg.get("temp_cleanup_max_age_seconds", 300) or 300)
        now_ts = time.time()
        if now_ts - float(LAST_STATE_TMP_CLEANUP_TS or 0) < max(5, interval):
            return
        with STATE_TMP_CLEANUP_LOCK:
            if now_ts - float(LAST_STATE_TMP_CLEANUP_TS or 0) < max(5, interval):
                return
            LAST_STATE_TMP_CLEANUP_TS = now_ts
        cleanup_orphan_board_state_temp_files(max_age, reason)
    except Exception:
        pass

def default_state() -> Dict[str, Any]:
    return {
        "daily_date": today_string() if "today_string" in globals() else "",
        "locks": {},
        "history": [],
        "queue": [],
        "jobs": {},
        "recent_jobs": [],
        "current_job": None,
        "current_jobs": [],
        "jtag_usage": {},
        "disabled_jtag": {},
        "queue_plan": {},
        "slot_clear_events": [],
        "teacher_override_events": [],
    }


def load_state() -> Dict[str, Any]:
    """
    Thread-safe state load.

    If a previous write crashed and left corrupt JSON, keep a backup and return
    a clean default state instead of crashing /queue or /boards.
    """
    p = state_path()
    with STATE_FILE_LOCK:
        if not p.exists():
            return default_state()
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default_state()
            # Ensure keys added in newer versions exist even when using an older board_state.json.
            base = default_state()
            for k, v in base.items():
                data.setdefault(k, v)
            try:
                global STATE_LAST_PAYLOAD, STATE_LAST_PRETTY
                storage = load_config().get("state_storage", {}) or {}
                pretty = bool(storage.get("pretty_json", False))
                if pretty:
                    STATE_LAST_PAYLOAD = json.dumps(data, indent=2, ensure_ascii=False)
                else:
                    STATE_LAST_PAYLOAD = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                STATE_LAST_PRETTY = pretty
            except Exception:
                pass
            return data
        except Exception:
            try:
                corrupt = p.with_suffix(f".corrupt_{int(time.time())}.json")
                p.replace(corrupt)
            except Exception:
                pass
            return default_state()


def _state_json_payload(state: Dict[str, Any], pretty: bool) -> str:
    if pretty:
        return json.dumps(state, indent=2, ensure_ascii=False)
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def save_state(state: Dict[str, Any]) -> None:
    """
    Thread-safe atomic state save.

    v3.94 latency optimization:
    - compact JSON by default to reduce file size and serialization time
    - optional fsync for maximum durability, disabled by default for low-latency queue writes
    - still uses unique tmp file + atomic replace, so partial/corrupt state writes are avoided

    v4.34 optimization:
    - skip the atomic replace when the serialized state did not change. Queue
      repair/scheduler cycles often call save_state defensively; avoiding
      identical JSON rewrites reduces SD-card write amplification without
      changing the saved content.
    """
    global STATE_LAST_PAYLOAD, STATE_LAST_PRETTY
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(state, dict):
        state = default_state()

    storage = load_config().get("state_storage", {}) or {}
    pretty = bool(storage.get("pretty_json", False))
    fsync_enabled = bool(storage.get("fsync_enabled", False))
    payload = _state_json_payload(state, pretty)

    # v4.21: keep temporary state-save files out of BASE_DIR.
    tmp = state_temp_dir() / f"{p.name}.{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000000)}.tmp"
    wrote_state = False
    with STATE_FILE_LOCK:
        if STATE_LAST_PRETTY == pretty and STATE_LAST_PAYLOAD == payload and p.exists():
            return
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                if fsync_enabled:
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
            tmp.replace(p)
            STATE_LAST_PAYLOAD = payload
            STATE_LAST_PRETTY = pretty
            wrote_state = True
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
    if wrote_state:
        notify_realtime_sync("state_saved")
    # Keep the Raspberry Pi folder clean if an older controller crash left
    # board_state.json.*.tmp files behind. Rate-limited so normal saves stay fast.
    maybe_cleanup_orphan_state_tmp_files("after_state_save")



def update_state_atomic(mutator, default_result=None):
    """Run a read-modify-write state update under one lock.

    load_state() and save_state() are individually thread-safe, but heavy stress tests
    exposed a lost-update race when many HTTP threads did:
        load -> modify -> save
    at the same time. This helper keeps the full transaction atomic.
    """
    with STATE_FILE_LOCK:
        state = load_state()
        result = mutator(state)
        save_state(state)
        return default_result if result is None else result



def save_state_preserving_concurrent_jobs(state: Dict[str, Any]) -> None:
    """Save a scheduler/planner state without losing jobs created concurrently.

    v3.96 fix:
    Some background queue-worker cycles still used load -> modify -> save. If a GUI
    uploaded a job between the worker's load and save, the stale worker save could
    overwrite the newly-created job, making it disappear from the queue table.

    This function is for background repair/planning saves only. It reloads the latest
    state under the same file lock and preserves jobs/queue entries that were created
    after this worker snapshot was loaded.
    """
    with STATE_FILE_LOCK:
        latest = load_state()
        state_jobs = state.setdefault("jobs", {})
        latest_jobs = latest.get("jobs", {}) or {}

        # Preserve jobs that exist in latest but are missing from this scheduler snapshot.
        for jid, latest_job in latest_jobs.items():
            if jid not in state_jobs:
                state_jobs[jid] = latest_job

        # If a concurrent request already put a job in a terminal/cancelled state,
        # do not let an older scheduler snapshot resurrect it as queued/running.
        terminal = {"completed", "failed", "cancelled"}
        for jid, latest_job in latest_jobs.items():
            if jid in state_jobs and isinstance(latest_job, dict) and isinstance(state_jobs.get(jid), dict):
                latest_status = str(latest_job.get("status") or "").lower()
                state_status = str(state_jobs[jid].get("status") or "").lower()
                if latest_status in terminal and state_status not in terminal:
                    state_jobs[jid] = latest_job

        # v4.04 burst-upload guard: never let an older worker snapshot move a job
        # backward from archived/server_paths/queued/running/testing to the older
        # receiving/uploading placeholder. Back-to-back uploads exposed this as
        # false upload timeouts after the files were already archived on the server.
        progress_rank = {
            "receiving": 10, "uploading": 20, "queued": 30,
            "running": 40, "testing": 50, "completed": 60,
            "failed": 60, "cancelled": 60,
        }
        archived_keys = ("archived_sof_path", "archived_verilog_path", "remote_sof", "sof_path", "verilog_path")
        for jid, latest_job in latest_jobs.items():
            if jid not in state_jobs or not isinstance(latest_job, dict) or not isinstance(state_jobs.get(jid), dict):
                continue
            snapshot_job = state_jobs[jid]
            latest_status = str(latest_job.get("status") or "").lower()
            snapshot_status = str(snapshot_job.get("status") or "").lower()
            latest_rank = progress_rank.get(latest_status, 0)
            snapshot_rank = progress_rank.get(snapshot_status, 0)
            # v5.04: a background archive snapshot must not overwrite a repaired
            # lightweight temp-stage job back to uploading/archive_retry.
            try:
                if snapshot_status == "queued" and _is_lightweight_tmp_handoff_job(snapshot_job) and _has_valid_tmp_stage(snapshot_job):
                    continue
            except Exception:
                pass
            latest_archived = bool(latest_job.get("upload_files_attached") or latest_job.get("kind") == "server_paths" or any(latest_job.get(k) for k in archived_keys))
            snapshot_archived = bool(snapshot_job.get("upload_files_attached") or snapshot_job.get("kind") == "server_paths" or any(snapshot_job.get(k) for k in archived_keys))
            if latest_archived and not snapshot_archived:
                state_jobs[jid] = latest_job
                continue
            if latest_rank > snapshot_rank and latest_status not in ("receiving", "uploading"):
                state_jobs[jid] = latest_job
                continue
            if latest_archived and snapshot_status in ("receiving", "uploading"):
                state_jobs[jid] = latest_job

        visible = {"receiving", "uploading", "queued"}
        active = {"running", "testing"}
        merged_queue = []
        for jid in list(state.get("queue", []) or []) + list(latest.get("queue", []) or []):
            if jid in state_jobs and str(state_jobs[jid].get("status") or "").lower() in visible:
                if jid not in merged_queue:
                    merged_queue.append(jid)
        state["queue"] = merged_queue

        # Preserve current running/testing job IDs from latest if this snapshot missed them.
        current_jobs = []
        for jid, job in state_jobs.items():
            if str(job.get("status") or "").lower() in active:
                current_jobs.append(jid)
        state["current_jobs"] = current_jobs
        state["current_job"] = current_jobs[0] if current_jobs else None

        # v4.54: preserve terminal job visibility across stale planner snapshots.
        # A background /boards or queue-repair save could previously keep the
        # terminal job object but overwrite recent_jobs with an older empty list.
        merged_recent: List[str] = []
        for jid in list(latest.get("recent_jobs", []) or []) + list(state.get("recent_jobs", []) or []):
            jid = str(jid or "")
            if not jid or jid not in state_jobs:
                continue
            if jid in merged_recent:
                merged_recent.remove(jid)
            merged_recent.append(jid)

        terminal_statuses = {"completed", "failed", "cancelled"}
        terminal_ids = [
            jid for jid, job in state_jobs.items()
            if isinstance(job, dict) and str(job.get("status") or "").lower() in terminal_statuses
        ]
        terminal_ids.sort(
            key=lambda jid: float(
                state_jobs.get(jid, {}).get("finished_ts")
                or state_jobs.get(jid, {}).get("test_end_ts")
                or state_jobs.get(jid, {}).get("created_ts")
                or 0
            )
        )
        for jid in terminal_ids:
            if jid in merged_recent:
                merged_recent.remove(jid)
            merged_recent.append(jid)
        state["recent_jobs"] = merged_recent[-25:]

        save_state(state)
def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def add_history(event: str, board: str = "", details: Optional[Dict[str, Any]] = None) -> None:
    def mutate(state: Dict[str, Any]):
        history = state.setdefault("history", [])
        history.append({"time": now_iso(), "event": event, "board": board, "details": details or {}})
        if len(history) > 200:
            del history[:-200]
    update_state_atomic(mutate)


def today_string() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def sanitize_history_name(value: Any) -> str:
    text = str(value or "unknown").strip()
    if "@" in text:
        text = text.split("@", 1)[0]
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return text or "unknown"


def _job_iso_date(job: Dict[str, Any]) -> str:
    for key in ("created_at", "queued_at", "upload_started_at", "upload_finished_at", "started_at", "test_start_at", "finished_at"):
        value = str((job or {}).get(key) or "")
        if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
            return value[:10]
    return ""


def cleanup_daily_state_rollover_in_state(state: Dict[str, Any]) -> bool:
    """Daily cleanup that never deletes current active/waiting jobs.

    v3.96 fix:
    If board_state.json still had yesterday's daily_date, the first queue-worker
    cycle after a new upload could clear jobs/queue completely. This made the GUI
    print 'Status: queued' but the queue table immediately became empty.
    """
    today = today_string()
    previous = str(state.get("daily_date") or "")
    if previous == today:
        return False

    jobs = state.setdefault("jobs", {})
    active_statuses = {"receiving", "uploading", "queued", "running", "testing"}
    keep_jobs = {}
    archived = 0
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            archived += 1
            continue
        status = str(job.get("status") or "").lower()
        job_day = _job_iso_date(job)
        # Always keep active/waiting jobs. Also keep any job created/updated today.
        if status in active_statuses or job_day == today:
            keep_jobs[jid] = job
        else:
            archived += 1

    state["previous_day_summary"] = {
        "date": previous,
        "cleared_at": now_iso(),
        "job_count_before": len(jobs),
        "archived_old_job_count": archived,
        "kept_current_job_count": len(keep_jobs),
        "mode": "safe_preserve_active_jobs",
    }
    state["daily_date"] = today
    state["jobs"] = keep_jobs
    state["queue"] = [jid for jid in state.get("queue", []) if jid in keep_jobs and str(keep_jobs[jid].get("status") or "").lower() in {"receiving", "uploading", "queued"}]
    state["recent_jobs"] = [jid for jid in state.get("recent_jobs", []) if jid in keep_jobs][-25:]
    active_ids = [jid for jid, job in keep_jobs.items() if str(job.get("status") or "").lower() in {"running", "testing"}]
    state["current_jobs"] = active_ids
    state["current_job"] = active_ids[0] if active_ids else None

    # Keep locks for active jobs; drop old stale locks only when no active owner matches.
    active_job_ids = set(active_ids)
    new_locks = {}
    for key, lock in list((state.get("locks", {}) or {}).items()):
        jid = str((lock or {}).get("job_id") or "")
        if jid and jid in active_job_ids:
            new_locks[key] = lock
        elif (lock or {}).get("busy") and str((lock or {}).get("phase") or "") == "testing" and jid in keep_jobs:
            new_locks[key] = lock
        elif not (lock or {}).get("busy"):
            # It is safe to drop old released lock records during daily rollover.
            continue
    state["locks"] = new_locks
    state["queue_plan"] = {}
    return True


def cleanup_daily_state_rollover() -> None:
    """Keep old completed jobs from growing forever without deleting live work."""
    def mutate(state: Dict[str, Any]):
        changed = cleanup_daily_state_rollover_in_state(state)
        return ok(daily_rollover_changed=bool(changed), daily_date=state.get("daily_date"))
    update_state_atomic(mutate)


EPHEMERAL_JOB_KEYS = {
    "extracted_evidence",
    "core_evidence",
    "weak_evidence",
    "qsf_evidence",
    "conflict_evidence",
    "raw_response",
    "raw_response_tail",
    "prompt",
    "prompt_json",
    "ai_prompt",
    "verilog_code",
    "qsf_text",
    "sof_bytes",
    "file_bytes",
}


def strip_ephemeral_job_material(value: Any) -> Any:
    """Return a persistence-safe copy without one-use source/evidence material."""
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in EPHEMERAL_JOB_KEYS:
                continue
            clean[str(key)] = strip_ephemeral_job_material(item)
        return clean
    if isinstance(value, list):
        return [strip_ephemeral_job_material(item) for item in value]
    return value


def build_job_history_summary(job: Dict[str, Any], event: str = "job_complete") -> Dict[str, Any]:
    job = strip_ephemeral_job_material(job or {})
    result = job.get("result", {}) if isinstance(job.get("result", {}), dict) else {}
    return {
        "event": event,
        "logged_at": now_iso(),
        "job_id": job.get("job_id", ""),
        "status": job.get("status", ""),
        "student": job.get("student") or job.get("client_hostname") or "unknown",
        "major": job.get("major", ""),
        "client_hostname": job.get("client_hostname", ""),
        "student_ip": job.get("student_ip", ""),
        "priority": job.get("priority_label") or priority_label_from_value(job.get("priority", "Student")),
        "source_mode": job.get("source_mode") or job.get("submit_mode") or job.get("origin") or "",
        "kind": job.get("kind", ""),
        "board": job.get("selected_board") or job.get("planned_board") or result.get("selected_board") or "",
        "jtag_instance": job.get("jtag_instance") or job.get("planned_instance_id") or "",
        "jtag_cable": job.get("jtag_cable") or job.get("planned_jtag_cable") or "",
        "requested_board": job.get("requested_board", ""),
        "created_at": job.get("created_at", ""),
        "started_at": job.get("started_at", ""),
        "finished_at": job.get("finished_at", ""),
        "elapsed_seconds": job.get("elapsed_seconds", 0),
        "program_seconds": job.get("program_seconds", result.get("program_seconds", 0)),
        "test_minutes": job.get("test_minutes", 0),
        "test_seconds": job.get("test_seconds", 0),
        "message": job.get("message", ""),
        "remote_sof": job.get("remote_sof", result.get("remote_sof", "")),
        "filename": job.get("filename", ""),
        "verilog_filename": job.get("verilog_filename", ""),
        "sof_filename": job.get("sof_filename", ""),
        "verilog_client_path": job.get("verilog_client_path", ""),
        "sof_client_path": job.get("sof_client_path", ""),
        "verilog_size_bytes": job.get("verilog_size_bytes", 0),
        "sof_size_bytes": job.get("sof_size_bytes", 0),
        "submission_signature": job.get("submission_signature", ""),
        "history_policy": job.get("history_policy", ""),
        "teacher_override": bool(job.get("teacher_override", False)),
        "bumped_by_teacher": bool(job.get("bumped_by_teacher", False)),
        "slot_cleared_at": job.get("slot_cleared_at", ""),
        "cleared_reason": job.get("cleared_reason", ""),
    }


def _terminal_history_event_for_status(status: str, fallback: str = "job_complete") -> str:
    st = str(status or "").strip().lower()
    if st == "cancelled":
        return "job_cancelled"
    if st == "failed":
        return "job_failed"
    if st == "completed":
        return "job_completed"
    return fallback


def history_base_dir() -> str:
    """Return the configured Quartus-server history directory.

    This helper is used by upload, history logging, and history test paths.
    It must never raise, because upload_files must not crash while only trying
    to store a metadata field such as archive_target.
    """
    try:
        cfg = load_config()
        hist_cfg = cfg.get("server_history", {}) or {}
        base_dir = str(hist_cfg.get("base_dir", "") or "").strip().rstrip("/")
        if base_dir:
            return base_dir
    except Exception:
        pass
    try:
        private_hist = get_pi_config_section("server_history", {}) or {}
        base_dir = str(private_hist.get("base_dir", "") or "").strip().rstrip("/")
        if base_dir:
            return base_dir
    except Exception:
        pass
    return "/home/lab4p0/History_of_jobs"


def latest_history_snapshot(job: Dict[str, Any], event: str) -> Tuple[Dict[str, Any], str]:
    """Return the freshest job state for the one-record-per-job history file.

    Queue records are written asynchronously so the GUI stays fast. If a user
    cancels immediately, an older queued-event worker may finish after the cancel.
    This helper prevents that stale queued snapshot from overwriting the final
    cancelled/failed/completed record.
    """
    snapshot = dict(job or {})
    jid = str(snapshot.get("job_id") or "").strip()
    resolved_event = str(event or "job_update")
    if not jid:
        return snapshot, resolved_event
    try:
        latest = (load_state().get("jobs", {}) or {}).get(jid)
        if isinstance(latest, dict) and latest:
            latest_status = str(latest.get("status") or "").strip().lower()
            snapshot_status = str(snapshot.get("status") or "").strip().lower()
            if latest_status in ("cancelled", "failed", "completed"):
                snapshot = dict(latest)
                snapshot["job_id"] = jid
                resolved_event = str(latest.get("history_final_event") or latest.get("server_history_last_async_event") or _terminal_history_event_for_status(latest_status, resolved_event))
            elif str(resolved_event).lower() in ("job_queued", "queue_add", "queue_prequeue") and snapshot_status in ("", "receiving", "queued", "pending"):
                snapshot = dict(latest)
                snapshot["job_id"] = jid
    except Exception:
        pass
    return snapshot, resolved_event


def mark_job_history_final(job: Dict[str, Any], event: str) -> Dict[str, Any]:
    if isinstance(job, dict):
        job["history_final_event"] = event
        job["history_final_at"] = now_iso()
    return job


def write_job_history_to_server(job: Dict[str, Any], event: str = "job_complete") -> Dict[str, Any]:
    """
    Write a small permanent history record on the Quartus server.

    v4.11 policy:
    - Do NOT archive large .v/.sv or .sof files here.
    - Keep only a small human-readable text record under the configured
      private server_history base directory.
    - The .sof is treated as a runtime programming file only.
    """
    job, event = latest_history_snapshot(job, event)
    cfg = load_config()
    hist_cfg = cfg.get("server_history", {}) or {}
    if not bool(hist_cfg.get("enabled", True)):
        return ok(history_logged=False, reason="server_history disabled")
    base_dir = str(hist_cfg.get("base_dir", "") or "").rstrip("/")
    if not base_dir:
        # v4.53 fallback: use the shared field directory for job history.
        try:
            base_dir = "/home/lab4p0/History_of_jobs"
            hist_cfg = dict(hist_cfg or {})
            hist_cfg["base_dir"] = base_dir
            private_hist = get_pi_config_section("server_history", {})
            private_hist["base_dir"] = base_dir
            set_pi_config_section("server_history", private_hist)
        except Exception:
            pass
    if not base_dir:
        return ok(history_logged=False, reason="server_history base_dir not configured")
    summary = build_job_history_summary(job, event=event)
    student_folder = sanitize_history_name(summary.get("student") or "unknown")
    safe_job_id = sanitize_history_name(summary.get("job_id") or uuid.uuid4().hex[:10])
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_dir = f"{base_dir}/{student_folder}"
    record_format = str(hist_cfg.get("record_format", "txt") or "txt").lower()
    ext = "json" if record_format == "json" else "txt"
    # v4.44: keep exactly one lightweight record file per Job ID. Earlier
    # versions used a timestamp in the filename, so queue/testing/completed
    # events could create multiple records for the same job. With this stable
    # filename, later lifecycle events update the same record instead of
    # adding duplicates.
    # Always use one stable file per Job ID. Queue, testing, cancel, failure,
    # and completion events update the same record instead of creating duplicate
    # files in /home/lab4p0/History_of_jobs.
    remote_file = f"{remote_dir}/{safe_job_id}_job_record.{ext}"

    if ext == "json":
        body = json.dumps(summary, indent=2, ensure_ascii=False)
    else:
        ordered_keys = [
            "event", "logged_at", "job_id", "status", "student", "major", "client_hostname", "student_ip",
            "priority", "source_mode", "kind", "board", "jtag_instance", "jtag_cable", "requested_board",
            "created_at", "started_at", "finished_at", "elapsed_seconds", "program_seconds",
            "test_minutes", "test_seconds", "message", "filename", "verilog_filename", "sof_filename",
            "verilog_client_path", "sof_client_path", "verilog_size_bytes", "sof_size_bytes",
            "submission_signature", "history_policy", "remote_sof",
        ]
        lines = ["UADY FPGA JOB RECORD", "====================", ""]
        for k in ordered_keys:
            if k in summary:
                lines.append(f"{k}: {summary.get(k, '')}")
        lines.append("")
        lines.append("NOTE: v4.44 stores one lightweight record per Job ID. The full .v/.sof files are temporary and are deleted from the Raspberry Pi after the job leaves active use.")
        body = "\n".join(lines) + "\n"

    ssh = connect_server()
    try:
        stdin, stdout, stderr = ssh.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
        stdout.channel.recv_exit_status()
        sftp = ssh.open_sftp()
        try:
            with sftp.file(remote_file, "w") as f:
                f.write(body)
        finally:
            sftp.close()
    finally:
        ssh.close()

    return ok(history_logged=True, history_path=remote_file, student_folder=student_folder, lightweight_record_only=True)


def log_server_history_result(job_id: str, event: str, res: Dict[str, Any]) -> None:
    """Print explicit history-write status so missing /home/lab4p0/History_of_jobs records are visible."""
    try:
        if isinstance(res, dict) and res.get("success") and res.get("history_logged"):
            print(f"[HISTORY OK] job={job_id} event={event} path={res.get('history_path', '')}", flush=True)
        else:
            print(f"[HISTORY WARN] job={job_id} event={event} result={json.dumps(res, ensure_ascii=False, default=str)}", flush=True)
    except Exception:
        pass


def write_job_history_immediate(job_id: str, job: Dict[str, Any], event: str) -> Dict[str, Any]:
    """Write/update the one stable history record now, recording any failure in state."""
    jid = str(job_id or (job or {}).get("job_id") or "").strip()
    snapshot = dict(job or {})
    if jid:
        snapshot["job_id"] = jid
    try:
        fresh, fresh_event = latest_history_snapshot(snapshot, event)
        res = write_job_history_to_server(fresh, event=fresh_event)
    except Exception as e:
        res = fail(
            f"server history logging failed: {e}",
            exception_type=type(e).__name__,
            history_base_dir=history_base_dir(),
            hint="On the Quartus server, make sure /home/lab4p0/History_of_jobs exists and is writable by the Quartus SSH user.",
        )
        fresh_event = event
    log_server_history_result(jid, fresh_event, res)
    try:
        def mutate(state: Dict[str, Any]):
            j = state.setdefault("jobs", {}).get(jid)
            if isinstance(j, dict):
                key_name = "server_history_" + re.sub(r"[^A-Za-z0-9_]+", "_", str(fresh_event)).strip("_")
                j[key_name] = res
                j["server_history_last_event"] = fresh_event
                j["server_history_last_result"] = res
                state.setdefault("jobs", {})[jid] = j
            return ok(job_id=jid, event=fresh_event)
        if jid:
            update_state_atomic(mutate)
    except Exception:
        pass
    return res


def write_job_history_to_server_async(job_id: str, job: Dict[str, Any], event: str) -> bool:
    """Write the small server history record without blocking the upload response.

    v4.22: each accepted job receives a lightweight record on the Quartus server
    soon after it is queued.  This lets the Raspberry Pi delete the temporary
    per-job stage folder later without losing accountability.  The function is
    best-effort and never fails the student job.
    """
    jid = str(job_id or (job or {}).get("job_id") or "").strip()
    if not jid:
        return False
    key = f"{jid}:{event}"
    with SERVER_HISTORY_THREADS_LOCK:
        old = SERVER_HISTORY_THREADS.get(key)
        if old and not old.done():
            return False

        snapshot = dict(job or {})
        snapshot["job_id"] = jid

        def _worker():
            fresh_event = event
            try:
                fresh_snapshot, fresh_event = latest_history_snapshot(snapshot, event)
                res = write_job_history_to_server(fresh_snapshot, event=fresh_event)
            except Exception as e:
                res = fail(f"async server history logging failed: {e}", exception_type=type(e).__name__, history_base_dir=history_base_dir(), hint="Make sure /home/lab4p0/History_of_jobs exists and is writable by the Quartus SSH user.")
            log_server_history_result(jid, fresh_event, res)
            try:
                def mutate(state: Dict[str, Any]):
                    j = state.setdefault("jobs", {}).get(jid)
                    if isinstance(j, dict):
                        key_name = "server_history_" + re.sub(r"[^A-Za-z0-9_]+", "_", str(fresh_event)).strip("_")
                        j[key_name] = res
                        j["server_history_last_async_event"] = fresh_event
                        state.setdefault("jobs", {})[jid] = j
                    return ok(job_id=jid, event=fresh_event)
                update_state_atomic(mutate)
            except Exception:
                pass
            finally:
                with SERVER_HISTORY_THREADS_LOCK:
                    SERVER_HISTORY_THREADS.pop(key, None)

        if not SERVER_HISTORY_PENDING_LIMIT.acquire(blocking=False):
            return False

        def _release_pending(fut):
            try:
                SERVER_HISTORY_PENDING_LIMIT.release()
            except Exception:
                pass

        try:
            future = SERVER_HISTORY_EXECUTOR.submit(_worker)
        except Exception:
            SERVER_HISTORY_PENDING_LIMIT.release()
            return False
        future.add_done_callback(_release_pending)
        SERVER_HISTORY_THREADS[key] = future
        return True


def cleanup_finished_job_temp_files(job: Dict[str, Any], cleanup_reason: str = "finished_job_cleanup") -> Dict[str, Any]:
    """Delete temporary Raspberry Pi per-job files after a job is no longer needed.

    This intentionally removes only Pi-side working cache from upload_spool.
    It never removes the permanent lightweight text record on the Quartus server.
    """
    result: Dict[str, Any] = {"success": True, "reason": cleanup_reason}
    try:
        if job.get("temporary_pi_spool"):
            result["spool"] = cleanup_temporary_spool_for_job(job)
    except Exception as e:
        result["spool"] = fail(f"temporary spool cleanup failed: {e}")
    try:
        if job.get("temporary_stage_cache"):
            result["stage"] = cleanup_staged_files_for_job(job)
    except Exception as e:
        result["stage"] = fail(f"temporary stage cleanup failed: {e}")
    try:
        deleted_ephemeral = []
        for raw_path in list(job.get("ephemeral_paths") or []):
            try:
                candidate = Path(str(raw_path)).expanduser().resolve()
                allowed_roots = [
                    upload_spool_root().resolve(),
                    Path(tempfile.gettempdir()).resolve(),
                ]
                if not any(candidate == root or root in candidate.parents for root in allowed_roots):
                    continue
                if candidate.is_dir():
                    shutil.rmtree(candidate, ignore_errors=True)
                    deleted_ephemeral.append(str(candidate))
                elif candidate.exists():
                    candidate.unlink(missing_ok=True)
                    deleted_ephemeral.append(str(candidate))
            except Exception:
                pass
        result["ephemeral_deleted"] = deleted_ephemeral
    except Exception:
        pass
    try:
        result["deleted_at"] = now_iso()
        result["job_id"] = job.get("job_id", "")
    except Exception:
        pass
    return result



def upload_spool_root() -> Path:
    """Return the Raspberry Pi temporary upload spool directory.

    This is only runtime storage used while a queued job is waiting/running.
    It is not permanent student storage and is cleaned after finish/cancel.
    The helper must exist before /queue/<job_id>/upload_files saves files.
    """
    try:
        cfg = load_config()
        spool_cfg = cfg.get("upload_spool", {}) or {}
        raw = str(spool_cfg.get("root", "upload_spool") or "upload_spool").strip()
    except Exception:
        raw = "upload_spool"
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = BASE_DIR / root
    root.mkdir(parents=True, exist_ok=True)
    return root

def cleanup_temporary_spool_for_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Delete temporary Pi upload spool after programming has started/finished.

    This keeps the Pi as a controller, not permanent student-file storage.
    """
    if not bool(job.get("temporary_pi_spool")):
        return ok(cleanup=False, reason="not temporary spool")
    raw = str(job.get("spool_dir") or "").strip()
    if not raw:
        return ok(cleanup=False, reason="no spool_dir")
    try:
        root = upload_spool_root().resolve()
        target = Path(raw).resolve()
        if root not in target.parents and target != root:
            return fail("refusing to delete path outside upload_spool", path=str(target))
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
            return ok(cleanup=True, deleted=str(target))
        return ok(cleanup=False, reason="spool already missing", path=str(target))
    except Exception as e:
        return fail("temporary spool cleanup failed", error=str(e), path=raw)




def staged_queue_root() -> Path:
    """Raspberry Pi temporary queued-job staging area.

    A queued upload is sealed here as paired .v.tmp/.sof.tmp files plus a
    manifest.  The files are not permanent history; they are a durable handoff
    cache that survives GUI disconnects and prevents the FIFO worker from
    forgetting the final waiting jobs when every board is busy.

    v4.21: the folder is configurable from config_pi_hat.json under
    queue_staging.root. Relative paths are stored under this Raspberry Pi
    controller folder. The default is now inside upload_spool so the main
    Raspberry Pi controller folder stays clean, for example:
        <pi-controller-dir>/upload_spool/queued_job_stage
    """
    cfg = (load_config().get("queue_staging", {}) or {})
    root_name = str(cfg.get("root", "upload_spool/queued_job_stage") or "upload_spool/queued_job_stage").strip()
    root_candidate = Path(root_name)
    if not root_candidate.is_absolute():
        root_candidate = BASE_DIR / root_candidate
    root = root_candidate
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_inside(root: Path, candidate: Path) -> bool:
    try:
        r = root.resolve()
        c = candidate.resolve()
        return c == r or r in c.parents
    except Exception:
        return False


def queue_stage_compression_config() -> Dict[str, Any]:
    """Configuration for smaller queued temporary files.

    Keeping a full uncompressed .sof is required only at the moment Quartus/JTAG
    programs the board. While a job is waiting in FIFO, the SOF can be stored as
    gzip to reduce Raspberry Pi disk usage. The code remains backward compatible
    with older .sof.tmp staging folders.
    """
    cfg = (load_config().get("queue_staging", {}) or {})
    return {
        # SOF is usually the large payload, so compress it while waiting in queue.
        "compress_sof_tmp": bool(cfg.get("compress_sof_tmp", True)),
        # Verilog/SystemVerilog and QSF are normally small, so use smart compression:
        # keep .tmp.gz only when gzip is actually smaller than the plain .tmp file.
        "compress_verilog_tmp": bool(cfg.get("compress_verilog_tmp", True)),
        "compress_qsf_tmp": bool(cfg.get("compress_qsf_tmp", True)),
        "gzip_level": max(1, min(9, int(cfg.get("sof_gzip_level", 3) or 3))),
        "text_gzip_level": max(1, min(9, int(cfg.get("text_gzip_level", 6) or 6))),
        "delete_compressed_after_activation": bool(cfg.get("delete_compressed_sof_after_activation", True)),
    }


def _gzip_copy_file(src: Path, dst: Path, *, compresslevel: int = 3) -> int:
    """Copy src to gzip-compressed dst and return compressed size in bytes."""
    with open(src, "rb") as f_in, gzip.open(dst, "wb", compresslevel=compresslevel) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
    return int(dst.stat().st_size)


def _gzip_or_copy_smallest(src: Path, plain_dst: Path, gz_dst: Path, *, compresslevel: int = 6) -> Tuple[Path, bool, int, int]:
    """Store src using the smallest safe temp representation.

    For tiny text files, gzip headers can make the file larger. This helper tries
    gzip first, then keeps .tmp.gz only when it is smaller than the original. If
    gzip is not smaller, it stores a normal .tmp copy. Return:
        (chosen_path, compressed, original_size, chosen_size)
    """
    original_size = int(src.stat().st_size)
    try:
        gz_size = _gzip_copy_file(src, gz_dst, compresslevel=compresslevel)
    except Exception:
        gz_size = original_size + 1
        try:
            if gz_dst.exists():
                gz_dst.unlink()
        except Exception:
            pass
    if gz_dst.exists() and gz_size < original_size:
        try:
            if plain_dst.exists():
                plain_dst.unlink()
        except Exception:
            pass
        return gz_dst, True, original_size, int(gz_size)
    shutil.copy2(src, plain_dst)
    try:
        if gz_dst.exists():
            gz_dst.unlink()
    except Exception:
        pass
    return plain_dst, False, original_size, int(plain_dst.stat().st_size)


def _gunzip_copy_file(src: Path, dst: Path) -> int:
    """Decompress gzip src to dst and return uncompressed size in bytes."""
    tmp = dst.with_name(dst.name + f".decompressing.{os.getpid()}.{threading.get_ident()}")
    try:
        with gzip.open(src, "rb") as f_in, open(tmp, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
        tmp.replace(dst)
        return int(dst.stat().st_size)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def create_staged_tmp_files_for_job(job_id: str, job: Dict[str, Any], spool: Dict[str, Any]) -> Dict[str, Any]:
    """Create the queued-job staging package.

    This is intentionally lightweight and local to the Pi controller:
    - .v.tmp stores the submitted Verilog/SystemVerilog source.
    - .sof.tmp stores the exact SOF payload to program later.
    - manifest.json indexes the files by Job ID, timestamp, user, and filenames.

    The queue worker activates the package only when a real JTAG slot is free.
    """
    with QUEUE_STAGE_WRITE_LOCK:
        try:
            root = staged_queue_root()
            job_id_safe = secure_filename(str(job_id)) or uuid.uuid4().hex[:10]
            stage_dir = root / job_id_safe
            if stage_dir.exists():
                # v4.41: upload retry/race hardening. If a prior request already
                # created or activated this stage directory while the GUI is still
                # waiting for the HTTP reply, do not crash the second attempt with
                # Directory not empty / missing tmp-file errors. Try to clear it;
                # if the queue worker is touching it, fall back to a unique stage
                # folder for this same job instead of failing the upload.
                try:
                    shutil.rmtree(stage_dir)
                except Exception:
                    stage_dir = root / f"{job_id_safe}_{uuid.uuid4().hex[:8]}"
            stage_dir.mkdir(parents=True, exist_ok=True)

            src_v = Path(str(spool.get("spool_verilog_path") or ""))
            src_s = Path(str(spool.get("spool_sof_path") or ""))
            src_q = Path(str(spool.get("spool_qsf_path") or "")) if spool.get("spool_qsf_path") else None
            if not src_v.is_file() or src_v.suffix.lower() not in (".v", ".sv"):
                return fail("staging failed: missing temporary Verilog file", job_id=job_id, path=str(src_v))
            if not src_s.is_file() or src_s.suffix.lower() != ".sof":
                return fail("staging failed: missing temporary SOF file", job_id=job_id, path=str(src_s))
            if src_q is not None and (not src_q.is_file() or src_q.suffix.lower() != ".qsf"):
                return fail("staging failed: optional temporary QSF file is invalid", job_id=job_id, path=str(src_q))

            stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            v_name = secure_filename(str(spool.get("verilog_filename") or src_v.name or "design.v"))
            s_name = secure_filename(str(spool.get("sof_filename") or src_s.name or "design.sof"))
            q_name = secure_filename(str(spool.get("qsf_filename") or (src_q.name if src_q else "") or ""))
            stage_v_tmp_plain = stage_dir / f"{stamp}_{job_id_safe}_{v_name}.tmp"
            stage_v_tmp_gz = stage_dir / f"{stamp}_{job_id_safe}_{v_name}.tmp.gz"
            active_v = stage_dir / f"{stamp}_{job_id_safe}_{v_name}"
            active_s = stage_dir / f"{stamp}_{job_id_safe}_{s_name}"
            stage_q_tmp_plain = stage_dir / f"{stamp}_{job_id_safe}_{q_name}.tmp" if src_q is not None and q_name else None
            stage_q_tmp_gz = stage_dir / f"{stamp}_{job_id_safe}_{q_name}.tmp.gz" if src_q is not None and q_name else None
            active_q = stage_dir / f"{stamp}_{job_id_safe}_{q_name}" if src_q is not None and q_name else None
            manifest_path = stage_dir / f"{stamp}_{job_id_safe}_manifest.json"

            compression = queue_stage_compression_config()
            compress_sof = bool(compression.get("compress_sof_tmp", True))
            compress_v = bool(compression.get("compress_verilog_tmp", True))
            compress_q = bool(compression.get("compress_qsf_tmp", True))
            if compress_sof:
                stage_s_tmp = stage_dir / f"{stamp}_{job_id_safe}_{s_name}.tmp.gz"
            else:
                stage_s_tmp = stage_dir / f"{stamp}_{job_id_safe}_{s_name}.tmp"

            # Keep every queued temp payload as small as safely possible.
            # - SOF is usually large, so it is compressed while waiting.
            # - Verilog/QSF are smart-compressed: .tmp.gz is kept only if smaller;
            #   otherwise the plain .tmp copy is kept to avoid gzip overhead.
            if compress_v:
                stage_v_tmp, v_compressed, v_original_size, v_tmp_size = _gzip_or_copy_smallest(
                    src_v, stage_v_tmp_plain, stage_v_tmp_gz, compresslevel=int(compression.get("text_gzip_level", 6))
                )
            else:
                shutil.copy2(src_v, stage_v_tmp_plain)
                stage_v_tmp = stage_v_tmp_plain
                v_compressed = False
                v_original_size = int(src_v.stat().st_size)
                v_tmp_size = int(stage_v_tmp.stat().st_size)

            stage_q_tmp = None
            q_compressed = False
            q_original_size = 0
            q_tmp_size = 0
            if src_q is not None and stage_q_tmp_plain is not None and stage_q_tmp_gz is not None:
                if compress_q:
                    stage_q_tmp, q_compressed, q_original_size, q_tmp_size = _gzip_or_copy_smallest(
                        src_q, stage_q_tmp_plain, stage_q_tmp_gz, compresslevel=int(compression.get("text_gzip_level", 6))
                    )
                else:
                    shutil.copy2(src_q, stage_q_tmp_plain)
                    stage_q_tmp = stage_q_tmp_plain
                    q_compressed = False
                    q_original_size = int(src_q.stat().st_size)
                    q_tmp_size = int(stage_q_tmp.stat().st_size)

            original_sof_size = int(src_s.stat().st_size)
            if compress_sof:
                compressed_size = _gzip_copy_file(src_s, stage_s_tmp, compresslevel=int(compression.get("gzip_level", 3)))
                s_size = original_sof_size
                stage_s_tmp_size = compressed_size
            else:
                shutil.copy2(src_s, stage_s_tmp)
                s_size = int(stage_s_tmp.stat().st_size)
                stage_s_tmp_size = s_size
            v_size = int(v_original_size)
            q_size = int(q_original_size)
            manifest = {
                "job_id": str(job_id),
                "created_at": now_iso(),
                "created_ts": time.time(),
                "student": job.get("student") or job.get("client_hostname") or "unknown",
                "client_hostname": job.get("client_hostname", ""),
                "verilog_filename": v_name,
                "sof_filename": s_name,
                "qsf_filename": q_name,
                "stage_dir": str(stage_dir),
                "stage_verilog_tmp_path": str(stage_v_tmp),
                "stage_sof_tmp_path": str(stage_s_tmp),
                "stage_qsf_tmp_path": str(stage_q_tmp) if stage_q_tmp is not None else "",
                "stage_verilog_active_path": str(active_v),
                "stage_sof_active_path": str(active_s),
                "stage_qsf_active_path": str(active_q) if active_q is not None else "",
                "stage_verilog_tmp_compressed": bool(v_compressed),
                "stage_verilog_tmp_size_bytes": int(v_tmp_size),
                "stage_verilog_uncompressed_size_bytes": int(v_original_size),
                "stage_qsf_tmp_compressed": bool(q_compressed),
                "stage_qsf_tmp_size_bytes": int(q_tmp_size),
                "stage_qsf_uncompressed_size_bytes": int(q_original_size),
                "stage_sof_tmp_compressed": bool(compress_sof),
                "stage_sof_tmp_size_bytes": int(stage_s_tmp_size),
                "stage_sof_uncompressed_size_bytes": int(original_sof_size),
                "verilog_size_bytes": v_size,
                "sof_size_bytes": s_size,
                "qsf_size_bytes": q_size,
                "policy": "queued_tmp_handoff_smallest_temp_activate_when_slot_free",
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            return ok(**manifest, stage_manifest_path=str(manifest_path), stage_ready=True)
        except Exception as e:
            return fail("queued staging failed", job_id=job_id, error=str(e), exception_type=type(e).__name__)


def staged_job_files_ready(job: Dict[str, Any]) -> Tuple[bool, str]:
    """Return whether a queued upload has a durable stage package or active files."""
    try:
        active_v = Path(str(job.get("stage_verilog_active_path") or ""))
        active_s = Path(str(job.get("stage_sof_active_path") or ""))
        if active_v.is_file() and active_v.suffix.lower() in (".v", ".sv") and active_s.is_file() and active_s.suffix.lower() == ".sof":
            return True, "active_staged_files_ready"
        tmp_v = Path(str(job.get("stage_verilog_tmp_path") or ""))
        tmp_s = Path(str(job.get("stage_sof_tmp_path") or ""))
        tmp_s_l = str(tmp_s).lower()
        tmp_v_l = str(tmp_v).lower()
        v_ready = tmp_v.is_file() and tmp_v_l.endswith((".v.tmp", ".sv.tmp", ".v.tmp.gz", ".sv.tmp.gz"))
        s_ready = tmp_s.is_file() and (tmp_s_l.endswith(".sof.tmp") or tmp_s_l.endswith(".sof.tmp.gz"))
        if v_ready and s_ready:
            compressed_any = tmp_v_l.endswith(".gz") or tmp_s_l.endswith(".gz") or str(job.get("stage_qsf_tmp_path") or "").lower().endswith(".gz")
            return True, "tmp_stage_files_waiting_for_slot_small_compressed_cache" if compressed_any else "tmp_stage_files_waiting_for_slot"
        return False, "waiting for queued .v/.sv + .sof temp stage files"
    except Exception as e:
        return False, f"staging check failed: {e}"


def upload_already_accepted_for_programming(job: Dict[str, Any]) -> Tuple[bool, str]:
    """True when /upload_files may be answered idempotently.

    v4.41: The queue worker can promote a freshly accepted upload to running
    very quickly.  If the GUI retries /upload_files while that is happening,
    the controller must not answer 400 and make the GUI think the Pi is offline.
    """
    try:
        if not isinstance(job, dict):
            return False, "no job"
        status_l = str(job.get("status") or "").lower()
        if status_l in ("queued", "running", "testing", "completed") and bool(job.get("upload_files_attached")):
            return True, f"upload already accepted; status={status_l}"
        ready, reason = staged_job_files_ready(job)
        if ready:
            return True, reason
        if is_valid_verilog_file_path(job.get("verilog_local_path")) and is_valid_sof_file_path(job.get("sof_local_path")):
            return True, "active local programming files already exist"
        if status_l in ("running", "testing", "completed") and (job.get("sof_local_path") or job.get("remote_sof") or job.get("archived_sof_path")):
            return True, f"job already reached {status_l} with a programming file"
        return False, "upload not accepted yet"
    except Exception as e:
        return False, f"accepted-check failed: {e}"


def activate_staged_job_files(job_id: str, job: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, str]:
    """Convert .v.tmp/.sof.tmp into active .v/.sof exactly when a slot is chosen.

    The operation is idempotent.  If active files already exist because a prior
    runner was requeued, they are reused.  This is the explicit queue-to-hardware
    handoff requested by the lab.
    """
    job = dict(job or {})
    try:
        root = staged_queue_root()
        active_v = Path(str(job.get("stage_verilog_active_path") or ""))
        active_s = Path(str(job.get("stage_sof_active_path") or ""))
        active_q = Path(str(job.get("stage_qsf_active_path") or "")) if job.get("stage_qsf_active_path") else None
        if active_v.is_file() and active_v.suffix.lower() in (".v", ".sv") and active_s.is_file() and active_s.suffix.lower() == ".sof":
            job["verilog_local_path"] = str(active_v)
            job["sof_local_path"] = str(active_s)
            if active_q is not None and active_q.is_file() and active_q.suffix.lower() == ".qsf":
                job["qsf_local_path"] = str(active_q)
                try:
                    job["qsf_text"] = active_q.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    pass
            job["upload_stage"] = "stage_activated_for_programming"
            return job, True, "stage already active"

        tmp_v = Path(str(job.get("stage_verilog_tmp_path") or ""))
        tmp_s = Path(str(job.get("stage_sof_tmp_path") or ""))
        tmp_q = Path(str(job.get("stage_qsf_tmp_path") or "")) if job.get("stage_qsf_tmp_path") else None
        safe_q = True if tmp_q is None else _safe_inside(root, tmp_q)
        if not (_safe_inside(root, tmp_v) and _safe_inside(root, tmp_s) and safe_q):
            return job, False, "stage files are outside upload_spool/queued_job_stage; refusing to activate"
        if not tmp_v.is_file() or not tmp_s.is_file():
            return job, False, "stage .v.tmp/.sof.tmp files are missing"
        if tmp_q is not None and not tmp_q.is_file():
            return job, False, "stage optional .qsf.tmp file is missing"
        tmp_v_l = str(tmp_v).lower()
        if not tmp_v_l.endswith((".v.tmp", ".sv.tmp", ".v.tmp.gz", ".sv.tmp.gz")):
            return job, False, "stage Verilog tmp file has invalid suffix"
        tmp_q_l = str(tmp_q).lower() if tmp_q is not None else ""
        if tmp_q is not None and not (tmp_q_l.endswith(".qsf.tmp") or tmp_q_l.endswith(".qsf.tmp.gz")):
            return job, False, "stage QSF tmp file has invalid suffix"
        tmp_s_l = str(tmp_s).lower()
        if not (tmp_s_l.endswith(".sof.tmp") or tmp_s_l.endswith(".sof.tmp.gz")):
            return job, False, "stage SOF tmp file has invalid suffix"

        # Use replace/gunzip so a retry can promote the same payload safely.
        # Compressed temp caches are decompressed only when a physical board slot
        # is actually available, then deleted to keep upload_spool small.
        delete_compressed = bool(queue_stage_compression_config().get("delete_compressed_after_activation", True))
        if tmp_v_l.endswith(".gz"):
            _gunzip_copy_file(tmp_v, active_v)
            job["stage_verilog_tmp_compressed"] = True
            if delete_compressed:
                try:
                    tmp_v.unlink()
                    job["stage_compressed_verilog_deleted_after_activation"] = True
                except Exception as e:
                    job["stage_compressed_verilog_delete_warning"] = str(e)
        else:
            tmp_v.replace(active_v)
            job["stage_verilog_tmp_compressed"] = False
        if tmp_q is not None and active_q is not None:
            if tmp_q_l.endswith(".gz"):
                _gunzip_copy_file(tmp_q, active_q)
                job["stage_qsf_tmp_compressed"] = True
                if delete_compressed:
                    try:
                        tmp_q.unlink()
                        job["stage_compressed_qsf_deleted_after_activation"] = True
                    except Exception as e:
                        job["stage_compressed_qsf_delete_warning"] = str(e)
            else:
                tmp_q.replace(active_q)
                job["stage_qsf_tmp_compressed"] = False
            job["qsf_local_path"] = str(active_q)
            try:
                job["qsf_text"] = active_q.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
        if tmp_s_l.endswith(".gz"):
            uncompressed_size = _gunzip_copy_file(tmp_s, active_s)
            job["stage_sof_tmp_compressed"] = True
            job["stage_sof_uncompressed_size_bytes"] = int(uncompressed_size)
            if delete_compressed:
                try:
                    tmp_s.unlink()
                    job["stage_compressed_sof_deleted_after_activation"] = True
                except Exception as e:
                    job["stage_compressed_sof_delete_warning"] = str(e)
        else:
            tmp_s.replace(active_s)
            job["stage_sof_tmp_compressed"] = False
        job["verilog_local_path"] = str(active_v)
        job["sof_local_path"] = str(active_s)
        job["upload_stage"] = "stage_activated_for_programming"
        job["stage_activated_at"] = now_iso()
        job["stage_activated_ts"] = time.time()
        job["message"] = "stage activated; programming can start"
        compressed_any = tmp_v_l.endswith(".gz") or tmp_s_l.endswith(".gz") or tmp_q_l.endswith(".gz")
        return job, True, "stage activated from compressed small temp cache" if compressed_any else "stage activated"
    except Exception as e:
        return job, False, f"stage activation failed: {e}"


def cleanup_staged_files_for_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Delete non-permanent queued stage cache once programming has succeeded/ended."""
    try:
        raw = str(job.get("stage_dir") or "").strip()
        if not raw:
            return ok(cleanup=False, reason="no stage_dir")
        root = staged_queue_root()
        target = Path(raw)
        if not _safe_inside(root, target):
            return fail("refusing to delete path outside upload_spool/queued_job_stage", path=str(target))
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
            return ok(cleanup=True, deleted=str(target))
        return ok(cleanup=False, reason="stage already missing", path=str(target))
    except Exception as e:
        return fail("staged file cleanup failed", error=str(e))



def job_is_testing_like(job: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> bool:
    """True when a row is logically in the student testing reservation even if an
    older UI/state snapshot still labels it as running.

    This fixes cancel problems where a programmed job held a testing lock but the
    cancel endpoint saw status=running and refused to cancel it.
    """
    try:
        if not isinstance(job, dict):
            return False
        if str(job.get("status") or "").lower() == "testing":
            return True
        if str(job.get("running_phase") or "").lower() == "testing":
            return True
        if bool(job.get("held_for_testing")) and (job.get("test_end_ts") or job.get("test_timer")):
            return True
        board = str(job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or "")
        cable = str(job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or "")
        jid = str(job.get("job_id") or "")
        if state is not None and board and cable and jid:
            lock = (state.get("locks", {}) or {}).get(instance_lock_key(board, cable), {})
            if str(lock.get("phase") or "").lower() == "testing" and str(lock.get("job_id") or "") == jid:
                return True
    except Exception:
        return False
    return False


def kill_remote_quartus_for_job(job_id: str) -> Dict[str, Any]:
    """Best-effort remote cancel for an in-flight quartus_pgm/copy process.

    It only targets commands/log paths containing this exact job id. If nothing is
    found, cancellation still proceeds logically and late runner results are ignored.
    """
    jid = secure_filename(str(job_id or "").strip())
    if not jid:
        return fail("missing job id for remote kill")
    try:
        pattern = shlex.quote(jid)
        cmd = (
            f"pkill -TERM -f {pattern} 2>/dev/null || true; "
            f"sleep 1; "
            f"pkill -KILL -f {pattern} 2>/dev/null || true; "
            f"rm -f /tmp/pi_ai_{jid}.log 2>/dev/null || true; "
            f"echo cancel_sent_for_{jid}"
        )
        code, out, err = run_remote(cmd, timeout=8)
        return ok(returncode=code, stdout=_tail_text(out, 300), stderr=_tail_text(err, 300), job_id=jid)
    except Exception as e:
        return fail("remote quartus cancel failed", error=str(e), job_id=jid)


def queue_staging_cleanup_config() -> Dict[str, Any]:
    cfg = (load_config().get("queue_staging", {}) or {})
    def _int_value(key: str, default: int) -> int:
        try:
            return max(1, int(cfg.get(key, default)))
        except Exception:
            return int(default)
    return {
        "enabled": bool(cfg.get("auto_delete_temp_files", True)),
        "ttl_seconds": _int_value("delete_temp_files_after_seconds", 3600),
        "terminal_ttl_seconds": max(0, int(cfg.get("delete_finished_job_temp_files_after_seconds", 0) or 0)),
        "interval_seconds": _int_value("cleanup_interval_seconds", 3600),
        "root": str(staged_queue_root()),
        "protected_statuses": set(cfg.get("protect_statuses", ["receiving", "uploading", "queued", "running"]) or []),
        "cleanup_statuses": set(cfg.get("cleanup_statuses", ["testing", "completed", "failed", "cancelled"]) or []),
    }


def _directory_age_seconds(path: Path, now_ts: float) -> float:
    latest = 0.0
    try:
        for p in path.rglob("*"):
            try:
                latest = max(latest, float(p.stat().st_mtime))
            except Exception:
                pass
        try:
            latest = max(latest, float(path.stat().st_mtime))
        except Exception:
            pass
    except Exception:
        pass
    if latest <= 0:
        return 0.0
    return max(0.0, now_ts - latest)


def cleanup_old_temporary_stage_cache(reason: str = "hourly_stage_cleanup") -> Dict[str, Any]:
    """Delete old inactive temporary stage folders from the Raspberry Pi.

    v4.20 policy:
    - Keep receiving/uploading/queued/running jobs even if they wait longer than one hour.
      Deleting those would break a real waiting job.
    - Delete testing/completed/failed/cancelled cache folders after the configured TTL.
    - Delete orphan stage folders not referenced by any current job after the TTL.

    The permanent audit trail is the small text record in the configured private server history folder.
    This folder is only a Pi-side operational cache.
    """
    cfg = queue_staging_cleanup_config()
    if not cfg.get("enabled"):
        return ok(cleanup_enabled=False, reason="stage cleanup disabled")
    now_ts = time.time()
    ttl = float(cfg.get("ttl_seconds", 3600))
    root = staged_queue_root()
    deleted: List[str] = []
    kept_active: List[str] = []
    errors: List[Dict[str, Any]] = []
    changed_jobs: List[str] = []

    state = load_state()
    jobs = state.get("jobs", {}) or {}
    referenced: Dict[str, Tuple[str, str]] = {}
    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        raw = str(job.get("stage_dir") or "").strip()
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
            if _safe_inside(root, p):
                referenced[str(p)] = (str(jid), str(job.get("status") or ""))
        except Exception:
            continue

    # First clean referenced terminal/test jobs after TTL and update state with the result.
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        if not job.get("temporary_stage_cache"):
            continue
        raw = str(job.get("stage_dir") or "").strip()
        if not raw:
            continue
        try:
            target = Path(raw).resolve()
            if not _safe_inside(root, target):
                continue
            status_l = str(job.get("status") or "").lower()
            age = _directory_age_seconds(target, now_ts) if target.exists() else ttl + 1
            if status_l in cfg.get("protected_statuses", set()):
                kept_active.append(f"{jid}:{status_l}")
                continue
            if status_l in cfg.get("cleanup_statuses", set()):
                status_ttl = float(cfg.get("terminal_ttl_seconds", ttl))
                if age < status_ttl:
                    continue
                res = cleanup_staged_files_for_job(job)
                job["hourly_stage_cleanup"] = {**res, "reason": reason, "age_seconds": int(age), "ttl_seconds": int(status_ttl)}
                state.setdefault("jobs", {})[jid] = job
                changed_jobs.append(str(jid))
                if res.get("cleanup"):
                    deleted.append(str(target))
        except Exception as e:
            errors.append({"job_id": str(jid), "error": str(e)})

    # Then remove orphan folders that no job actively references anymore.
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                resolved = str(child.resolve())
                age = _directory_age_seconds(child, now_ts)
                if resolved in referenced:
                    jid, status_l = referenced[resolved]
                    if status_l.lower() in cfg.get("protected_statuses", set()):
                        continue
                    if age < ttl:
                        continue
                else:
                    if age < ttl:
                        continue
                shutil.rmtree(child)
                deleted.append(str(child))
            except Exception as e:
                errors.append({"path": str(child), "error": str(e)})
    except Exception as e:
        errors.append({"root": str(root), "error": str(e)})

    if changed_jobs:
        try:
            state["last_stage_temp_cleanup"] = {
                "at": now_iso(),
                "ts": now_ts,
                "reason": reason,
                "deleted_count": len(deleted),
                "changed_jobs": changed_jobs[-20:],
                "ttl_seconds": int(ttl),
                "root": str(root),
            }
            save_state_preserving_concurrent_jobs(state)
        except Exception as e:
            errors.append({"save_state": str(e)})

    return ok(cleanup_enabled=True, reason=reason, root=str(root), ttl_seconds=int(ttl), deleted_count=len(deleted), deleted=deleted[-20:], kept_active_count=len(kept_active), errors=errors[-20:])


def temp_stage_cleanup_worker_loop() -> None:
    global TEMP_STAGE_CLEANUP_HEARTBEAT_TS
    while True:
        try:
            TEMP_STAGE_CLEANUP_HEARTBEAT_TS = time.time()
            cfg = queue_staging_cleanup_config()
            if cfg.get("enabled"):
                cleanup_old_temporary_stage_cache("hourly_stage_cleanup_worker")
            time.sleep(float(cfg.get("interval_seconds", 3600)))
        except Exception:
            time.sleep(60.0)


def ensure_temp_stage_cleanup_worker() -> None:
    global TEMP_STAGE_CLEANUP_WORKER_STARTED, TEMP_STAGE_CLEANUP_WORKER_THREAD
    if TEMP_STAGE_CLEANUP_WORKER_THREAD and TEMP_STAGE_CLEANUP_WORKER_THREAD.is_alive():
        TEMP_STAGE_CLEANUP_WORKER_STARTED = True
        return
    t = threading.Thread(target=temp_stage_cleanup_worker_loop, daemon=True, name="hourly_temp_stage_cleanup")
    TEMP_STAGE_CLEANUP_WORKER_THREAD = t
    TEMP_STAGE_CLEANUP_WORKER_STARTED = True
    t.start()


def state_tmp_cleanup_worker_loop() -> None:
    global STATE_TMP_CLEANUP_HEARTBEAT_TS
    while True:
        try:
            STATE_TMP_CLEANUP_HEARTBEAT_TS = time.time()
            cfg = load_config().get("state_storage", {}) or {}
            if bool(cfg.get("cleanup_orphan_temp_files", True)):
                max_age = int(cfg.get("temp_cleanup_max_age_seconds", 300) or 300)
                cleanup_orphan_board_state_temp_files(max_age, "state_tmp_cleanup_worker")
            interval = int(cfg.get("temp_cleanup_interval_seconds", 60) or 60)
            time.sleep(max(15, interval))
        except Exception:
            time.sleep(60.0)


def ensure_state_tmp_cleanup_worker() -> None:
    global STATE_TMP_CLEANUP_WORKER_STARTED, STATE_TMP_CLEANUP_WORKER_THREAD
    if STATE_TMP_CLEANUP_WORKER_THREAD and STATE_TMP_CLEANUP_WORKER_THREAD.is_alive():
        STATE_TMP_CLEANUP_WORKER_STARTED = True
        return
    t = threading.Thread(target=state_tmp_cleanup_worker_loop, daemon=True, name="board_state_tmp_cleanup")
    STATE_TMP_CLEANUP_WORKER_THREAD = t
    STATE_TMP_CLEANUP_WORKER_STARTED = True
    t.start()


def lightweight_upload_finalize_to_queue(job_id: str, spool: Dict[str, Any]) -> Dict[str, Any]:
    """Finalize a GUI upload into a runnable FIFO job.

    v5.10 stable path:
    - Keep the uploaded .v/.qsf/.sof in the temporary Pi spool until the job is finished/cancelled.
    - Do not compress/defer into a second staged-cache location.
    - Set verilog_local_path and sof_local_path immediately so the FIFO dispatcher can run now.
    - Permanent history is still only the lightweight text record on the Quartus server.

    This removes the previous failure mode where the GUI showed a 30-minute
    `uploading` receive deadline even though the upload had already completed.
    """
    v_path = Path(str(spool.get("spool_verilog_path") or ""))
    s_path = Path(str(spool.get("spool_sof_path") or ""))
    q_path = Path(str(spool.get("spool_qsf_path") or "")) if spool.get("spool_qsf_path") else None

    if not is_valid_verilog_file_path(v_path):
        return fail(
            "temporary Verilog spool file is missing after upload",
            job_id=job_id,
            path=str(v_path),
            hint="The HTTP upload did not finish writing the .v/.sv file. Submit again.",
        )
    if not is_valid_sof_file_path(s_path):
        return fail(
            "temporary SOF spool file is missing after upload",
            job_id=job_id,
            path=str(s_path),
            hint="The HTTP upload did not finish writing the .sof file. Submit again.",
        )
    if q_path is not None and not q_path.is_file():
        q_path = None

    def mutate(state: Dict[str, Any]):
        j = state.setdefault("jobs", {}).get(job_id, {})
        if not j:
            return fail("Unknown queue job after temporary upload stage", job_id=job_id)

        now_ts = time.time()
        j.update({
            "status": "queued",
            "kind": "upload",
            "message": "queued; uploaded files are ready for FIFO programming",
            "upload_stage": "queued_pi_spool_ready_for_programming",
            "upload_files_attached": True,
            "upload_files_in_progress": False,
            "upload_finished_at": now_iso(),
            "upload_finished_ts": now_ts,
            "spool_dir": spool.get("spool_dir", ""),
            "spool_verilog_path": str(v_path),
            "spool_sof_path": str(s_path),
            "spool_qsf_path": str(q_path) if q_path is not None else "",
            "verilog_local_path": str(v_path),
            "sof_local_path": str(s_path),
            "qsf_local_path": str(q_path) if q_path is not None else "",
            "verilog_filename": spool.get("verilog_filename", j.get("verilog_filename", "")),
            "sof_filename": spool.get("sof_filename", j.get("sof_filename", "")),
            "qsf_filename": spool.get("qsf_filename", j.get("qsf_filename", "")),
            "filename": spool.get("verilog_filename", j.get("filename", "design.v")),
            "verilog_size_bytes": int(spool.get("verilog_size_bytes", 0) or 0),
            "sof_size_bytes": int(spool.get("sof_size_bytes", 0) or 0),
            "qsf_size_bytes": int(spool.get("qsf_size_bytes", 0) or 0),
            "verilog_code": "",
            "qsf_text": "",
            "verilog_preview": spool.get("verilog_code", ""),
            "qsf_preview": spool.get("qsf_text", ""),
            "pi_file_storage": False,
            "temporary_pi_spool": True,
            "temporary_stage_cache": False,
            "no_pi_student_file_storage": True,
            "archive_policy": "lightweight_text_record_only_runtime_spool_until_finish",
            "history_policy": "small_text_record_only",
            "sof_source": "queued_pi_spool_runtime_passthrough",
            "staging_policy": "no_second_stage_direct_runtime_spool",
            "archive_disabled_by_lightweight_queue": True,
            "archive_retry_count": 0,
            "archive_attempt": 0,
            "wait_seconds": 0,
            "remaining_seconds": 0,
            "dispatch_not_before_ts": 0,
        })

        # Clear all stale upload/receive/stage fields that previously kept jobs
        # stuck in `uploading` for the 30-minute receive deadline.
        for key in (
            "receive_deadline_ts", "upload_deadline_at", "last_upload_error",
            "last_stage_wait_reason", "last_dispatch_block_reason",
            "stage_dir", "stage_manifest_path", "stage_verilog_tmp_path",
            "stage_sof_tmp_path", "stage_qsf_tmp_path", "stage_verilog_active_path",
            "stage_sof_active_path", "stage_qsf_active_path",
            "archive_thread_last_seen_at", "archive_thread_last_seen_ts", "archive_thread_started_at",
        ):
            j.pop(key, None)
        if str(j.get("planned_instance_id") or "") == "uploading":
            j.pop("planned_instance_id", None)
        if str(j.get("planned_slot") or "") == "uploading":
            j.pop("planned_slot", None)

        state.setdefault("jobs", {})[job_id] = j
        if job_id not in state.setdefault("queue", []):
            state["queue"].append(job_id)

        history = state.setdefault("history", [])
        history.append({
            "time": now_iso(),
            "event": "queue_upload_runtime_spool_ready",
            "board": j.get("requested_board") or j.get("target_board_hint") or "",
            "details": {
                "job_id": job_id,
                "student": j.get("student") or j.get("client_hostname") or "",
                "major": j.get("major") or "",
                "source_mode": j.get("source_mode") or j.get("submit_mode") or "",
                "verilog_local_path": str(v_path),
                "sof_local_path": str(s_path),
                "history_policy": j.get("history_policy"),
            },
        })
        if len(history) > 200:
            del history[:-200]

        state = annotate_queue_assignments(state)
        state = apply_teacher_override_for_job(state, job_id)
        state = annotate_queue_assignments(state)
        queued = state.setdefault("jobs", {}).get(job_id, j)
        return ok(
            job_id=job_id,
            cancel_token=queued.get("cancel_token", ""),
            status=queued.get("status", "queued"),
            job=public_queue_job(queued),
            queue_length=len(sorted_queued_job_ids(state)),
            queue_plan=state.get("queue_plan", {}),
            runtime_spool_ready=True,
        )

    final = update_state_atomic(mutate)
    try:
        hist_cfg = (load_config().get("server_history", {}) or {})
        if isinstance(final, dict) and final.get("success") and bool(hist_cfg.get("record_on_queue_accept", True)):
            latest = load_state().get("jobs", {}).get(job_id, {})
            final["server_history"] = write_job_history_immediate(job_id, latest, "job_queued")
    except Exception:
        pass
    try:
        ensure_queue_worker(force_restart_if_stalled=True)
        ensure_auto_repair_worker()
        wake_queue_worker("upload_finalize_runtime_spool_ready")
    except Exception:
        pass
    return final

def save_upload_files_to_temporary_spool(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """Save multipart upload to a temporary Pi spool folder quickly.

    v4.07 policy:
    - The Pi is still not permanent student storage.
    - The Pi only stages the HTTP body until the job is programmed.
    - The system writes a small text record instead of archiving full .v/.sof files.
    """
    spool_dir = upload_spool_root() / secure_filename(str(job_id))
    if spool_dir.exists():
        try:
            shutil.rmtree(spool_dir)
        except Exception:
            pass
    spool_dir.mkdir(parents=True, exist_ok=True)

    if "verilog_file" not in request.files:
        return fail("Upload attach requires multipart field verilog_file.")
    if "sof_file" not in request.files:
        return fail("Upload attach requires multipart field sof_file.")

    vf = request.files["verilog_file"]
    sf = request.files["sof_file"]
    qf = request.files.get("qsf_file")
    v_original = secure_filename(vf.filename or job.get("verilog_filename") or "design.v")
    sof_original = secure_filename(sf.filename or job.get("sof_filename") or "design.sof")
    qsf_original = secure_filename((qf.filename if qf else "") or job.get("qsf_filename") or "")
    if not v_original.lower().endswith((".v", ".sv")):
        return fail("verilog_file must end with .v or .sv", filename=v_original)
    if not sof_original.lower().endswith(".sof"):
        return fail("sof_file must end with .sof", filename=sof_original)
    if qf and not qsf_original.lower().endswith(".qsf"):
        return fail("qsf_file must end with .qsf", filename=qsf_original)

    v_path = spool_dir / v_original
    sof_path = spool_dir / sof_original
    qsf_path = spool_dir / qsf_original if qf else None
    try:
        vf.save(str(v_path))
        sf.save(str(sof_path))
        if qf and qsf_path is not None:
            qf.save(str(qsf_path))
    except Exception as e:
        return fail("Could not stage upload files on temporary Pi spool", error=str(e))

    v_size = int(v_path.stat().st_size) if v_path.exists() else 0
    sof_size = int(sof_path.stat().st_size) if sof_path.exists() else 0
    qsf_size = int(qsf_path.stat().st_size) if qsf_path and qsf_path.exists() else 0
    if v_size <= 0:
        return fail("Temporary Verilog spool file is empty", path=str(v_path))
    if sof_size <= 0:
        return fail("Temporary SOF spool file is empty", path=str(sof_path))

    limits = controller_runtime_limits()
    if v_size + sof_size + qsf_size > int(limits.get("max_upload_bytes", 64 * 1024 * 1024)):
        try:
            shutil.rmtree(spool_dir)
        except Exception:
            pass
        return fail(
            "Upload is larger than the Raspberry Pi safety limit.",
            total_upload_bytes=v_size + sof_size + qsf_size,
            max_upload_bytes=int(limits.get("max_upload_bytes", 64 * 1024 * 1024)),
        )

    verilog_code = limited_text_file_read(v_path, int(limits.get("max_inline_verilog_bytes", 256 * 1024)))
    qsf_text = str(request.form.get("qsf_text") or job.get("qsf_text") or "")
    if qsf_path and qsf_path.exists():
        qsf_text = limited_text_file_read(qsf_path, int(limits.get("max_inline_qsf_bytes", 128 * 1024)))

    return ok(
        spool_dir=str(spool_dir),
        spool_verilog_path=str(v_path),
        spool_sof_path=str(sof_path),
        spool_qsf_path=str(qsf_path) if qsf_path else "",
        verilog_filename=v_original,
        sof_filename=sof_original,
        qsf_filename=qsf_original,
        verilog_size_bytes=v_size,
        sof_size_bytes=sof_size,
        qsf_size_bytes=qsf_size,
        verilog_preview=verilog_code,
        qsf_text=qsf_text,
        temporary_pi_spool=True,
    )


def archive_local_spool_to_quartus_server(job_id: str, job: Dict[str, Any], spool: Dict[str, Any]) -> Dict[str, Any]:
    """Archive already-received temporary Pi files to Quartus server history."""
    v_path = Path(str(spool.get("spool_verilog_path") or job.get("spool_verilog_path") or ""))
    sof_path = Path(str(spool.get("spool_sof_path") or job.get("spool_sof_path") or ""))
    v_original = secure_filename(str(spool.get("verilog_filename") or job.get("verilog_filename") or v_path.name or "design.v"))
    sof_original = secure_filename(str(spool.get("sof_filename") or job.get("sof_filename") or sof_path.name or "design.sof"))
    if not v_path.exists():
        return fail("Temporary Verilog spool file is missing; ask GUI to retry upload", path=str(v_path))
    if not sof_path.exists():
        return fail("Temporary SOF spool file is missing; ask GUI to retry upload", path=str(sof_path))

    remote_dir, prefix, student_folder = history_bundle_prefix(job, job_id)
    remote_v = f"{remote_dir}/{prefix}_{v_original}"
    remote_sof = f"{remote_dir}/{prefix}_{sof_original}"
    v_size = int(v_path.stat().st_size)
    sof_size = int(sof_path.stat().st_size)
    limits = controller_runtime_limits()
    verilog_code = str(spool.get("verilog_code") or job.get("verilog_preview") or "")
    if not verilog_code:
        verilog_code = limited_text_file_read(v_path, int(limits.get("max_inline_verilog_bytes", 256 * 1024)))

    # v4.10: archive copy is supervised and bounded. A crashed/stalled archive
    # must never leave the GUI job permanently in "uploading" with no explanation.
    hist_cfg = (load_config().get("server_history", {}) or {})
    lock_timeout = int(hist_cfg.get("archive_lock_timeout_seconds", 30) or 30)
    io_timeout = int(hist_cfg.get("archive_io_timeout_seconds", 120) or 120)
    got_lock = SERVER_ARCHIVE_LOCK.acquire(timeout=max(1, lock_timeout))
    if not got_lock:
        return fail("Quartus-server archive is busy; retrying shortly", retryable=True, archive_lock_timeout_seconds=lock_timeout)
    try:
        ssh = connect_server()
        try:
            try:
                transport = ssh.get_transport()
                if transport:
                    transport.set_keepalive(10)
            except Exception:
                pass
            stdin, stdout, stderr = ssh.exec_command(f"mkdir -p {shlex.quote(remote_dir)}", timeout=io_timeout)
            code = stdout.channel.recv_exit_status()
            if code != 0:
                err = stderr.read().decode("utf-8", errors="ignore")
                return fail("Unable to create server history folder", remote_dir=remote_dir, stderr=err, retryable=True)
            sftp = ssh.open_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(io_timeout)
                except Exception:
                    pass
                with open(v_path, "rb") as src, sftp.file(remote_v, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                with open(sof_path, "rb") as src, sftp.file(remote_sof, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
        finally:
            try:
                ssh.close()
            except Exception:
                pass
    except Exception as e:
        return fail(
            "Quartus-server archive exception; retrying",
            retryable=True,
            error=str(e),
            exception_type=type(e).__name__,
        )
    finally:
        try:
            SERVER_ARCHIVE_LOCK.release()
        except Exception:
            pass

    return ok(
        archive_policy="quartus_server_history_direct_no_pi_storage_temp_spool",
        archived_dir=remote_dir,
        student_folder=student_folder,
        archived_verilog_path=remote_v,
        archived_sof_path=remote_sof,
        verilog_filename=v_original,
        sof_filename=sof_original,
        verilog_size_bytes=v_size,
        sof_size_bytes=sof_size,
        verilog_code=verilog_code,
        remote_sof=remote_sof,
        no_pi_student_file_storage=True,
        temporary_pi_spool=True,
    )


def finalize_archived_upload_success(job_id: str, archive: Dict[str, Any]) -> Dict[str, Any]:
    """Move an uploaded job from uploading to queued after server archive succeeds."""
    def finalize_upload_state(state: Dict[str, Any]):
        current = state.setdefault("jobs", {}).get(job_id, {})
        if not current:
            return fail("Unknown queue job during archive finalize", job_id=job_id)
        current_status = str(current.get("status") or "").lower()
        merged = dict(current)
        test_seconds = int(merged.get("test_seconds", 0) or 0)
        requested_board = merged.get("requested_board") or ""
        archived_v = archive.get("archived_verilog_path", "")
        archived_sof = archive.get("archived_sof_path", "")
        merged.update({
            "kind": "server_paths",
            "message": "queued; files archived on Quartus server",
            "upload_finished_at": now_iso(),
            "upload_finished_ts": time.time(),
            "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds),
            "upload_files_attached": True,
            "upload_files_in_progress": False,
            "upload_stage": "archive_complete",
            "pi_file_storage": False,
            "no_pi_student_file_storage": True,
            "archive_policy": archive.get("archive_policy"),
            "archived_dir": archive.get("archived_dir"),
            "archived_verilog_path": archived_v,
            "archived_sof_path": archived_sof,
            "verilog_path": archived_v,
            "sof_path": archived_sof,
            "remote_sof": archived_sof,
            "filename": archive.get("verilog_filename") or Path(archived_v).name,
            "sof_filename": archive.get("sof_filename") or Path(archived_sof).name,
            "sof_source": "quartus_server_history_archive_passthrough",
            "verilog_size_bytes": archive.get("verilog_size_bytes", 0),
            "sof_size_bytes": archive.get("sof_size_bytes", 0),
        })
        if archive.get("verilog_code"):
            merged["verilog_code"] = archive.get("verilog_code")
        merged.pop("receive_deadline_ts", None)
        if str(merged.get("planned_instance_id") or "") == "uploading":
            merged.pop("planned_instance_id", None)
        # If a rare late archive completes after the job was already cancelled/failed/completed,
        # keep that terminal status but still preserve the archive paths.
        if current_status not in ("running", "testing", "completed", "cancelled", "failed"):
            merged["status"] = "queued"
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
        state.setdefault("jobs", {})[job_id] = merged
        state = annotate_queue_assignments(state)
        state = apply_teacher_override_for_job(state, job_id)
        state = annotate_queue_assignments(state)
        updated = state.setdefault("jobs", {}).get(job_id, merged)
        history = state.setdefault("history", [])
        history.append({"time": now_iso(), "event": "queue_upload_archived_complete_async", "board": updated.get("requested_board") or updated.get("planned_board") or "", "details": {"job_id": job_id, "kind": updated.get("kind"), "student": updated.get("student"), "archived_verilog_path": updated.get("archived_verilog_path", ""), "archived_sof_path": updated.get("archived_sof_path", "")}})
        if len(history) > 200:
            del history[:-200]
        return ok(job_id=job_id, cancel_token=updated.get("cancel_token", ""), status=updated.get("status", "queued"), job=public_queue_job(updated), queue_length=len(sorted_queued_job_ids(state)), queue_plan=state.get("queue_plan", {}))
    return update_state_atomic(finalize_upload_state)


def _archive_thread_alive(job_id: str) -> bool:
    with ARCHIVE_JOB_THREADS_LOCK:
        t = ARCHIVE_JOB_THREADS.get(str(job_id))
        return bool(t and t.is_alive())


def _archive_thread_count() -> int:
    with ARCHIVE_JOB_THREADS_LOCK:
        dead = [jid for jid, t in ARCHIVE_JOB_THREADS.items() if not t.is_alive()]
        for jid in dead:
            ARCHIVE_JOB_THREADS.pop(jid, None)
        return len(ARCHIVE_JOB_THREADS)


def archive_spooled_upload_runner(job_id: str, job_snapshot: Dict[str, Any], spool: Dict[str, Any]) -> None:
    """Background archive worker. System-side archive errors do not fail the job."""
    try:
        max_retry_seconds = int((load_config().get("server_history", {}) or {}).get("archive_retry_sleep_seconds", 5) or 5)
        attempt = 0
        while True:
            attempt += 1
            # Stop retrying if the user cancelled the job.
            current_state = load_state()
            current = current_state.setdefault("jobs", {}).get(job_id, {})
            current_status = str(current.get("status") or "").lower()
            if current_status == "cancelled":
                break
            # v5.02: the new lightweight queue mode does not archive full files
            # to the Quartus server.  If a stale archive thread exists from a
            # previous attempt, it must not downgrade a queued job to uploading.
            if _is_lightweight_tmp_handoff_job(current):
                if current_status in ("queued", "running", "testing", "completed", "failed") or _has_valid_tmp_stage(current):
                    if current_status in ("receiving", "uploading") and _has_valid_tmp_stage(current):
                        def mark_lightweight_ready(state: Dict[str, Any]):
                            j = state.setdefault("jobs", {}).get(job_id, {})
                            if j and _repair_lightweight_stage_to_queued(job_id, j):
                                state.setdefault("jobs", {})[job_id] = j
                                if job_id not in state.setdefault("queue", []):
                                    state["queue"].append(job_id)
                            return ok(job_id=job_id, stage="lightweight_archive_thread_exit")
                        update_state_atomic(mark_lightweight_ready)
                    break
            def mark_archiving(state: Dict[str, Any]):
                j = state.setdefault("jobs", {}).get(job_id, {})
                if j and str(j.get("status") or "").lower() not in ("cancelled", "completed", "failed", "queued", "running", "testing") and not _is_lightweight_tmp_handoff_job(j):
                    j["status"] = "uploading"
                    j["upload_stage"] = "archiving_to_server"
                    j["archive_attempt"] = attempt
                    j["archive_thread_started_at"] = j.get("archive_thread_started_at") or now_iso()
                    j["archive_thread_last_seen_at"] = now_iso()
                    j["archive_thread_last_seen_ts"] = time.time()
                    j["upload_files_in_progress"] = False
                    j["planned_instance_id"] = "uploading"
                    j["message"] = f"upload received; archiving to Quartus server (attempt {attempt})"
                    state.setdefault("jobs", {})[job_id] = j
                    if job_id not in state.setdefault("queue", []):
                        state["queue"].append(job_id)
                return ok(job_id=job_id, stage="archiving_to_server", attempt=attempt)
            update_state_atomic(mark_archiving)
            try:
                result = archive_local_spool_to_quartus_server(job_id, current or job_snapshot, spool)
            except Exception as e:
                result = fail("Quartus-server archive worker exception; retrying", retryable=True, error=str(e), exception_type=type(e).__name__)
            if result.get("success"):
                final = finalize_archived_upload_success(job_id, result)
                # Remove temporary Pi spool only after the server archive is sealed.
                try:
                    sd = Path(str(spool.get("spool_dir") or current.get("spool_dir") or ""))
                    if sd.exists() and sd.is_dir():
                        shutil.rmtree(sd)
                except Exception:
                    pass
                add_history("async_archive_success", current.get("requested_board") or current.get("target_board_hint") or "", {"job_id": job_id, "attempt": attempt, "final_status": final.get("status")})
                break

            # Do not fail student job for SSH/SFTP/archive system problems. Keep visible and retry.
            def mark_retry(state: Dict[str, Any]):
                j = state.setdefault("jobs", {}).get(job_id, {})
                if j and _is_lightweight_tmp_handoff_job(j):
                    _repair_lightweight_stage_to_queued(job_id, j)
                    state.setdefault("jobs", {})[job_id] = j
                    if job_id not in state.setdefault("queue", []):
                        state["queue"].append(job_id)
                    return ok(job_id=job_id, retry=False, skipped="lightweight_tmp_handoff")
                if j and str(j.get("status") or "").lower() not in ("cancelled", "completed", "failed"):
                    j["status"] = "uploading"
                    j["upload_stage"] = "archive_retry"
                    j["upload_files_in_progress"] = False
                    j["archive_retry_count"] = int(j.get("archive_retry_count", 0) or 0) + 1
                    j["last_upload_error"] = result.get("error", "archive retry")
                    j["message"] = f"upload received; archive retry {j['archive_retry_count']} to Quartus server: {j['last_upload_error']}"
                    j["remaining_seconds"] = int(max_retry_seconds)
                    j["wait_seconds"] = 0
                    j["planned_instance_id"] = "uploading"
                    state.setdefault("jobs", {})[job_id] = j
                    if job_id not in state.setdefault("queue", []):
                        state["queue"].append(job_id)
                return ok(job_id=job_id, retry=True)
            retry_mark = update_state_atomic(mark_retry)
            if isinstance(retry_mark, dict) and retry_mark.get("retry") is False:
                # v5.04: lightweight temp-stage jobs are already queued/repaired.
                # Do not keep a legacy archive loop alive that can confuse status.
                break
            time.sleep(max(2, min(60, max_retry_seconds)))
    finally:
        with ARCHIVE_JOB_THREADS_LOCK:
            ARCHIVE_JOB_THREADS.pop(str(job_id), None)


def start_archive_spooled_upload_thread(job_id: str, job: Dict[str, Any], spool: Dict[str, Any]) -> bool:
    jid = str(job_id)
    with ARCHIVE_JOB_THREADS_LOCK:
        t = ARCHIVE_JOB_THREADS.get(jid)
        if t and t.is_alive():
            return False
        t = threading.Thread(target=archive_spooled_upload_runner, args=(jid, dict(job), dict(spool)), daemon=True, name=f"archive_upload_{jid}")
        ARCHIVE_JOB_THREADS[jid] = t
        t.start()
        return True


def _archive_submission_to_quartus_server_locked(
    job_id: str,
    job: Dict[str, Any],
    *,
    has_verilog_file: bool,
    has_sof_file: bool,
    form_verilog_path: str = "",
    form_sof_path: str = "",
) -> Dict[str, Any]:
    """
    Archive the submitted .v/.sv and .sof directly on the Quartus server.

    v4.01 policy:
    - The Pi is controller/AI only.
    - Local GUI file uploads are received by Flask and streamed/written to the
      Quartus server history folder immediately.
    - The Pi does not keep permanent student .v/.sof files.
    - The archived .sof path is the exact file sent to quartus_pgm.
    - No SHA/stable-size/min-size verification is performed; Quartus decides.
    """
    remote_dir, prefix, student_folder = history_bundle_prefix(job, job_id)

    v_original = ""
    sof_original = ""
    verilog_code = ""
    remote_v = ""
    remote_sof = ""
    v_size = 0
    sof_size = 0

    ssh = connect_server()
    try:
        stdin, stdout, stderr = ssh.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            err = stderr.read().decode("utf-8", errors="ignore")
            return fail("Unable to create server history folder", remote_dir=remote_dir, stderr=err)

        sftp = ssh.open_sftp()
        try:
            # Verilog/SystemVerilog source
            if has_verilog_file:
                vf = request.files["verilog_file"]
                v_original = secure_filename(vf.filename or job.get("verilog_filename") or "design.v")
                if not v_original.lower().endswith((".v", ".sv")):
                    return fail("verilog_file must end with .v or .sv", filename=v_original)
                data = vf.read()
                v_size = len(data)
                verilog_code = data.decode("utf-8", errors="ignore")
                remote_v = f"{remote_dir}/{prefix}_{v_original}"
                with sftp.file(remote_v, "wb") as f:
                    f.write(data)
            else:
                if not form_verilog_path.lower().endswith((".v", ".sv")):
                    return fail("verilog_path must point to a .v or .sv file on the server.", verilog_path=form_verilog_path)
                if not remote_path_allowed(form_verilog_path):
                    return fail("verilog_path is outside allowed server project/history folders.", verilog_path=form_verilog_path)
                if not remote_file_exists(form_verilog_path):
                    return fail("Remote Verilog file does not exist.", verilog_path=form_verilog_path)
                v_original = secure_filename(Path(form_verilog_path).name or "server_design.v")
                remote_v = f"{remote_dir}/{prefix}_{v_original}"
                cp_cmd = f"cp {shlex.quote(form_verilog_path)} {shlex.quote(remote_v)}"
                stdin, stdout, stderr = ssh.exec_command(cp_cmd)
                code = stdout.channel.recv_exit_status()
                if code != 0:
                    return fail("Unable to copy server Verilog into history", source=form_verilog_path, dest=remote_v, stderr=stderr.read().decode("utf-8", errors="ignore"))
                try:
                    verilog_code = read_remote_text(remote_v)
                except Exception:
                    verilog_code = ""
                try:
                    v_size = int(sftp.stat(remote_v).st_size)
                except Exception:
                    v_size = 0

            # SOF programming file
            if has_sof_file:
                sf = request.files["sof_file"]
                sof_original = secure_filename(sf.filename or job.get("sof_filename") or "design.sof")
                if not sof_original.lower().endswith(".sof"):
                    return fail("Uploaded programming file must end with .sof", filename=sof_original)
                remote_sof = f"{remote_dir}/{prefix}_{sof_original}"
                try:
                    sf.stream.seek(0)
                except Exception:
                    pass
                sftp.putfo(sf.stream, remote_sof)
                try:
                    sof_size = int(sftp.stat(remote_sof).st_size)
                except Exception:
                    sof_size = 0
            else:
                if not form_sof_path.lower().endswith(".sof"):
                    return fail("sof_path must point to a .sof file.", sof_path=form_sof_path)
                if not remote_path_allowed(form_sof_path):
                    return fail("sof_path is outside allowed server project/history folders.", sof_path=form_sof_path)
                # Keep this as a fast existence guard for a server path; no SOF validation.
                if not remote_file_exists(form_sof_path):
                    return fail("Remote SOF file does not exist.", sof_path=form_sof_path)
                sof_original = secure_filename(Path(form_sof_path).name or "design.sof")
                remote_sof = f"{remote_dir}/{prefix}_{sof_original}"
                cp_cmd = f"cp {shlex.quote(form_sof_path)} {shlex.quote(remote_sof)}"
                stdin, stdout, stderr = ssh.exec_command(cp_cmd)
                code = stdout.channel.recv_exit_status()
                if code != 0:
                    return fail("Unable to copy server SOF into history", source=form_sof_path, dest=remote_sof, stderr=stderr.read().decode("utf-8", errors="ignore"))
                try:
                    sof_size = int(sftp.stat(remote_sof).st_size)
                except Exception:
                    sof_size = 0
        finally:
            sftp.close()
    finally:
        ssh.close()

    return ok(
        archive_policy="quartus_server_history_direct_no_pi_storage",
        archived_dir=remote_dir,
        student_folder=student_folder,
        archived_verilog_path=remote_v,
        archived_sof_path=remote_sof,
        verilog_filename=v_original,
        sof_filename=sof_original,
        verilog_code=verilog_code,
        verilog_size_bytes=v_size,
        sof_size_bytes=sof_size,
    )


def is_lock_expired(lock: Dict[str, Any]) -> bool:
    cfg = load_config()
    locked_at = float(lock.get("locked_at", 0) or 0)
    expected = int(lock.get("expected_seconds", 0) or 0)
    timeout = expected if expected > 0 else int(cfg.get("lock_timeout_seconds", 600))
    return bool(locked_at and (time.time() - locked_at > timeout))


def default_test_minutes() -> int:
    cfg = load_config()
    testing = cfg.get("testing", {}) or {}
    return int(testing.get("default_test_minutes", 5) or 5)


def max_test_minutes() -> int:
    cfg = load_config()
    testing = cfg.get("testing", {}) or {}
    return int(testing.get("max_test_minutes", 60) or 60)


def sanitize_test_minutes(value: Any) -> int:
    """Clamp requested student test time to a safe range."""
    default = default_test_minutes()
    max_minutes = max(1, max_test_minutes())
    try:
        minutes = int(float(value))
    except Exception:
        minutes = default
    if minutes < 0:
        minutes = 0
    if minutes > max_minutes:
        minutes = max_minutes
    return minutes


def test_seconds_from_value(value: Any) -> int:
    return sanitize_test_minutes(value) * 60



def complete_testing_job_in_state(state: Dict[str, Any], job_id: str, job: Dict[str, Any], reason: str = "test_timer_expired") -> bool:
    """
    Complete a testing job and release/clear its physical JTAG slot inside the same state object.

    This is intentionally state-local: it avoids the stale-state overwrite bug where a slot
    is released in one save and then re-marked busy by another old state save.
    """
    if not job or job.get("status") != "testing":
        return False

    now_ts = time.time()
    board = job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or ""
    cable = job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or ""
    lock_key = job.get("selected_lock_key") or (instance_lock_key(board, cable) if board and cable else "")

    if lock_key:
        state.setdefault("locks", {})[lock_key] = {
            "busy": False,
            "released_at": now_ts,
            "released_at_iso": now_iso(),
            "reason": f"{reason}_slot_cleared",
            "board": board,
            "detected_cable": cable,
            "lock_key": lock_key,
            "cleared": True,
            "clear_mode": "timer_expiration_logical_clear",
        }

    state.setdefault("slot_clear_events", []).append({
        "cleared_at": now_iso(),
        "cleared_ts": now_ts,
        "board": board,
        "detected_cable": cable,
        "lock_key": lock_key,
        "job_id": job_id,
        "reason": f"{reason}_slot_cleared",
        "clear_mode": "timer_expiration_logical_clear",
    })
    if len(state.get("slot_clear_events", [])) > 100:
        del state["slot_clear_events"][:-100]

    job["status"] = "completed"
    job["finished_at"] = now_iso()
    job["finished_ts"] = now_ts
    job["test_finished_at"] = job["finished_at"]
    job["test_finished_ts"] = now_ts
    job["slot_cleared_at"] = job["finished_at"]
    job["cleared_reason"] = reason
    job["remaining_seconds"] = 0
    if job.get("started_ts"):
        try:
            job["elapsed_seconds"] = max(0, int(now_ts - float(job.get("started_ts", 0) or 0)))
        except Exception:
            pass
    job["message"] = f"test timer complete; slot cleared and released {job.get('jtag_instance') or job.get('planned_instance_id') or 'JTAG'} | {cable}"

    # v4.44: history is written asynchronously and uses one stable filename per
    # job_id, so completing the test updates the existing record without delaying
    # queue/timer cleanup or creating a duplicate file.
    try:
        job["server_history_async_requested"] = write_job_history_to_server_async(job_id, dict(job), "test_timer_completed")
        job["server_history_pending_event"] = "test_timer_completed"
    except Exception as e:
        job["server_history"] = fail(f"server history logging failed: {e}")

    try:
        if job.get("temporary_pi_spool"):
            job["temporary_spool_cleanup"] = cleanup_temporary_spool_for_job(job)
    except Exception as e:
        job["temporary_spool_cleanup"] = fail(f"temporary spool cleanup failed: {e}")
    try:
        if job.get("temporary_stage_cache"):
            job["temporary_stage_cleanup"] = cleanup_staged_files_for_job(job)
    except Exception as e:
        job["temporary_stage_cleanup"] = fail(f"temporary stage cleanup failed: {e}")

    state.setdefault("jobs", {})[job_id] = job
    _record_recent_job(state, job_id)

    try:
        if board and board in load_config().get("board_catalog", {}):
            set_led(load_config().get("board_catalog", {}).get(board, {}), busy=False, ready=True)
    except Exception:
        pass

    return True


def cleanup_expired_testing_jobs_by_job_timer(state: Dict[str, Any]) -> bool:
    """
    Safety net for test timer expiration.

    If the lock object is missing/stale but the job's test_end_ts has passed, complete the job,
    clear the slot, and let the queue worker move the next waiting job forward.
    """
    now_ts = time.time()
    changed = False
    for job_id, job in list(state.setdefault("jobs", {}).items()):
        if not job or job.get("status") != "testing":
            continue
        test_end = float(job.get("test_end_ts", 0) or 0)
        if not test_end:
            start = float(job.get("test_start_ts", 0) or 0)
            seconds = int(job.get("test_seconds", job.get("estimated_seconds", 0)) or 0)
            if start and seconds:
                test_end = start + seconds
                job["test_end_ts"] = test_end
                job["test_end_at"] = iso_from_ts(test_end)
        if test_end and now_ts >= test_end:
            changed = complete_testing_job_in_state(state, job_id, job, reason="test_timer_expired")
    return changed


def cleanup_expired_locks() -> None:
    """Release expired locks and complete testing jobs when their test timer ends."""
    state = load_state()
    changed = False
    now_ts = time.time()
    for key, lock in list(state.get("locks", {}).items()):
        if lock.get("busy") and is_lock_expired(lock):
            board = lock.get("board") or key
            cable = lock.get("detected_cable", "")
            job_id = lock.get("job_id", "")
            phase = lock.get("phase", "")
            active_job = state.get("jobs", {}).get(str(job_id), {}) if job_id else {}
            if phase != "testing" and isinstance(active_job, dict) and str(active_job.get("status") or "").lower() == "running":
                lock["expected_seconds"] = programming_lock_expected_seconds(board, int(active_job.get("test_seconds", 0) or 0))
                lock["extended_at"] = now_iso()
                lock["reason"] = "running_job_lock_extended"
                state.setdefault("locks", {})[key] = lock
                changed = True
                continue
            clear_reason = "test_timer_expired_slot_cleared" if phase == "testing" else "timeout_slot_cleared"
            state["locks"][key] = {
                "busy": False,
                "released_at": now_ts,
                "released_at_iso": now_iso(),
                "reason": clear_reason,
                "board": board,
                "detected_cable": cable,
                "lock_key": key,
                "cleared": True,
                "clear_mode": "timer_expiration_logical_clear",
            }
            state.setdefault("slot_clear_events", []).append({
                "cleared_at": now_iso(),
                "cleared_ts": now_ts,
                "board": board,
                "detected_cable": cable,
                "lock_key": key,
                "job_id": job_id,
                "reason": clear_reason,
                "clear_mode": "timer_expiration_logical_clear",
            })
            if len(state.get("slot_clear_events", [])) > 100:
                del state["slot_clear_events"][:-100]
            if phase == "testing" and job_id:
                job = state.setdefault("jobs", {}).get(job_id)
                if job and job.get("status") == "testing":
                    complete_testing_job_in_state(state, job_id, job, reason="test_timer_expired")
            changed = True

    # Second safety check: a job can be testing even if the lock object was stale/missing.
    if cleanup_expired_testing_jobs_by_job_timer(state):
        changed = True

    if changed:
        state = annotate_queue_assignments(state) if "annotate_queue_assignments" in globals() else state
        save_state_preserving_concurrent_jobs(state)


def _board_is_heavy_for_estimate(board: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Return True for boards whose programming/JTAG path is normally slower."""
    cfg = cfg or load_config()
    catalog = cfg.get("board_catalog", {}) or {}
    bcfg = catalog.get(board, {}) if board else {}
    family = str(bcfg.get("quartus_family", "") or "").lower()
    board_l = str(board or "").lower()
    features = set(bcfg.get("features", []) or [])
    return (
        family == "pro"
        or "agilex" in board_l
        or bool(features & {"pcie", "ddr4", "qsfp", "high_speed", "transceivers", "advanced"})
    )


def _clamp_program_estimate(seconds: float, board: str, cfg: Dict[str, Any]) -> int:
    qe = cfg.get("queue_estimates", {}) or {}
    is_heavy = _board_is_heavy_for_estimate(board, cfg)
    min_s = int(qe.get("min_program_seconds", 1) or 1)
    if is_heavy:
        max_s = int(qe.get("max_pro_program_seconds", qe.get("pro_program_estimate_ceiling_seconds", 300)) or 300)
    else:
        max_s = int(qe.get("max_standard_program_seconds", qe.get("standard_program_estimate_ceiling_seconds", 60)) or 60)
    if max_s < min_s:
        max_s = min_s
    raw = float(seconds or min_s)
    rounding = str(qe.get("estimate_rounding", "nearest") or "nearest").lower()
    if rounding == "ceil":
        est = math.ceil(raw)
    elif rounding == "floor":
        est = math.floor(raw)
    else:
        est = int(raw + 0.5)
    return int(max(min_s, min(max_s, est)))


def _dynamic_program_estimate_from_usage(board: str, cfg: Dict[str, Any]) -> Optional[int]:
    """Estimate programming seconds from real successful JTAG runs saved in board_state.json.

    This removes the old hard-coded 10/180 second visible queue estimate.  The
    first job uses a configurable bootstrap value; after successful programs the
    estimate follows the measured average per board family/slot.
    """
    qe = cfg.get("queue_estimates", {}) or {}
    if not bool(qe.get("dynamic_program_estimates", True)):
        return None
    try:
        state = load_state()
        usage = state.get("jtag_usage", {}) or {}
        samples: List[float] = []
        for _key, rec in usage.items():
            if not isinstance(rec, dict):
                continue
            rec_board = str(rec.get("board") or "")
            if board and rec_board and rec_board != board:
                continue
            success_count = int(rec.get("success_count", rec.get("program_count", 0)) or 0)
            total_seconds = float(rec.get("total_program_seconds", 0) or 0)
            last_seconds = float(rec.get("last_program_seconds", 0) or 0)
            if success_count > 0 and total_seconds > 0:
                samples.append(total_seconds / max(1, success_count))
            elif bool(rec.get("last_success")) and last_seconds > 0:
                samples.append(last_seconds)
        if not samples:
            return None
        # Recent/slot average. Use the fastest successful matching board path plus
        # a small configurable buffer so the GUI does not overstate queue time.
        measured = min(samples)
        buffer_s = float(qe.get("measured_success_buffer_seconds", 1) or 0)
        return _clamp_program_estimate(measured + buffer_s, board, cfg)
    except Exception:
        return None


def estimate_program_seconds(board: str = "") -> int:
    """Estimate only the visible programming phase.

    v4.41: this estimate is dynamic.  It uses real successful programming times
    from jtag_usage when available instead of hard-coded DE1/Agilex constants.
    It is still only a GUI/queue-planning estimate; the real timeout remains in
    quartus_server.*_program_timeout_seconds.
    """
    cfg = load_config()
    dyn = _dynamic_program_estimate_from_usage(board, cfg)
    if dyn is not None:
        return dyn

    qe = cfg.get("queue_estimates", {}) or {}
    is_heavy = _board_is_heavy_for_estimate(board, cfg)
    if is_heavy:
        base = qe.get("bootstrap_pro_program_seconds", qe.get("pro_min_program_seconds", 60))
    else:
        base = qe.get("bootstrap_standard_program_seconds", qe.get("standard_min_program_seconds", 1))
    return _clamp_program_estimate(float(base or 1), board, cfg)


def estimate_deploy_seconds(board: str = "", test_seconds: int = 0) -> int:
    """
    Estimate total physical-slot occupancy for planning:
    programming phase + student test/session timer.
    """
    return estimate_program_seconds(board) + max(0, int(test_seconds or 0))


def quartus_program_timeout_for_board(board: str = "") -> int:
    """Board-aware hard timeout for the actual quartus_pgm command.

    v4.14: do not use the Agilex/global 900 second timeout for DE1-SoC.
    Standard boards normally program in a few seconds; a short board-aware
    timeout prevents stuck running rows while still giving Quartus enough time.
    """
    cfg = load_config()
    qs = (cfg.get("quartus_server", {}) or {})
    catalog = (cfg.get("board_catalog", {}) or {})
    bcfg = catalog.get(str(board or ""), {}) or {}
    family = str(bcfg.get("quartus_family", "") or "").lower()
    board_l = str(board or "").lower()
    if family == "pro" or "agilex" in board_l:
        return int(qs.get("pro_program_timeout_seconds", qs.get("program_timeout_seconds", 900)) or 900)
    return int(qs.get("standard_program_timeout_seconds", 120) or 120)


def sof_copy_timeout_for_board(board: str = "") -> int:
    cfg = load_config()
    qs = (cfg.get("quartus_server", {}) or {})
    catalog = (cfg.get("board_catalog", {}) or {})
    bcfg = catalog.get(str(board or ""), {}) or {}
    family = str(bcfg.get("quartus_family", "") or "").lower()
    board_l = str(board or "").lower()
    if family == "pro" or "agilex" in board_l:
        return int(qs.get("pro_sof_copy_timeout_seconds", qs.get("sof_copy_timeout_seconds", 180)) or 180)
    return int(qs.get("standard_sof_copy_timeout_seconds", 90) or 90)


def programming_lock_expected_seconds(board: str = "", test_seconds: int = 0) -> int:
    """Return the physical JTAG lock lifetime while SOF copy/quartus_pgm runs.

    v4.14: running locks must describe only the programming phase, not the
    later student test timer. The test timer creates its own testing lock after
    Quartus succeeds. This prevents rows from sitting in running for 15+ minutes
    when the board should either enter testing or be requeued.
    """
    try:
        grace = min(max(strict_running_watchdog_grace_seconds(), 15), 60)
    except Exception:
        grace = 30
    copy_timeout = sof_copy_timeout_for_board(board)
    program_timeout = quartus_program_timeout_for_board(board)
    visible_estimate = estimate_program_seconds(board)
    base = max(visible_estimate + grace, copy_timeout + program_timeout + grace)
    if "agilex" in str(board or "").lower():
        base = max(base, int((strict_resource_engine_config() or {}).get("agilex_programming_watchdog_min_seconds", 900) or 900))
    else:
        base = min(base, int((strict_resource_engine_config() or {}).get("standard_programming_watchdog_max_seconds", 240) or 240))
    return int(max(60, base))


def iso_from_ts(ts: float) -> str:
    try:
        if not ts:
            return ""
        return _dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except Exception:
        return ""


def countdown_seconds_until(deadline_ts: float = 0.0, now_ts: float = 0.0) -> int:
    """Return a display countdown using one shared server timestamp.

    v4.12: use ceil() and the same snapshot_now for all rows so a queued
    job's Wait and the active job's Remaining value do not drift by one
    second because two different time.time() calls rounded differently.
    """
    try:
        deadline = float(deadline_ts or 0.0)
        now = float(now_ts or time.time())
    except Exception:
        return 0
    if deadline <= 0:
        return 0
    return int(max(0, math.ceil(deadline - now)))


def timing_fields(start_ts: float = 0.0, expected_seconds: int = 0, active: bool = False, end_ts: float = 0.0, eta_base_ts: float = 0.0, now_ts: float = 0.0) -> Dict[str, Any]:
    now = float(now_ts or time.time())
    elapsed = 0
    remaining = 0
    finish_ts = 0.0
    if end_ts:
        finish_ts = float(end_ts)
    elif eta_base_ts and expected_seconds:
        finish_ts = float(eta_base_ts) + int(expected_seconds or 0)
    elif start_ts and expected_seconds:
        finish_ts = float(start_ts) + int(expected_seconds or 0)

    if start_ts:
        ref = now if active or not end_ts else float(end_ts)
        elapsed = max(0, int(math.floor(ref - float(start_ts))))

    if finish_ts:
        remaining = countdown_seconds_until(finish_ts, now)
    elif expected_seconds:
        remaining = max(0, int(expected_seconds) - elapsed)

    return {
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "expected_seconds": int(expected_seconds or 0),
        "estimated_finish_ts": finish_ts,
        "estimated_finish_at": iso_from_ts(finish_ts),
    }




def running_deadline_ts_for_job(state: Dict[str, Any], job: Dict[str, Any], now_ts: float = 0.0) -> float:
    """Return the real logical deadline for a running job.

    v4.13: the visible programming estimate can reach 00:00 while the physical
    JTAG slot is still reserved by the hard Quartus/SFTP timeout. Use the same
    lock deadline that the JTAG table uses so Queue Remain and JTAG Busy Time
    stay synchronized instead of showing running 00:00.
    """
    try:
        board = str(job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or "")
        cable = str(job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or "")
        key = str(job.get("selected_lock_key") or job.get("planned_lock_key") or "")
        if not key and board and cable:
            key = instance_lock_key(board, cable)
        lock = (state.get("locks") or {}).get(key, {}) if key else {}
        if isinstance(lock, dict) and lock.get("busy") and str(lock.get("job_id") or "") == str(job.get("job_id") or ""):
            locked_at = float(lock.get("locked_at", 0) or 0)
            expected = int(lock.get("expected_seconds", 0) or 0)
            if locked_at and expected:
                return locked_at + expected
        started = float(job.get("started_ts", 0) or 0)
        board_for_timer = board or str(job.get("requested_board") or "")
        program_expected = int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(board_for_timer))
        program_timeout = quartus_program_timeout_for_board(board_for_timer)
        copy_timeout = sof_copy_timeout_for_board(board_for_timer)
        grace = min(max(strict_running_watchdog_grace_seconds(), 15), 60)
        expected = max(program_expected, copy_timeout + program_timeout + grace)
        if "agilex" in board_for_timer.lower():
            expected = max(expected, int((strict_resource_engine_config() or {}).get("agilex_programming_watchdog_min_seconds", 900) or 900))
        else:
            expected = min(expected, int((strict_resource_engine_config() or {}).get("standard_programming_watchdog_max_seconds", 240) or 240))
        return started + expected if started else 0.0
    except Exception:
        return 0.0


def should_requeue_for_system_transient(result: Dict[str, Any]) -> bool:
    """System-side transfer/runner problems should be retried, not blamed on the user's SOF."""
    if not isinstance(result, dict) or result.get("success"):
        return False
    text = json.dumps(result, ensure_ascii=False).lower()

    # AI-only mode is fail-closed. An Ollama timeout, invalid JSON, low
    # confidence, or evidence rejection must become one visible failed job.
    # Treating the word "timeout" as a generic transport retry previously
    # caused the same job to cycle through running forever.
    if (
        bool(result.get("ai_only_no_fallback"))
        or str(result.get("selection_mode") or "").lower() == "ai_only"
        or "ai board classification failed:" in text
        or "qwen_prompt_only_strict" in text
        or "ollama_qwen" in text
    ):
        return False

    if result.get("system_requeue") or result.get("retryable_system_error") or result.get("system_wait"):
        return True
    transient_terms = ("sof copy to quartus server failed", "unable to create remote sof directory", "unable to finalize remote sof", "sftp", "ssh", "socket", "transport", "timed out", "timeout")
    if any(t in text for t in transient_terms):
        return True
    text = json.dumps(result, ensure_ascii=False).lower()
    transient_terms = (
        "sftp", "ssh", "socket", "transport", "connection reset", "connection refused",
        "timed out", "timeout", "remote command exceeded", "temporarily unavailable",
        "copy sof", "upload sof", "runner disappeared", "system recovered",
    )
    if "target_validation_failed" in text or "programming target safety stop" in text or "jtag target mismatch" in text:
        return True
    # Do not retry real Quartus programming failures. Those are the user's SOF/JTAG result.
    if "quartus_pgm failed" in text or "provided sof programming failed" in text:
        return False
    return any(term in text for term in transient_terms)

def priority_value_from_role(value: Any) -> int:
    """Map role names to numeric priority.

    Teacher: highest priority
    Student: second priority
    Background: lowest priority for display/showcase jobs
    """
    text = str(value or "Student").strip().lower()
    if text == "teacher":
        return 10
    if text == "background":
        return 1
    if text == "student":
        return 5
    try:
        return int(value)
    except Exception:
        return 5


def priority_label_from_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("teacher", "student", "background"):
        return text.capitalize()
    try:
        num = int(value)
    except Exception:
        num = 5
    if num >= 10:
        return "Teacher"
    if num <= 1:
        return "Background"
    return "Student"


def sorted_queued_job_ids(state: Dict[str, Any]) -> List[str]:
    jobs = state.get("jobs", {})
    ids = [jid for jid in state.get("queue", []) if jobs.get(jid, {}).get("status") == "queued"]
    return sorted(ids, key=lambda jid: (-priority_value_from_role(jobs.get(jid, {}).get("priority", 5)), float(jobs.get(jid, {}).get("created_ts", 0) or 0)))


def sorted_waiting_display_job_ids(state: Dict[str, Any]) -> List[str]:
    """
    Jobs visible in the Queue tab.

    receiving/uploading jobs are not runnable yet, but they must appear immediately
    so students can see the request entered the system before the large .sof upload completes.
    """
    jobs = state.get("jobs", {})
    visible_statuses = {"queued", "receiving", "uploading"}
    ids = [jid for jid in state.get("queue", []) if str(jobs.get(jid, {}).get("status", "")).lower() in visible_statuses]
    return sorted(ids, key=lambda jid: (
        -priority_value_from_role(jobs.get(jid, {}).get("priority", 5)),
        float(jobs.get(jid, {}).get("created_ts", 0) or 0)
    ))



def public_queue_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact queue job safe for public queue display.

    Keep the live /queue payload small so Raspberry Pi GUI polling and SSE updates
    stay responsive. Large previews, detailed classifier blobs, Quartus logs, and
    history internals are intentionally omitted from the public snapshot.
    """
    job = dict(job or {})
    public_keys = {
        "job_id", "status", "student", "user", "priority_role", "priority_label",
        "major", "kind", "source_mode", "selected_board", "planned_board",
        "selected_instance_id", "planned_instance_id", "selected_jtag_cable",
        "planned_jtag_cable", "queue_position_for_slot", "queue_position_message",
        "wait_seconds", "remaining_seconds", "test_minutes", "test_seconds",
        "created_at", "started_at", "queued_at", "test_start_at", "test_end_at",
        "finished_at", "message", "filename", "jtag_instance", "jtag_cable",
        "planned_start_at", "estimated_finish_at", "estimated_seconds",
        "upload_stage", "requested_board", "ai_selected_board", "role",
        "slot_occupancy_estimated_seconds", "running_phase", "phase_updated_at", "failure_reason"
    }
    out = {k: job.get(k) for k in public_keys if k in job}

    # Keep the internal lifecycle as "running" for queue safety, but expose the
    # AI inference stage honestly to the GUI. The board is not being programmed
    # until Qwen has returned and passed the evidence/confidence gates.
    phase = str(job.get("running_phase") or "").lower()
    status_lower = str(job.get("status") or "").lower()
    ai_board = str(job.get("ai_selected_board") or job.get("selected_board") or "").strip()
    awaiting_ai = (
        status_lower in {"queued", "running", "analyzing"}
        and (
            phase in {"", "queued", "ai_selecting", "ai_validating", "analyzing"}
            or not ai_board
        )
        and not ai_board
    )

    if awaiting_ai:
        out["status"] = "analyzing" if status_lower != "queued" else "queued"
        out["running_phase"] = "ai_selecting"
        out["message"] = "AI board classification in progress"
        out["queue_position_message"] = "Waiting for AI board-family selection"
        for key in (
            "planned_board",
            "planned_instance_id",
            "planned_jtag_cable",
            "selected_instance_id",
            "selected_jtag_cable",
            "jtag_instance",
            "jtag_cable",
        ):
            out.pop(key, None)

    # Compatibility aliases used by existing GUI code.
    out["user"] = job.get("student") or job.get("user") or ""
    out["role"] = job.get("priority_role") or job.get("role") or ""
    if awaiting_ai:
        out["slot"] = ""
        out["jtag"] = ""
    else:
        out["slot"] = job.get("selected_instance_id") or job.get("jtag_instance") or ""
        out["jtag"] = job.get("selected_jtag_cable") or job.get("jtag_cable") or ""
    out.pop("cancel_token", None)
    out.pop("owner_token_hash", None)
    return out


# v4.31: block the same unchanged .v/.sof pair only while it is truly active.
# Completed/failed/cancelled jobs must NOT block a new attempt, because students
# often cancel and immediately resubmit the same code for testing.
ACTIVE_DUPLICATE_STATUSES = {"receiving", "uploading", "queued", "running", "testing", "pending"}


def is_valid_verilog_file_path(path_value: Any) -> bool:
    """True only for a real .v/.sv file. Important: Path("") becomes '.',
    so use is_file() and suffix checks instead of exists()."""
    try:
        p = Path(str(path_value or ""))
        return p.is_file() and p.suffix.lower() in (".v", ".sv")
    except Exception:
        return False


def is_valid_sof_file_path(path_value: Any) -> bool:
    """True only for a real .sof file; directories like '.' are not valid."""
    try:
        p = Path(str(path_value or ""))
        return p.is_file() and p.suffix.lower() == ".sof"
    except Exception:
        return False


def job_has_valid_server_archive_paths(job: Dict[str, Any]) -> bool:
    v = str(job.get("verilog_path") or job.get("archived_verilog_path") or "")
    s = str(job.get("sof_path") or job.get("archived_sof_path") or job.get("remote_sof") or "")
    return v.lower().endswith((".v", ".sv")) and s.lower().endswith(".sof") and v.startswith("/") and s.startswith("/")


def upload_job_ready_to_program(job: Dict[str, Any]) -> Tuple[bool, str]:
    """Guard against starting a queued upload before real files are available.

    v4.08 could accidentally treat Path("") as '.', promote the job to queued,
    then fail instantly with 'Only .v or .sv files are accepted'.
    """
    kind = str(job.get("kind") or "").lower()
    if kind != "upload":
        return True, "ready"
    if is_valid_verilog_file_path(job.get("verilog_local_path")) and is_valid_sof_file_path(job.get("sof_local_path")):
        return True, "ready_local_files"
    stage_ok, stage_reason = staged_job_files_ready(job)
    if stage_ok:
        return True, stage_reason
    if job_has_valid_server_archive_paths(job):
        return True, "ready_server_archive"
    if is_valid_verilog_file_path(job.get("spool_verilog_path")) and is_valid_sof_file_path(job.get("spool_sof_path")):
        stage_l = str(job.get("upload_stage") or "").lower()
        if bool(job.get("upload_files_attached")) or stage_l in (
            "queued_pi_spool_ready_for_programming",
            "received_temp_spool_queued_for_programming",
            "queued_tmp_staged_waiting_for_slot",
        ):
            return True, "ready_temporary_spool_runtime_passthrough"
        return False, "waiting for upload finalize to mark temporary spool ready"
    return False, stage_reason or "waiting for valid uploaded .v/.sof files"


def _norm_identity(value: Any) -> str:
    return str(value or "").strip().lower()


def submission_owner_key(job: Dict[str, Any]) -> str:
    """Return one stable fair-share/duplicate owner key.

    v4.45 fix: the old code treated student, hostname, OR IP as equivalent.
    During local stress tests, every request comes from 127.0.0.1, so 100
    simulated users were collapsed into one owner. In labs behind a proxy/NAT,
    different students could also be grouped by the same source IP. Prefer the
    explicit GUI student/user value, then the GUI computer name, and use IP only
    as a fallback when the GUI did not identify the client.
    """
    for key in ("student", "client_hostname", "student_ip"):
        value = _norm_identity(job.get(key))
        if value and value not in ("unknown", "none", "null", "localhost", "127.0.0.1"):
            return f"{key}:{value}"
    value = _norm_identity(job.get("student_ip")) or _norm_identity(job.get("client_hostname")) or _norm_identity(job.get("student"))
    return f"fallback:{value}" if value else ""


def same_submission_owner(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Treat duplicate/fair-share protection as user-local, not IP-global."""
    ak = submission_owner_key(a)
    bk = submission_owner_key(b)
    return bool(ak and bk and ak == bk)


def compute_submission_signature_from_job(job: Dict[str, Any]) -> str:
    """Stable duplicate key for one .v/.sof pair.

    The GUI sends submission_signature based on the selected local file paths,
    sizes, and mtimes. For older clients or server-path jobs, fall back to the
    visible source/bitstream identity. This is not a security hash; it prevents
    accidental double-click duplicate jobs while still allowing a new job after
    either file changes.
    """
    explicit = str(job.get("submission_signature") or job.get("file_pair_signature") or "").strip()
    if explicit:
        return explicit
    payload = {
        "kind": job.get("kind", ""),
        "filename": job.get("filename", ""),
        "verilog_filename": job.get("verilog_filename", ""),
        "sof_filename": job.get("sof_filename", ""),
        "verilog_path": job.get("verilog_path", ""),
        "sof_path": job.get("sof_path", ""),
        "archived_verilog_path": job.get("archived_verilog_path", ""),
        "archived_sof_path": job.get("archived_sof_path", ""),
        "remote_sof": job.get("remote_sof", ""),
        "verilog_local_path": job.get("verilog_local_path", ""),
        "sof_local_path": job.get("sof_local_path", ""),
        "verilog_size_bytes": job.get("verilog_size_bytes", ""),
        "sof_size_bytes": job.get("sof_size_bytes", ""),
    }
    code = str(job.get("verilog_code") or "")
    if code:
        payload["verilog_code_sha256"] = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def attach_submission_signature(job: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = data or {}
    sig = str(data.get("submission_signature") or data.get("file_pair_signature") or job.get("submission_signature") or "").strip()
    if sig:
        job["submission_signature"] = sig
    else:
        job["submission_signature"] = compute_submission_signature_from_job(job)
    for key in (
        "verilog_file_signature", "sof_file_signature", "verilog_client_path",
        "sof_client_path", "verilog_client_mtime_ns", "sof_client_mtime_ns",
        "verilog_client_size", "sof_client_size",
    ):
        if key in data and data.get(key) not in (None, ""):
            job[key] = data.get(key)
    return job


def find_active_duplicate_submission(state: Dict[str, Any], candidate: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    sig = compute_submission_signature_from_job(candidate)
    if not sig:
        return None
    candidate_id = str(candidate.get("job_id") or "")
    for jid, existing in (state.get("jobs") or {}).items():
        if jid == candidate_id or not isinstance(existing, dict):
            continue
        status = str(existing.get("status") or "").lower()
        if status not in ACTIVE_DUPLICATE_STATUSES:
            continue
        if compute_submission_signature_from_job(existing) != sig:
            continue
        if not same_submission_owner(existing, candidate):
            continue
        return jid, existing
    return None


def duplicate_submission_response(existing_id: str, existing: Dict[str, Any]) -> Dict[str, Any]:
    return ok(
        job_id=existing_id,
        cancel_token=existing.get("cancel_token", ""),
        status=existing.get("status", "queued"),
        duplicate_existing=True,
        duplicate_policy="one_active_job_per_unchanged_v_sof_pair",
        message=(
            f"Duplicate submit blocked: unchanged .v/.sof pair already has active job "
            f"{existing_id} with status {existing.get('status', 'unknown')}. "
            "Wait/cancel that active job first, or change/rebuild the .v or .sof file."
        ),
        job=public_queue_job(existing),
    )


CLASSROOM_ACTIVE_STATUSES = {"receiving", "uploading", "queued", "running", "testing"}


def fair_share_config() -> Dict[str, Any]:
    cfg = load_config()
    fs = (cfg.get("fair_share", {}) or {})
    def iv(name: str, default: int) -> int:
        try:
            return int(fs.get(name, default))
        except Exception:
            return int(default)
    return {
        "enabled": bool(fs.get("enabled", True)),
        "max_active_jobs_per_student": max(0, iv("max_active_jobs_per_student", 1)),
        "reject_when_queue_over_soft_capacity": bool(fs.get("reject_when_queue_over_soft_capacity", True)),
        "soft_capacity_retry_after_seconds": max(1, iv("soft_capacity_retry_after_seconds", 5)),
    }


def active_jobs_for_same_owner(state: Dict[str, Any], candidate: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    candidate_id = str(candidate.get("job_id") or "")
    for jid, job in (state.get("jobs") or {}).items():
        if jid == candidate_id or not isinstance(job, dict):
            continue
        if str(job.get("status") or "").lower() not in CLASSROOM_ACTIVE_STATUSES:
            continue
        if same_submission_owner(job, candidate):
            out.append((jid, job))
    return out



def cleanup_stale_upload_blockers_for_owner(state: Dict[str, Any], candidate: Dict[str, Any], reason: str = "prequeue_admission") -> int:
    """Cancel abandoned receiving/uploading rows for the same owner before fair-share.

    v4.42: If the GUI was closed, cancelled mid-upload, or a prequeue/upload race
    left an old row in receiving/uploading with no files attached, that row should
    not block the same student forever.  This only touches jobs that have no
    accepted upload/stage/spool evidence and are older than a short grace window.
    """
    cfg = load_config()
    seconds = 30
    try:
        seconds = int((((cfg.get("upload_recovery", {}) or {}).get("abandoned_upload_grace_seconds")) or 30))
    except Exception:
        seconds = 30
    seconds = max(10, seconds)
    now_ts = time.time()
    changed = 0
    jobs = state.setdefault("jobs", {})
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        if not same_submission_owner(job, candidate):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l not in ("receiving", "uploading"):
            continue
        created = float(job.get("created_ts") or job.get("upload_started_ts") or 0)
        upload_started = float(job.get("upload_files_request_started_ts", 0) or 0)
        age = now_ts - (upload_started or created or now_ts)
        has_evidence = any(bool(job.get(k)) for k in (
            "upload_files_attached", "stage_sof_active_path", "stage_sof_tmp_path",
            "spool_sof_path", "sof_local_path", "archived_sof_path", "remote_sof"
        ))
        in_progress = bool(job.get("upload_files_in_progress"))
        if (not has_evidence) and (not in_progress) and age >= seconds:
            job["status"] = "cancelled"
            job["cancelled_at"] = now_iso()
            job["cancel_reason"] = f"{reason}: abandoned upload blocker cleaned after {int(age)}s"
            job["message"] = "abandoned upload cleaned automatically so resubmit can continue"
            job["upload_files_in_progress"] = False
            job["remaining_seconds"] = 0
            job["wait_seconds"] = 0
            job.pop("receive_deadline_ts", None)
            if str(job.get("planned_instance_id") or "") == "uploading":
                job.pop("planned_instance_id", None)
            changed += 1
    return changed

def fair_share_admission_check(state: Dict[str, Any], candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Protect the lab from one student flooding the Pi while many students use it.

    The queue can scale dynamically, but each user should only hold a small number
    of active jobs. Completed/failed/cancelled jobs do not count, so the student can
    submit again once their current job leaves active use.
    """
    fs = fair_share_config()
    if not fs.get("enabled", True):
        return None
    active = active_jobs_for_same_owner(state, candidate)
    limit = int(fs.get("max_active_jobs_per_student", 1) or 0)
    if limit > 0 and len(active) >= limit:
        oldest = active[0][0] if active else ""
        return fail(
            f"Classroom fair-share limit: you already have {len(active)} active job(s). Wait, cancel your active job, or let it finish before submitting another.",
            fair_share_blocked=True,
            max_active_jobs_per_student=limit,
            active_job_ids=[jid for jid, _j in active[:10]],
            existing_job_id=oldest,
            retry_after_seconds=5,
        )
    if fs.get("reject_when_queue_over_soft_capacity", True):
        try:
            dyn = adaptive_runtime_config(refresh=False).get("limits", {}) if "adaptive_runtime_config" in globals() else {}
            cap = int(dyn.get("queue_soft_capacity_jobs", 0) or 0)
            if cap > 0:
                active_count = sum(1 for j in (state.get("jobs") or {}).values() if isinstance(j, dict) and str(j.get("status") or "").lower() in CLASSROOM_ACTIVE_STATUSES)
                if active_count >= cap:
                    return fail(
                        "Classroom queue is temporarily full. The Pi protected itself from overload; try again shortly.",
                        fair_share_blocked=True,
                        queue_soft_capacity_jobs=cap,
                        active_job_count=active_count,
                        retry_after_seconds=int(fs.get("soft_capacity_retry_after_seconds", 5) or 5),
                    )
        except Exception:
            pass
    return None




def queue_format_duration(seconds: Any) -> str:
    """Human-readable mm:ss/seconds for queue wait notifications."""
    try:
        total = max(0, int(float(seconds or 0)))
    except Exception:
        total = 0
    minutes, sec = divmod(total, 60)
    if minutes:
        return f"{minutes}m {sec:02d}s"
    return f"{sec}s"


def queue_job_verilog_code(job: Dict[str, Any]) -> str:
    """Best-effort local Verilog text for board-family prediction."""
    code = str(job.get("verilog_code") or job.get("verilog_preview") or "")
    if code.strip():
        return code

    limit = int(controller_runtime_limits().get("max_classifier_verilog_bytes", 1024 * 1024))
    for key in ("verilog_local_path", "verilog_local_copy_path", "stage_verilog_active_path", "spool_verilog_path"):
        p = str(job.get(key) or "").strip()
        if not p:
            continue
        try:
            path = Path(p)
            if path.exists() and path.is_file():
                return limited_text_file_read(path, limit)
        except Exception:
            pass
    return ""


def queue_job_qsf_text(job: Dict[str, Any]) -> str:
    """Best-effort QSF text for board-family prediction and final AI selection."""
    text = str(job.get("qsf_text") or job.get("qsf_preview") or "")
    if text.strip():
        return text
    limit = int(controller_runtime_limits().get("max_classifier_qsf_bytes", 256 * 1024))
    for key in ("qsf_local_path", "stage_qsf_active_path", "spool_qsf_path"):
        p = str(job.get(key) or "").strip()
        if not p:
            continue
        try:
            path = Path(p)
            if path.exists() and path.is_file() and path.suffix.lower() == ".qsf":
                return limited_text_file_read(path, limit)
        except Exception:
            pass
    remote_qsf = str(job.get("qsf_path") or "").strip()
    if remote_qsf and remote_path_allowed(remote_qsf):
        try:
            return read_remote_text(remote_qsf)
        except Exception:
            return ""
    return ""


def queue_target_board_hint(job: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    Predict the board family for queue planning.

    Works for any board family listed in board_catalog.
    - If user forced a board family, use it.
    - If user forced a specific instance/cable, infer family when possible.
    - Otherwise score the Verilog features against every configured board family.
    """
    cfg = cfg or load_config()
    catalog = cfg.get("board_catalog", {}) or {}
    requested = str(job.get("requested_board") or "").strip()

    if requested in catalog:
        return requested

    if requested:
        requested_l = requested.lower()
        for board_name in catalog:
            if requested_l.startswith(board_name.lower() + "-"):
                return board_name
        # Requested might be an exact JTAG cable string. Infer from aliases if possible.
        inferred = infer_board_type_from_jtag_cable(requested)
        if inferred in catalog:
            return inferred

    existing = str(job.get("target_board_hint") or "").strip()
    if existing in catalog:
        return existing

    code = queue_job_verilog_code(job)
    if not code.strip():
        return ""

    # If a QSF was uploaded/found from a QPF, use the same deterministic classifier
    # that the final programming safety gate uses. This lets the planner reserve
    # the correct board family before the job starts.
    try:
        qsf_text = queue_job_qsf_text(job)
        if qsf_text.strip():
            cls = classify_fpga_board(code, qsf_text=qsf_text, filename=str(job.get("filename") or job.get("verilog_filename") or "design.v"))
            tb = str(cls.get("target_board") or cls.get("selected_board") or "").strip()
            if tb in catalog and bool(cls.get("safe_to_program", False)):
                return tb
    except Exception:
        pass

    required = extract_features(code)
    if not required:
        return ""

    best_board = ""
    best_score = -10**9
    for board_name, board_cfg in catalog.items():
        if not bool(board_cfg.get("enabled", True)):
            continue
        score = score_board(required, board_cfg.get("features", []), board_name)
        if score > best_score:
            best_score = score
            best_board = board_name

    return best_board if best_score > 0 else ""


def build_queue_planner_slots(state: Optional[Dict[str, Any]] = None, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Build enabled physical JTAG slots for the smart FIFO planner.

    This uses the same config alias system as Real-Time JTAG, so it is not limited to DE1-SoC.
    Any board family added to board_catalog can be planned.
    """
    cfg = cfg or load_config()
    state = state or load_state()
    catalog = cfg.get("board_catalog", {}) or {}
    now_ts = time.time()

    try:
        jtag = discover_jtag(force=False)
        detected = jtag.get("cables", []) or []
    except Exception:
        detected = []

    per_board_count: Dict[str, int] = {}
    slots: List[Dict[str, Any]] = []

    for raw_idx, cable in enumerate(detected, start=1):
        board = infer_board_type_from_jtag_cable(cable)
        if board not in catalog:
            continue

        bcfg = catalog.get(board, {}) or {}
        lock_key = instance_lock_key(board, cable)
        disabled_info = state.setdefault("disabled_jtag", {}).get(lock_key, {})
        manually_disabled = bool(disabled_info)
        enabled = bool(bcfg.get("enabled", True)) and not manually_disabled

        pstatus = physical_status(board, bcfg)
        physical_ok = pstatus in ("ok", "ok_simulated", "unknown")
        if not enabled or not physical_ok:
            continue

        busy, lock, timing = instance_lock_timing(board, cable, state)
        remaining = int(timing.get("remaining_seconds", 0) or 0)
        if busy:
            # instance_lock_timing uses the lock expected_seconds field. If it is 0,
            # fall back to the controller lock timeout so ETA does not show 0 for a busy board.
            locked_at = float(lock.get("locked_at", 0) or 0)
            expected = int(lock.get("expected_seconds", 0) or 0)
            if expected <= 0:
                expected = int(cfg.get("lock_timeout_seconds", 600) or 600)
            if locked_at:
                remaining = max(0, expected - int(now_ts - locked_at))

        available_ts = now_ts + max(0, remaining)
        per_board_count[board] = per_board_count.get(board, 0) + 1

        slots.append({
            "board": board,
            "instance_id": f"{board}-{per_board_count[board]}",
            "raw_instance_id": f"JTAG-{raw_idx}",
            "detected_cable": cable,
            "lock_key": lock_key,
            "quartus_family": bcfg.get("quartus_family", "unknown"),
            "busy": bool(busy),
            "available_now": not bool(busy),
            "available_ts": float(available_ts),
            "available_at": iso_from_ts(available_ts),
            "available_in_seconds": int(max(0, available_ts - now_ts)),
            "physical_status": pstatus,
        })

    return slots


def queue_job_matches_slot(job: Dict[str, Any], slot: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Return True when a queued job can use this physical JTAG slot."""
    cfg = cfg or load_config()
    requested = str(job.get("requested_board") or "").strip()
    if requested:
        return requested in (
            slot.get("board", ""),
            slot.get("instance_id", ""),
            slot.get("raw_instance_id", ""),
            slot.get("detected_cable", ""),
            slot.get("lock_key", ""),
        )

    target = queue_target_board_hint(job, cfg)
    if target:
        return slot.get("board") == target

    # Auto with no reliable Verilog hint: any enabled visible board family can be planned.
    return True


def annotate_queue_assignments(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Assign each queued job to the earliest matching physical JTAG slot.

    This creates the user-facing messages:
    - You are next in line for Board X / JTAG slot Y.
    - Live ETA until the current occupant's timer expires.
    The calculation is FIFO by queue order and works across every board family in board_catalog.
    """
    cfg = load_config()
    now_ts = time.time()
    jobs = state.setdefault("jobs", {})
    slots = build_queue_planner_slots(state, cfg)

    slot_cursor = {s["lock_key"]: float(s.get("available_ts", now_ts) or now_ts) for s in slots}
    slot_position = {s["lock_key"]: 0 for s in slots}

    for job_id in sorted_queued_job_ids(state):
        job = jobs.get(job_id, {})
        if not job or job.get("status") != "queued":
            continue

        target = queue_target_board_hint(job, cfg)
        if target:
            job["target_board_hint"] = target

        candidates = [s for s in slots if queue_job_matches_slot(job, s, cfg)]

        if not candidates:
            job["planned_board"] = target or job.get("requested_board") or "Auto"
            job["planned_instance_id"] = ""
            job["planned_jtag_cable"] = ""
            job["planned_lock_key"] = ""
            job["wait_seconds"] = 0
            job["planned_start_at"] = ""
            job["queue_position_for_slot"] = 0
            job["queue_position_message"] = "waiting for a matching enabled/detected JTAG board"
            job["message"] = job["queue_position_message"]
            jobs[job_id] = job
            continue

        def slot_key(slot: Dict[str, Any]):
            key = slot.get("lock_key", "")
            return (
                float(slot_cursor.get(key, now_ts)),
                int(slot_position.get(key, 0)),
                str(slot.get("board", "")),
                str(slot.get("instance_id", "")),
                str(slot.get("detected_cable", "")),
            )

        chosen = min(candidates, key=slot_key)
        key = chosen.get("lock_key", "")
        start_ts = max(now_ts, float(slot_cursor.get(key, now_ts)))
        wait_seconds = max(0, int(start_ts - now_ts))
        slot_position[key] = int(slot_position.get(key, 0)) + 1
        position = int(slot_position[key])

        board = chosen.get("board", "")
        instance = chosen.get("instance_id", "")
        cable = chosen.get("detected_cable", "")
        estimated_seconds = int(job.get("estimated_seconds", 0) or 0)
        if estimated_seconds <= 0:
            estimated_seconds = estimate_deploy_seconds(board, int(job.get("test_seconds", 0) or 0))
            job["estimated_seconds"] = estimated_seconds

        job["planned_board"] = board
        job["planned_instance_id"] = instance
        job["planned_jtag_cable"] = cable
        job["planned_lock_key"] = key
        job["planned_start_ts"] = start_ts
        job["planned_start_at"] = iso_from_ts(start_ts)
        job["wait_seconds"] = wait_seconds
        job["queue_position_for_slot"] = position

        if position == 1:
            phrase = f"You are next in line for {instance}"
        else:
            phrase = f"You are #{position} in line for {instance}"
        job["queue_position_message"] = f"{phrase} ({board})"
        job["message"] = f"waiting: {job['queue_position_message']}. ETA to start: {queue_format_duration(wait_seconds)}."

        # Reserve this future slot in the planner for following queued jobs.
        slot_cursor[key] = start_ts + max(1, estimated_seconds)
        jobs[job_id] = job

    state["jobs"] = jobs
    state["queue_plan"] = {
        "updated_at": now_iso(),
        "slot_count": len(slots),
        "slots": [
            {
                "board": s.get("board", ""),
                "instance_id": s.get("instance_id", ""),
                "raw_instance_id": s.get("raw_instance_id", ""),
                "detected_cable": s.get("detected_cable", ""),
                "busy": s.get("busy", False),
                "available_at": s.get("available_at", ""),
                "available_in_seconds": s.get("available_in_seconds", 0),
            }
            for s in slots
        ],
        "policy": "FIFO per matching board family / earliest matching physical JTAG slot",
    }
    return state



def assign_immediate_free_slot_for_job(job: Dict[str, Any], state: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, str]:
    """
    For the oldest queued job, choose a matching slot that is free right now.

    This is the final FIFO routing step. It prevents a stale planned slot from
    holding the oldest queued job hostage when another matching board already freed.
    """
    cfg = load_config()
    slots = build_queue_planner_slots(state, cfg)
    candidates = []
    for slot in slots:
        if not queue_job_matches_slot(job, slot, cfg):
            continue
        if not bool(slot.get("available_now", False)):
            continue
        temp = dict(job)
        temp["planned_board"] = slot.get("board", "")
        temp["planned_instance_id"] = slot.get("instance_id", "")
        temp["planned_jtag_cable"] = slot.get("detected_cable", "")
        temp["planned_lock_key"] = slot.get("lock_key", "")
        ok_slot, reason = hard_slot_available_for_job(temp, state)
        if ok_slot:
            candidates.append(slot)

    if not candidates:
        return job, False, "no matching physical slot free right now"

    chosen = min(candidates, key=lambda s: (
        float(s.get("available_ts", time.time()) or time.time()),
        str(s.get("board", "")),
        str(s.get("instance_id", "")),
        str(s.get("detected_cable", "")),
    ))

    board = chosen.get("board", "")
    test_seconds = int(job.get("test_seconds", 0) or 0)
    program_seconds = estimate_program_seconds(board)

    job["planned_board"] = board
    job["planned_instance_id"] = chosen.get("instance_id", "")
    job["planned_jtag_cable"] = chosen.get("detected_cable", "")
    job["planned_lock_key"] = chosen.get("lock_key", "")
    job["planned_start_ts"] = time.time()
    job["planned_start_at"] = now_iso()
    job["wait_seconds"] = 0
    job["remaining_seconds"] = 0
    job["program_estimated_seconds"] = program_seconds
    job["slot_occupancy_estimated_seconds"] = estimate_deploy_seconds(board, test_seconds)
    job["queue_position_for_slot"] = 1
    job["queue_position_message"] = f"You are next in line for {job.get('planned_instance_id')} ({board})"
    return job, True, "assigned immediate free slot"


def queued_job_can_run_now(job: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """True only if a matching physical JTAG slot is free now and not claimed by any active job."""
    _job, ok_now, _reason = assign_immediate_free_slot_for_job(dict(job), state)
    return bool(ok_now)


def first_runnable_queued_job_id(state: Dict[str, Any]) -> str:
    """
    Pick the first FIFO queued job that can run now.

    This prevents a DE1-SoC waiting job from blocking an Agilex job when the Agilex is free,
    while still preserving FIFO order among jobs competing for the same board family.
    """
    now_ts = time.time()
    for job_id in sorted_queued_job_ids(state):
        job = state.get("jobs", {}).get(job_id, {})
        if not job or job.get("status") != "queued":
            continue
        try:
            if float(job.get("dispatch_not_before_ts", 0) or 0) > now_ts:
                continue
        except Exception:
            pass
        if queued_job_can_run_now(job, state):
            return job_id
    return ""



def clear_board_slot_state(state: Dict[str, Any], board: str, cable: str, reason: str, job_id: str = "", clear_mode: str = "logical") -> Dict[str, Any]:
    """
    Clear a physical JTAG slot from the controller's perspective.

    Default clear mode is logical:
    - releases the lock
    - clears active user/session metadata
    - updates the queue/job display
    - records a slot clear event

    This avoids resetting an entire board family by mistake when multiple physical boards
    share the same board type. A future config can add a blank-SOF or per-instance reset policy.
    """
    now_ts = time.time()
    key = instance_lock_key(board, cable)
    event = {
        "cleared_at": now_iso(),
        "cleared_ts": now_ts,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
        "reason": reason,
        "job_id": job_id,
        "clear_mode": clear_mode,
    }
    state.setdefault("locks", {})[key] = {
        "busy": False,
        "released_at": now_ts,
        "released_at_iso": event["cleared_at"],
        "reason": reason,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
        "cleared": True,
        "clear_mode": clear_mode,
    }
    events = state.setdefault("slot_clear_events", [])
    events.append(event)
    if len(events) > 100:
        del events[:-100]

    try:
        bcfg = load_config().get("board_catalog", {}).get(board, {})
        if bcfg:
            set_led(bcfg, busy=False, ready=True)
    except Exception:
        pass

    return event


def apply_teacher_override_for_job(state: Dict[str, Any], teacher_job_id: str) -> Dict[str, Any]:
    """
    Teacher Override.

    If a Teacher job is queued and every matching physical slot is occupied by lower-priority
    testing jobs, immediately bump the lowest-priority matching testing job back into the queue.

    Notes:
    - Active Quartus programming is not killed mid-command because that can corrupt programming.
    - Override is applied at the safe boundary: testing/session timer locks.
    - Background is lower than Student, so Background is bumped before Student.
    - Works for any board family because it uses the same board_catalog/JTAG slot planner.
    """
    jobs = state.setdefault("jobs", {})
    teacher_job = jobs.get(teacher_job_id, {})
    if not teacher_job or teacher_job.get("status") != "queued":
        return state
    if priority_label_from_value(teacher_job.get("priority_label") or teacher_job.get("priority")) != "Teacher":
        return state

    cfg = load_config()
    slots = build_queue_planner_slots(state, cfg)
    matching_slots = [s for s in slots if queue_job_matches_slot(teacher_job, s, cfg)]
    if not matching_slots:
        return state

    # If one matching slot is already free, no override is needed.
    now_ts = time.time()
    if any(float(s.get("available_ts", now_ts) or now_ts) <= now_ts + 1 for s in matching_slots):
        return state

    teacher_priority = priority_value_from_role("Teacher")
    candidates = []
    for slot in matching_slots:
        key = slot.get("lock_key", "")
        lock = state.setdefault("locks", {}).get(key, {})
        victim_id = lock.get("job_id", "")
        victim = jobs.get(victim_id, {})
        if not victim_id or not victim:
            continue
        if victim.get("status") != "testing":
            continue
        victim_priority = priority_value_from_role(victim.get("priority_label") or victim.get("priority") or "Student")
        if victim_priority >= teacher_priority:
            continue
        remaining = int(slot.get("available_in_seconds", 0) or 0)
        candidates.append((victim_priority, -remaining, float(victim.get("created_ts", 0) or 0), slot, victim_id, victim, lock))

    if not candidates:
        return state

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    _vp, _neg_remaining, _created, slot, victim_id, victim, lock = candidates[0]
    board = slot.get("board", "")
    cable = slot.get("detected_cable", "")
    instance_id = slot.get("instance_id", "")
    reason = f"teacher_override_by:{teacher_job_id}"

    clear_event = clear_board_slot_state(state, board, cable, reason=reason, job_id=victim_id, clear_mode="teacher_override_logical_clear")

    # Put the interrupted lower-priority job back into the queue.
    victim["status"] = "queued"
    victim["message"] = f"interrupted by Teacher override; returned to queue from {instance_id} | {cable}"
    victim["bumped_by_teacher"] = True
    victim["bumped_by_job_id"] = teacher_job_id
    victim["bumped_at"] = now_iso()
    victim["bumped_from_board"] = board
    victim["bumped_from_instance_id"] = instance_id
    victim["bumped_from_jtag_cable"] = cable
    victim["last_wait_reason"] = "teacher_override"
    victim["started_at"] = ""
    victim["started_ts"] = 0
    victim["finished_at"] = ""
    victim["finished_ts"] = 0
    victim["test_start_at"] = ""
    victim["test_start_ts"] = 0
    victim["test_end_at"] = ""
    victim["test_end_ts"] = 0
    for key in ("selected_board", "ai_selected_board", "selected_jtag_cable", "jtag_cable", "selected_instance_id", "jtag_instance", "selected_lock_key", "test_timer", "held_for_testing"):
        victim.pop(key, None)
    jobs[victim_id] = victim
    if victim_id not in state.setdefault("queue", []):
        state["queue"].append(victim_id)

    # Mark the teacher job so the GUI can explain the override transparently.
    teacher_job["teacher_override"] = True
    teacher_job["override_victim_job_id"] = victim_id
    teacher_job["override_instance_id"] = instance_id
    teacher_job["override_board"] = board
    teacher_job["override_jtag_cable"] = cable
    teacher_job["override_at"] = now_iso()
    teacher_job["override_clear_event"] = clear_event
    teacher_job["message"] = f"Teacher override: bumped lower-priority job {victim_id}; assigned to {instance_id} ({board})"
    teacher_job["planned_board"] = board
    teacher_job["planned_instance_id"] = instance_id
    teacher_job["planned_jtag_cable"] = cable
    teacher_job["planned_lock_key"] = slot.get("lock_key", "")
    teacher_job["planned_start_ts"] = time.time()
    teacher_job["planned_start_at"] = now_iso()
    teacher_job["wait_seconds"] = 0
    teacher_job["queue_position_message"] = f"Teacher override assigned to {instance_id} ({board})"
    jobs[teacher_job_id] = teacher_job
    state["jobs"] = jobs

    state.setdefault("teacher_override_events", []).append({
        "at": now_iso(),
        "teacher_job_id": teacher_job_id,
        "victim_job_id": victim_id,
        "board": board,
        "instance_id": instance_id,
        "detected_cable": cable,
        "reason": "Teacher priority override",
    })
    if len(state["teacher_override_events"]) > 50:
        del state["teacher_override_events"][:-50]

    return state





def job_slot_lock_key(job: Dict[str, Any]) -> str:
    """Return the best physical slot key for a job without doing JTAG/SSH."""
    if not isinstance(job, dict):
        return ""
    key = str(job.get("selected_lock_key") or job.get("planned_lock_key") or "").strip()
    if key:
        return key
    board = str(job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or job.get("requested_board") or "").strip()
    cable = str(job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or "").strip()
    if board and cable:
        return instance_lock_key(board, cable)
    return ""


def active_job_free_ts(job: Dict[str, Any], snapshot_now: float) -> float:
    """
    Return when an active job should release its slot, using live timer fields.

    Testing jobs use test_end_ts/test_start_ts+test_seconds, which is the most accurate
    source for the visible Remaining value. Running jobs use started_ts+estimated_seconds.
    """
    status = str(job.get("status") or "").lower()

    if status == "testing":
        end_ts = float(job.get("test_end_ts", 0) or 0)
        if end_ts:
            return max(snapshot_now, end_ts)
        start_ts = float(job.get("test_start_ts", 0) or 0)
        seconds = int(job.get("test_seconds", job.get("estimated_seconds", 0)) or 0)
        if start_ts and seconds:
            return max(snapshot_now, start_ts + seconds)
        remaining = int(job.get("remaining_seconds", 0) or 0)
        return snapshot_now + max(0, remaining)

    if status == "running":
        start_ts = float(job.get("started_ts", 0) or 0)
        board = str(job.get("planned_board") or job.get("selected_board") or job.get("ai_selected_board") or job.get("requested_board") or "")
        program_expected = int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(board))

        # v4.16: running wait ETA is programming-only. Test time is applied only
        # after quartus_pgm succeeds and the job actually enters testing.
        if start_ts and program_expected:
            return max(snapshot_now, start_ts + max(1, program_expected))
        remaining = int(job.get("remaining_seconds", 0) or program_expected or 60)
        return snapshot_now + max(1, remaining)

    return snapshot_now


def build_live_slot_cursor_from_state(state: Dict[str, Any], snapshot_now: float) -> Dict[str, float]:
    """
    Build a live slot cursor using only local state.

    This avoids SSH/JTAG during fast /queue polling and keeps Wait ETA synchronized
    with the active job's live Remaining timer.
    """
    cursor: Dict[str, float] = {}

    for job_id, job in list((state.get("jobs", {}) or {}).items()):
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "").lower() not in ("running", "testing"):
            continue
        key = job_slot_lock_key(job)
        if not key:
            continue
        free_ts = active_job_free_ts(job, snapshot_now)
        cursor[key] = max(cursor.get(key, snapshot_now), free_ts)

    # Also consider busy locks that may not have a matching active job row.
    for key, lock in list((state.get("locks", {}) or {}).items()):
        if not isinstance(lock, dict):
            continue
        if not (lock.get("busy") and not is_lock_expired(lock)):
            continue
        locked_at = float(lock.get("locked_at", 0) or 0)
        expected = int(lock.get("expected_seconds", 0) or 0)
        if expected <= 0:
            expected = int(load_config().get("lock_timeout_seconds", 600) or 600)
        free_ts = snapshot_now
        if locked_at:
            free_ts = max(snapshot_now, locked_at + expected)
        else:
            free_ts = snapshot_now + max(0, int(lock.get("remaining_seconds", 0) or 0))
        cursor[key] = max(cursor.get(key, snapshot_now), free_ts)

    return cursor


def live_wait_eta_for_queued_job(job: Dict[str, Any], slot_cursor: Dict[str, float], snapshot_now: float) -> Tuple[int, float]:
    """
    Return live wait seconds and live planned start time for a queued job.

    Uses planned_lock_key when available. This is display-only and keeps Wait/Remain
    aligned with the active slot owner's timer instead of stale annotation time.
    """
    key = job_slot_lock_key(job)
    if not key:
        planned_start = float(job.get("planned_start_ts", 0) or 0)
        if planned_start:
            return countdown_seconds_until(planned_start, snapshot_now), planned_start
        return int(job.get("wait_seconds", 0) or 0), planned_start

    start_ts = max(snapshot_now, float(slot_cursor.get(key, snapshot_now) or snapshot_now))
    wait_seconds = countdown_seconds_until(start_ts, snapshot_now)

    estimated_seconds = int(job.get("estimated_seconds", 0) or 0)
    if estimated_seconds <= 0:
        estimated_seconds = estimate_deploy_seconds(str(job.get("planned_board") or job.get("requested_board") or ""), int(job.get("test_seconds", 0) or 0))
        job["estimated_seconds"] = estimated_seconds

    # Reserve this slot in the live display cursor for following queued jobs.
    slot_cursor[key] = start_ts + max(1, estimated_seconds)
    return wait_seconds, start_ts




def strict_resource_engine_config() -> Dict[str, Any]:
    cfg = load_config()
    raw = cfg.get("strict_resource_engine", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    return raw


def strict_resource_engine_enabled() -> bool:
    return bool(strict_resource_engine_config().get("enabled", True))


def strict_upload_timeout_seconds() -> int:
    cfg = load_config()
    strict = strict_resource_engine_config()
    return int(strict.get("upload_timeout_seconds", cfg.get("queue_receive_timeout_seconds", 180)) or 180)


def strict_running_watchdog_grace_seconds() -> int:
    return int(strict_resource_engine_config().get("running_watchdog_grace_seconds", 30) or 30)


def strict_fifo_waiting_ids(state: Dict[str, Any]) -> List[str]:
    """All non-active waiting jobs, ordered by role priority then true created timestamp."""
    jobs = state.get("jobs", {}) or {}
    waiting = []
    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "").lower() in ("receiving", "uploading", "queued"):
            waiting.append(jid)
    return sorted(waiting, key=lambda jid: (
        -priority_value_from_role(jobs.get(jid, {}).get("priority_label") or jobs.get(jid, {}).get("priority") or "Student"),
        float(jobs.get(jid, {}).get("created_ts", 0) or 0),
        str(jid),
    ))


def _strict_clear_active_fields(job: Dict[str, Any]) -> None:
    for key in (
        "selected_board", "ai_selected_board", "selected_jtag_cable", "jtag_cable",
        "selected_instance_id", "jtag_instance", "selected_lock_key", "running_phase",
        "planned_board", "planned_instance_id", "planned_jtag_cable", "planned_lock_key",
        "locked_target_board", "locked_target_jtag_cable", "locked_target_instance_id",
        "locked_target_lock_key", "locked_target_device_index", "locked_target_quartus_family",
        "started_at", "started_ts", "test_start_at", "test_start_ts", "test_end_at", "test_end_ts",
        "finished_at", "finished_ts", "test_timer", "held_for_testing",
    ):
        job.pop(key, None)
    job["wait_seconds"] = 0
    job["remaining_seconds"] = 0



def stale_runner_config_for_board(board: str) -> Dict[str, int]:
    """Return automatic stale-runner recovery thresholds for a board family."""
    cfg = strict_resource_engine_config() or {}
    b = str(board or "").lower()
    if "agilex" in b or "pro" in b:
        return {
            "force_requeue_after": int(cfg.get("agilex_force_requeue_alive_runner_after_seconds", 1200) or 1200),
            "quarantine_seconds": int(cfg.get("agilex_abandoned_runner_quarantine_seconds", 300) or 300),
        }
    return {
        "force_requeue_after": int(cfg.get("standard_force_requeue_alive_runner_after_seconds", 180) or 180),
        "quarantine_seconds": int(cfg.get("standard_abandoned_runner_quarantine_seconds", 120) or 120),
    }


def quarantine_old_runner_slot(state: Dict[str, Any], board: str, cable: str, job_id: str, reason: str = "stale_runner_requeued", seconds: Optional[int] = None) -> None:
    """Temporarily keep an abandoned runner's original physical slot busy."""
    if not board or not cable:
        return
    cfg = stale_runner_config_for_board(board)
    seconds = int(seconds if seconds is not None else cfg.get("quarantine_seconds", 120))
    seconds = max(15, seconds)
    key = instance_lock_key(board, cable)
    now_ts = time.time()
    state.setdefault("locks", {})[key] = {
        "busy": True,
        "owner": f"abandoned_runner:{job_id}",
        "locked_at": now_ts,
        "locked_at_iso": now_iso(),
        "expected_seconds": seconds,
        "job_id": job_id,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
        "phase": "abandoned_runner_quarantine",
        "reason": reason,
        "quarantine_seconds": seconds,
        "expires_at": iso_from_ts(now_ts + seconds),
    }


def runner_generation_matches(current: Dict[str, Any], snapshot: Dict[str, Any]) -> bool:
    """Return True only if a runner result belongs to the current run attempt."""
    try:
        return int(current.get("run_generation", 0) or 0) == int(snapshot.get("run_generation", 0) or 0)
    except Exception:
        return str(current.get("run_generation", "")) == str(snapshot.get("run_generation", ""))


def job_slot_identity(job: Dict[str, Any]) -> Tuple[str, str, str]:
    board = str(job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or job.get("requested_board") or "")
    cable = str(job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or "")
    key = str(job.get("selected_lock_key") or job.get("planned_lock_key") or "")
    if board and cable:
        key = instance_lock_key(board, cable)
    return board, cable, key


def requeue_jobs_assigned_to_disabled_slots(state: Dict[str, Any], reason: str = "disabled_slot_requeue") -> bool:
    """Move queued/running jobs off disabled slots, but never interrupt testing.

    v4.17 policy requested by the lab:
    - Disable means AI/FIFO will not select that physical JTAG slot for NEW work.
    - If a job is already TESTING on that slot, keep its reserved test timer until
      the normal expiration/cancel path finishes. Do not requeue it and do not
      clear its lock early.
    - If a QUEUED or RUNNING programming job is assigned to that disabled slot,
      relocate/requeue it to another enabled matching board.
    """
    disabled = state.setdefault("disabled_jtag", {}) or {}
    if not disabled:
        return False
    jobs = state.setdefault("jobs", {})
    queue = state.setdefault("queue", [])
    changed = False
    now_text = now_iso()

    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").lower()
        if status not in ("queued", "running", "testing"):
            continue
        board, cable, key = job_slot_identity(job)
        if not key or key not in disabled:
            continue

        if status == "testing":
            # Keep the user's reserved hardware test session alive. The slot is
            # disabled for future AI/FIFO selection, but the current test timer
            # owns it until the timer expires or the user cancels the test.
            if not job.get("disable_policy_noted"):
                job["disable_policy_noted"] = True
                job["disabled_during_testing_at"] = now_text
                job["last_system_recovery_reason"] = f"slot disabled while testing; reservation kept: {key}"
                job["message"] = f"testing continues on disabled slot {cable or key}; no new jobs will be assigned here"
                jobs[job_id] = job
                changed = True
            continue

        if board and cable:
            try:
                clear_board_slot_state(state, board, cable, reason=reason, job_id=job_id)
            except Exception:
                pass

        retry_count = int(job.get("system_retry_count", 0) or 0) + 1
        _strict_clear_active_fields(job)
        for k in ("planned_board", "planned_instance_id", "planned_jtag_cable", "planned_lock_key", "planned_start_at", "planned_start_ts"):
            job.pop(k, None)
        job["status"] = "queued"
        job["system_retry_count"] = retry_count
        job["last_system_recovery_at"] = now_text
        job["last_system_recovery_reason"] = f"assigned slot disabled: {key}"
        job["message"] = f"slot {cable or key} was disabled; requeued for another enabled board"
        job["estimated_seconds"] = estimate_deploy_seconds(board or job.get("target_board_hint") or job.get("requested_board") or "", int(job.get("test_seconds", 0) or 0))
        jobs[job_id] = job
        if job_id not in queue:
            queue.append(job_id)
        with QUEUE_JOB_THREADS_LOCK:
            # Forget this active runner for scheduling purposes. If the old
            # thread returns late, queue_job_runner ignores it because the job is
            # no longer in running state.
            QUEUE_JOB_THREADS.pop(str(job_id), None)
        changed = True

    if changed:
        state["jobs"] = jobs
        state["queue"] = strict_fifo_waiting_ids(state)
        active_running = [jid for jid, j in jobs.items() if isinstance(j, dict) and j.get("status") == "running"]
        state["current_jobs"] = active_running
        state["current_job"] = active_running[0] if active_running else None
        try:
            add_history("disabled_slot_jobs_requeued", "", {"reason": reason})
        except Exception:
            pass
    return changed


def strict_resource_reconcile(state: Dict[str, Any], reason: str = "strict_resource_reconcile") -> bool:
    """
    Strict Resource Engine.

    This is a local state-reconciliation pass that enforces the rebuilt queue model:
    - one physical JTAG slot can have only one active owner
    - receiving/uploading jobs never own hardware
    - queued jobs stay in the waiting state until the scheduler assigns a free slot
    - expired testing sessions are completed and their slot is cleared immediately
    - stale locks without a real active owner are cleared
    - current_jobs/current_job are rebuilt from actual running jobs
    """
    if not strict_resource_engine_enabled() or not isinstance(state, dict):
        return False

    changed = False
    now_ts = time.time()
    jobs = state.setdefault("jobs", {})
    locks = state.setdefault("locks", {})

    # 1) Receiving/uploading is a network state only. It must never hold hardware.
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l in ("receiving", "uploading"):
            had_hw = bool(job.get("selected_lock_key") or job.get("planned_lock_key") or job.get("jtag_cable") or job.get("selected_jtag_cable"))
            for key in ("selected_board", "ai_selected_board", "selected_jtag_cable", "jtag_cable", "selected_instance_id", "jtag_instance", "selected_lock_key", "planned_lock_key", "planned_jtag_cable"):
                job.pop(key, None)
            job["planned_instance_id"] = "uploading"
            if not job.get("receive_deadline_ts"):
                job["receive_deadline_ts"] = now_ts + strict_upload_timeout_seconds()
                job["upload_deadline_at"] = iso_from_ts(job["receive_deadline_ts"])
                changed = True
            if had_hw:
                job["message"] = "uploading files; hardware lock removed by strict resource engine"
                changed = True
            jobs[jid] = job

    # 2) Complete expired testing sessions exactly at timer expiration.
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict) or str(job.get("status") or "").lower() != "testing":
            continue
        end_ts = float(job.get("test_end_ts", 0) or 0)
        if end_ts and now_ts >= end_ts:
            complete_testing_job_in_state(state, jid, job, reason="strict_test_timer_expired")
            changed = True

    # Refresh after completions.
    jobs = state.setdefault("jobs", {})
    locks = state.setdefault("locks", {})

    # 3) Enforce exactly one active job per physical slot.
    active_by_key: Dict[str, str] = {}
    duplicate_active: List[Tuple[str, str, str]] = []
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l not in ("running", "testing"):
            continue
        key = job_slot_lock_key(job)
        if not key:
            # An active job without a physical slot is unsafe; quarantine it.
            job["status"] = "failed"
            job["finished_at"] = now_iso()
            job["finished_ts"] = now_ts
            job["message"] = "strict resource engine: active job had no physical slot assignment"
            _record_recent_job(state, jid)
            changed = True
            jobs[jid] = job
            continue
        if key in active_by_key:
            duplicate_active.append((jid, key, active_by_key[key]))
        else:
            active_by_key[key] = jid

    for jid, key, owner_id in duplicate_active:
        job = jobs.get(jid, {})
        if not isinstance(job, dict):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l == "testing":
            # Safest recovery for duplicate testing session: return to queue for a clean reprogram later.
            job["status"] = "queued"
            job["message"] = f"strict resource conflict: slot already owned by {owner_id}; returned to FIFO queue"
            job["last_wait_reason"] = "strict_duplicate_slot_owner"
            job["last_wait_at"] = now_iso()
            _strict_clear_active_fields(job)
            jobs[jid] = job
            if jid not in state.setdefault("queue", []):
                state["queue"].append(jid)
        else:
            # Do not allow two programming runners to claim one JTAG cable.
            job["status"] = "failed"
            job["finished_at"] = now_iso()
            job["finished_ts"] = now_ts
            job["message"] = f"strict resource conflict: duplicate running owner for {key}; kept {owner_id}"
            _record_recent_job(state, jid)
            jobs[jid] = job
            with QUEUE_JOB_THREADS_LOCK:
                QUEUE_JOB_THREADS.pop(str(jid), None)
        changed = True

    # Refresh active owners after duplicate cleanup.
    active_by_key = {}
    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l in ("running", "testing"):
            key = job_slot_lock_key(job)
            if key:
                active_by_key[key] = jid
                board = str(job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or "")
                cable = str(job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or "")
                if board and cable:
                    expected = int(job.get("test_seconds", 0) or 0) if status_l == "testing" else int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(board))
                    lock = locks.get(key, {}) if isinstance(locks.get(key, {}), dict) else {}
                    if lock.get("job_id") != jid or not lock.get("busy") or lock.get("phase") != status_l:
                        locks[key] = {
                            "busy": True,
                            "owner": f"strict:{status_l}:{jid}",
                            "job_id": jid,
                            "phase": status_l,
                            "board": board,
                            "detected_cable": cable,
                            "lock_key": key,
                            "locked_at": float(job.get("test_start_ts" if status_l == "testing" else "started_ts", now_ts) or now_ts),
                            "locked_at_iso": job.get("test_start_at" if status_l == "testing" else "started_at") or now_iso(),
                            "expected_seconds": max(1, expected),
                        }
                        changed = True

    # 4) Clear stale busy locks that do not correspond to an active job.
    for key, lock in list(locks.items()):
        if not isinstance(lock, dict) or not lock.get("busy"):
            continue
        if key not in active_by_key:
            locks[key] = {
                "busy": False,
                "released_at": now_ts,
                "released_at_iso": now_iso(),
                "reason": f"{reason}:stale_lock_without_active_job",
                "board": lock.get("board", ""),
                "detected_cable": lock.get("detected_cable", ""),
                "lock_key": key,
                "cleared": True,
                "clear_mode": "strict_stale_lock_clear",
            }
            changed = True

    # 5) Rebuild current running lists and queue order from job states.
    running_ids = [jid for jid, job in jobs.items() if isinstance(job, dict) and str(job.get("status") or "").lower() == "running"]
    running_ids.sort(key=lambda jid: (float(jobs.get(jid, {}).get("started_ts", 0) or 0), str(jid)))
    if state.get("current_jobs") != running_ids:
        state["current_jobs"] = running_ids
        changed = True
    new_current = running_ids[0] if running_ids else None
    if state.get("current_job") != new_current:
        state["current_job"] = new_current
        changed = True

    waiting_ids = strict_fifo_waiting_ids(state)
    if state.get("queue") != waiting_ids:
        state["queue"] = waiting_ids
        changed = True

    state["strict_resource_engine"] = {
        "enabled": True,
        "last_reconciled_at": now_iso(),
        "active_slot_count": len(active_by_key),
        "waiting_count": len(waiting_ids),
        "policy": "one active job per physical JTAG slot; receiving/uploading cannot own hardware; FIFO waiting order",
    }
    state["jobs"] = jobs
    state["locks"] = locks
    return changed

def queue_snapshot(fast: bool = False) -> Dict[str, Any]:
    # Fast mode is for live GUI/SSE polling. It must not SSH or run JTAG discovery,
    # but it may do local state repair so the UI does not show running 00:00 forever.
    if fast:
        state = load_state()
        repaired = False
        if recover_staged_tmp_jobs(state):
            repaired = True
        if recover_stuck_upload_jobs(state):
            repaired = True
        if recover_orphan_uploaded_jobs(state):
            repaired = True
        if recover_stuck_running_jobs(state):
            repaired = True
        if requeue_jobs_assigned_to_disabled_slots(state, reason="fast_queue_snapshot_disabled_slot"):
            repaired = True
        if strict_resource_reconcile(state, reason="fast_queue_snapshot"):
            repaired = True
        if repaired:
            save_state_preserving_concurrent_jobs(state)
    else:
        cleanup_daily_state_rollover()
        cleanup_expired_locks()
        state = load_state()
        conflict_repaired = repair_active_slot_conflicts(state)
        upload_repaired = recover_stuck_upload_jobs(state)
        orphan_repaired = recover_orphan_uploaded_jobs(state)
        running_repaired = recover_stuck_running_jobs(state)
        disabled_repaired = requeue_jobs_assigned_to_disabled_slots(state, reason="forced_queue_snapshot_disabled_slot")
        strict_repaired = strict_resource_reconcile(state, reason="forced_queue_snapshot")
        if conflict_repaired or upload_repaired or orphan_repaired or running_repaired or disabled_repaired or strict_repaired:
            state = annotate_queue_assignments(state)
            save_state_preserving_concurrent_jobs(state)
    if not fast:
        before_plan = json.dumps(state.get("queue_plan", {}), sort_keys=True, ensure_ascii=False)
        before_jobs = json.dumps({
            jid: {
                "planned_board": job.get("planned_board", ""),
                "planned_instance_id": job.get("planned_instance_id", ""),
                "planned_jtag_cable": job.get("planned_jtag_cable", ""),
                "wait_seconds": job.get("wait_seconds", 0),
                "status": job.get("status", ""),
            }
            for jid, job in state.get("jobs", {}).items()
        }, sort_keys=True, ensure_ascii=False)
        state = annotate_queue_assignments(state)
        after_plan = json.dumps(state.get("queue_plan", {}), sort_keys=True, ensure_ascii=False)
        after_jobs = json.dumps({
            jid: {
                "planned_board": job.get("planned_board", ""),
                "planned_instance_id": job.get("planned_instance_id", ""),
                "planned_jtag_cable": job.get("planned_jtag_cable", ""),
                "wait_seconds": job.get("wait_seconds", 0),
                "status": job.get("status", ""),
            }
            for jid, job in state.get("jobs", {}).items()
        }, sort_keys=True, ensure_ascii=False)
        if before_plan != after_plan or before_jobs != after_jobs:
            save_state_preserving_concurrent_jobs(state)
    jobs = state.get("jobs", {})
    out_jobs = []
    running_jobs = []
    current_job = None
    snapshot_now = time.time()
    live_slot_cursor = build_live_slot_cursor_from_state(state, snapshot_now)
    eta_cursor = snapshot_now

    # Show running and active testing jobs. Testing jobs do not block the worker,
    # but their board/JTAG remains locked until the timer expires.
    running_ids = []
    if state.get("current_job"):
        running_ids.append(state.get("current_job"))
    for jid, raw_job in jobs.items():
        if raw_job.get("status") in ("running", "testing") and jid not in running_ids:
            running_ids.append(jid)

    for jid in running_ids:
        job = dict(jobs.get(jid, {}))
        if not job:
            continue
        if job.get("status") == "testing":
            timing = timing_fields(
                float(job.get("test_start_ts", 0) or 0),
                int(job.get("test_seconds", job.get("estimated_seconds", 0)) or 0),
                active=True,
                end_ts=float(job.get("test_end_ts", 0) or 0),
                now_ts=snapshot_now
            )
            job.update(timing)
            rem = job.get("remaining_seconds", 0)
            job["message"] = f"testing: {rem//60}m {rem%60}s remaining on {job.get('jtag_instance') or 'JTAG'} | {job.get('jtag_cable') or ''}"
        else:
            # Running means the controller is copying the SOF and/or quartus_pgm is active.
            # v4.13: display the same physical-slot lock countdown used by the JTAG table.
            # This prevents a confusing running 00:00 while the JTAG slot still shows busy.
            board_for_timer = job.get("planned_board") or job.get("selected_board") or job.get("ai_selected_board") or job.get("requested_board") or ""
            deadline_ts = running_deadline_ts_for_job(state, job, snapshot_now)
            started_ts = float(job.get("started_ts", 0) or 0)
            expected = int(max(0, (deadline_ts - started_ts))) if deadline_ts and started_ts else int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(str(board_for_timer)))
            job.update(timing_fields(
                started_ts,
                expected,
                active=True,
                end_ts=deadline_ts,
                now_ts=snapshot_now
            ))
            rem = int(job.get("remaining_seconds", 0) or 0)
            phase = str(job.get("running_phase") or "programming")
            if rem <= 0 and _running_thread_alive(jid):
                job["remaining_seconds"] = 1
                rem = 1
            job["message"] = f"{phase}: {rem//60}m {rem%60:02d}s remaining on {job.get('planned_instance_id') or job.get('jtag_instance') or 'JTAG'} | {job.get('planned_jtag_cable') or job.get('jtag_cable') or ''}"
        public_job = public_queue_job(job)
        running_jobs.append(public_job)
        if jid == state.get("current_job"):
            current_job = public_job
            eta_cursor = time.time() + int(job.get("remaining_seconds", 0) or 0)

    for job_id in sorted_waiting_display_job_ids(state):
        job = dict(jobs.get(job_id, {}))
        if not job:
            continue

        status_l = str(job.get("status") or "").lower()
        if status_l in ("receiving", "uploading"):
            # Visible immediately, but not runnable until upload is attached.
            job["wait_seconds"] = 0
            deadline = float(job.get("receive_deadline_ts", 0) or 0)
            if deadline:
                job["remaining_seconds"] = countdown_seconds_until(deadline, snapshot_now)
            else:
                job["remaining_seconds"] = 0
            job.setdefault("planned_instance_id", "uploading")
            job["message"] = "uploading files to Raspberry Pi; waiting for upload to finish"
            out_jobs.append(public_queue_job(job))
            continue

        live_wait_seconds, live_planned_start = live_wait_eta_for_queued_job(job, live_slot_cursor, snapshot_now)
        planned_start = live_planned_start or float(job.get("planned_start_ts", 0) or 0)

        job.update(timing_fields(
            float(job.get("started_ts", 0) or 0),
            int(job.get("estimated_seconds", 0) or 0),
            active=False,
            end_ts=float(job.get("finished_ts", 0) or 0),
            eta_base_ts=planned_start or eta_cursor,
            now_ts=snapshot_now
        ))

        # Live display fields. Wait and Remain for queued jobs must be the same
        # and must count down from the active slot owner's real timer.
        job["wait_seconds"] = int(max(0, live_wait_seconds))
        job["remaining_seconds"] = job["wait_seconds"]
        if planned_start:
            job["planned_start_ts"] = planned_start
            job["planned_start_at"] = iso_from_ts(planned_start)
        if job.get("planned_instance_id"):
            position = int(job.get("queue_position_for_slot", 1) or 1)
            phrase = f"You are next in line for {job.get('planned_instance_id')}" if position == 1 else f"You are #{position} in line for {job.get('planned_instance_id')}"
            job["queue_position_message"] = f"{phrase} ({job.get('planned_board', '')})"
            job["message"] = f"waiting: {job['queue_position_message']}. ETA to start: {queue_format_duration(job['wait_seconds'])}."

        eta_cursor = max(eta_cursor, job.get("estimated_finish_ts", eta_cursor) or eta_cursor)
        out_jobs.append(public_queue_job(job))

    # v4.54: terminal jobs must remain visible even if an older concurrent
    # planner snapshot temporarily lost the recent_jobs ID list. Reconstruct the
    # list from terminal job objects, then merge it with the saved order.
    recent_ids: List[str] = []
    for job_id in list(state.get("recent_jobs", []) or []):
        job_id = str(job_id or "")
        if job_id and job_id in jobs:
            if job_id in recent_ids:
                recent_ids.remove(job_id)
            recent_ids.append(job_id)

    terminal_ids = [
        jid for jid, item in jobs.items()
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() in {"completed", "failed", "cancelled"}
    ]
    terminal_ids.sort(
        key=lambda jid: float(
            jobs.get(jid, {}).get("finished_ts")
            or jobs.get(jid, {}).get("test_end_ts")
            or jobs.get(jid, {}).get("created_ts")
            or 0
        )
    )
    for job_id in terminal_ids:
        if job_id in recent_ids:
            recent_ids.remove(job_id)
        recent_ids.append(job_id)

    recent = []
    recent_seen = set()
    for job_id in recent_ids[-12:][::-1]:
        if job_id in recent_seen:
            continue
        recent_seen.add(job_id)
        job = dict(jobs.get(job_id, {}))
        if not job:
            continue
        if job.get("status") == "testing":
            job.update(timing_fields(float(job.get("test_start_ts", 0) or 0), int(job.get("test_seconds", 0) or 0), active=True, end_ts=float(job.get("test_end_ts", 0) or 0), now_ts=snapshot_now))
        else:
            job.update(timing_fields(float(job.get("started_ts", 0) or 0), int(job.get("estimated_seconds", 0) or 0), active=False, end_ts=float(job.get("finished_ts", 0) or 0), now_ts=snapshot_now))
        recent.append(public_queue_job(job))

    return {
        "success": True,
        "timestamp": iso_from_ts(snapshot_now),
        "timer_sync": {
            "enabled": True,
            "server_snapshot_ts": snapshot_now,
            "rounding": "ceil_deadline_minus_snapshot",
            "note": "Wait and Remain are computed from the same server timestamp per queue snapshot."
        },
        "current_job": current_job,
        "running_jobs": running_jobs,
        "queued_jobs": out_jobs,
        "recent_jobs": recent,
        "running_count": len([j for j in running_jobs if j.get("status") == "running"]),
        "analyzing_count": len([j for j in running_jobs if j.get("status") == "analyzing"]),
        "testing_count": len([j for j in running_jobs if j.get("status") == "testing"]),
        "active_runner_threads": _running_thread_count(),
        "queue_length": len(out_jobs),
        "queue_plan": state.get("queue_plan", {}),
        "teacher_override_events": state.get("teacher_override_events", [])[-10:],
        "slot_clear_events": state.get("slot_clear_events", [])[-10:],
    }


def _record_recent_job(state: Dict[str, Any], job_id: str) -> None:
    """Keep recent job IDs unique so the GUI never renders duplicate rows."""
    if not job_id:
        return
    recent = state.setdefault("recent_jobs", [])
    recent[:] = [jid for jid in recent if jid != job_id]
    recent.append(job_id)
    if len(recent) > 25:
        del recent[:-25]



def _tail_text(value: Any, limit: int = 1200) -> str:
    """Return a compact tail of stdout/stderr for queue display and history."""
    try:
        text = str(value or "")
    except Exception:
        return ""
    text = text.replace("\r", "")
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text.strip()


def summarize_program_failure(result: Dict[str, Any]) -> str:
    """Produce a useful user-visible failure reason without hiding Quartus output."""
    if not isinstance(result, dict):
        return "failed"
    for key in ("reason", "error", "fallback_reason"):
        val = str(result.get(key) or "").strip()
        if val:
            return _tail_text(val, 900)
    pr = result.get("program_result") if isinstance(result.get("program_result"), dict) else {}
    # Quartus usually writes useful diagnostics to stdout, sometimes stderr.
    stderr = _tail_text(pr.get("stderr"), 900)
    stdout = _tail_text(pr.get("stdout"), 900)
    rc = pr.get("returncode", result.get("returncode", ""))
    if stderr:
        return f"quartus_pgm failed rc={rc}: {stderr}"
    if stdout:
        return f"quartus_pgm failed rc={rc}: {stdout}"
    if rc != "":
        return f"quartus_pgm failed returncode={rc}"
    return "quartus_pgm failed"


def _finalize_job(state: Dict[str, Any], job_id: str, status: str, result: Dict[str, Any]) -> None:
    job = state.setdefault("jobs", {}).setdefault(job_id, {})
    now_ts = time.time()
    test_seconds = int((result or {}).get("test_seconds") or job.get("test_seconds") or 0)
    success = bool((result or {}).get("success"))
    job["result"] = result
    for key in (
        "selected_board",
        "ai_selected_board",
        "selected_jtag_cable",
        "jtag_cable",
        "selected_instance_id",
        "jtag_instance",
        "selected_lock_key",
        "remote_sof",
        "program_seconds",
        "jtag_usage_after",
        "test_seconds",
        "test_minutes",
        "test_timer",
        "held_for_testing",
    ):
        if isinstance(result, dict) and key in result:
            job[key] = result.get(key)
    if not job.get("jtag_cable") and job.get("selected_jtag_cable"):
        job["jtag_cable"] = job.get("selected_jtag_cable")
    if not job.get("jtag_instance") and job.get("selected_instance_id"):
        job["jtag_instance"] = job.get("selected_instance_id")

    if success and test_seconds > 0:
        job["status"] = "testing"
        job["test_start_at"] = now_iso()
        job["test_start_ts"] = now_ts
        job["test_end_ts"] = now_ts + test_seconds
        job["test_end_at"] = iso_from_ts(now_ts + test_seconds)
        job["estimated_seconds"] = test_seconds
        job["remaining_seconds"] = test_seconds
        job["running_phase"] = "testing"
        job["message"] = f"student test timer active: {int(round(test_seconds / 60))} min on {job.get('jtag_instance') or 'JTAG'} | {job.get('jtag_cable') or ''}"
    else:
        job["status"] = status
        job["finished_at"] = now_iso()
        job["finished_ts"] = now_ts
        if success:
            used = job.get("jtag_instance") or "JTAG"
            cable = job.get("jtag_cable") or ""
            job["message"] = f"completed on {used} | {cable}"
        else:
            job["message"] = summarize_program_failure(result)
            job["failure_reason"] = job["message"]
            try:
                pr = result.get("program_result") if isinstance(result, dict) else {}
                if isinstance(pr, dict):
                    job["program_returncode"] = pr.get("returncode", "")
                    job["program_command"] = pr.get("command", "")
                    job["quartus_stdout_tail"] = _tail_text(pr.get("stdout"), 3000)
                    job["quartus_stderr_tail"] = _tail_text(pr.get("stderr"), 3000)
            except Exception:
                pass

    if job.get("started_ts") and job.get("finished_ts"):
        job["elapsed_seconds"] = max(0, int(float(job.get("finished_ts", 0)) - float(job.get("started_ts", 0))))
    elif job.get("started_ts") and job.get("status") == "testing":
        job["elapsed_seconds"] = max(0, int(time.time() - float(job.get("started_ts", 0))))

    try:
        if job.get("status") in ("testing", "completed", "failed"):
            if job.get("status") == "testing":
                hist_event = "job_testing_started"
            elif job.get("status") == "completed":
                hist_event = "job_completed"
            else:
                hist_event = "job_failed"
            # v4.44: do not block the programming thread on SSH/SFTP history I/O.
            # The GUI should be notified that the FPGA is ready for testing as soon
            # as quartus_pgm succeeds. The async writer uses one stable filename per
            # job_id, so repeated lifecycle events do not create duplicate records.
            job["server_history_async_requested"] = write_job_history_to_server_async(job_id, dict(job), hist_event)
            job["server_history_pending_event"] = hist_event
    except Exception as e:
        job["server_history"] = fail(f"server history logging failed: {e}")

    if job.get("status") in ("testing", "completed", "failed", "cancelled"):
        job["finished_job_temp_cleanup"] = cleanup_finished_job_temp_files(job, f"cleanup_after_{job.get('status')}")

    state["jobs"][job_id] = job
    if state.get("current_job") == job_id:
        state["current_job"] = None
    state["queue"] = [q for q in state.get("queue", []) if q != job_id]
    _record_recent_job(state, job_id)


def process_queued_job(job: Dict[str, Any]) -> Dict[str, Any]:
    kind = job.get("kind")
    # Use the pre-assigned physical slot when the smart queue planner selected one.
    requested_board = job.get("locked_target_jtag_cable") or job.get("planned_jtag_cable") or job.get("planned_instance_id") or job.get("requested_board") or None
    client_hostname = job.get("client_hostname") or "unknown"
    student_ip = job.get("student_ip") or "unknown"
    queue_job_id = job.get("job_id") or ""
    test_seconds = int(job.get("test_seconds", test_seconds_from_value(job.get("test_minutes", default_test_minutes()))) or 0)
    qsf_text = queue_job_qsf_text(job)
    if kind == "server_paths":
        return perform_deploy_server_paths(job.get("verilog_path", ""), job.get("sof_path", ""), requested_board, client_hostname, student_ip, job_id=queue_job_id, test_seconds=test_seconds, qsf_text=qsf_text, qsf_remote_path=str(job.get("qsf_path") or ""))
    if kind == "code_server_sof":
        return perform_deploy_code_server_sof(job.get("verilog_code", ""), job.get("filename", "design.v"), job.get("sof_path", ""), requested_board, client_hostname, student_ip, job_id=queue_job_id, test_seconds=test_seconds, qsf_text=qsf_text)
    if kind == "upload":
        # v4.09: never program a half-created upload job. Path("") is '.',
        # which made v4.08 sometimes fail instantly with a false Verilog-extension error.
        ready, reason = upload_job_ready_to_program(job)
        if not ready:
            return fail(f"Upload job is not ready to program yet: {reason}", system_wait=True, requeue_upload=True)
        if job_has_valid_server_archive_paths(job):
            return perform_deploy_server_paths(
                job.get("verilog_path") or job.get("archived_verilog_path") or "",
                job.get("sof_path") or job.get("archived_sof_path") or job.get("remote_sof") or "",
                requested_board, client_hostname, student_ip, job_id=queue_job_id, test_seconds=test_seconds, qsf_text=qsf_text, qsf_remote_path=str(job.get("qsf_path") or ""),
            )
        if job.get("temporary_stage_cache") and not (is_valid_verilog_file_path(job.get("verilog_local_path")) and is_valid_sof_file_path(job.get("sof_local_path"))):
            job, ok_stage, stage_reason = activate_staged_job_files(queue_job_id, job)
            if not ok_stage:
                return fail(f"Upload job stage could not be activated: {stage_reason}", system_wait=True, requeue_upload=True)
        return perform_deploy(Path(job.get("verilog_local_path", "")), Path(job.get("sof_local_path", "")), requested_board, client_hostname, student_ip, job_id=queue_job_id, test_seconds=test_seconds, qsf_text=queue_job_qsf_text(job), qsf_path=str(job.get("qsf_local_path") or job.get("stage_qsf_active_path") or ""))
    if kind == "server_verilog_local_sof":
        result = perform_deploy(Path(job.get("verilog_local_copy_path", "")), Path(job.get("sof_local_path", "")), requested_board, client_hostname, student_ip, job_id=queue_job_id, test_seconds=test_seconds, qsf_text=qsf_text, qsf_path=str(job.get("qsf_local_path") or ""))
        if isinstance(result, dict):
            result.setdefault("verilog_path", job.get("verilog_path", ""))
            result.setdefault("mode", "server_verilog_local_sof")
        return result
    return fail(f"Unknown queued job kind: {kind}")


def should_requeue_for_busy_resources(result: Dict[str, Any]) -> bool:
    """Do not fail a queued job just because every matching JTAG is currently in use/testing."""
    if not isinstance(result, dict) or result.get("success"):
        return False
    text = json.dumps(result, ensure_ascii=False).lower()
    patterns = [
        "no available jtag board instance",
        "requested board/instance is not available",
        "board instance is already busy",
        "already busy",
        "no jtag cable was selected",
    ]
    return any(p in text for p in patterns)



def _running_thread_alive(job_id: str) -> bool:
    with QUEUE_JOB_THREADS_LOCK:
        t = QUEUE_JOB_THREADS.get(str(job_id))
        return bool(t and t.is_alive())


def _running_thread_count() -> int:
    with QUEUE_JOB_THREADS_LOCK:
        dead = [jid for jid, t in QUEUE_JOB_THREADS.items() if not t.is_alive()]
        for jid in dead:
            QUEUE_JOB_THREADS.pop(jid, None)
        return len(QUEUE_JOB_THREADS)


def _set_lock_testing_in_state(state: Dict[str, Any], board: str, cable: str, job_id: str, test_seconds: int, reason: str = "programming_success_recovered") -> None:
    if not board or not cable:
        return
    key = instance_lock_key(board, cable)
    now_ts = time.time()
    test_seconds = max(0, int(test_seconds or 0))
    if test_seconds <= 0:
        state.setdefault("locks", {})[key] = {
            "busy": False,
            "released_at": now_ts,
            "released_at_iso": now_iso(),
            "reason": "no_test_timer_recovered",
            "board": board,
            "detected_cable": cable,
            "lock_key": key,
        }
        return
    state.setdefault("locks", {})[key] = {
        "busy": True,
        "owner": f"testing:{job_id}",
        "locked_at": now_ts,
        "locked_at_iso": now_iso(),
        "expected_seconds": test_seconds,
        "job_id": job_id,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
        "phase": "testing",
        "reason": reason,
        "test_seconds": test_seconds,
        "test_minutes": int(round(test_seconds / 60)),
        "test_end_ts": now_ts + test_seconds,
        "test_end_at": iso_from_ts(now_ts + test_seconds),
    }


def recover_stuck_running_jobs(state: Dict[str, Any]) -> bool:
    """Repair running jobs that reached 00:00 but never transitioned."""
    changed = False
    now_ts = time.time()
    jobs = state.setdefault("jobs", {})
    usage = state.setdefault("jtag_usage", {})
    # v4.14: board-aware timeout. DE1-SoC should not inherit the Agilex/global 900s limit.
    program_timeout_default = int(load_config().get("quartus_server", {}).get("standard_program_timeout_seconds", 120) or 120)

    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict) or str(job.get("status") or "").lower() != "running":
            continue
        started_ts = float(job.get("started_ts", 0) or 0)
        if not started_ts:
            continue
        elapsed = now_ts - started_ts
        board = str(job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or "")
        cable = str(job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or "")
        key = job.get("selected_lock_key") or job.get("planned_lock_key") or (instance_lock_key(board, cable) if board and cable else "")
        u = usage.get(key, {}) if key else {}

        if key and key in state.setdefault("disabled_jtag", {}):
            if board and cable:
                if _running_thread_alive(job_id):
                    quarantine_old_runner_slot(state, board, cable, job_id, reason="running_slot_disabled_abandoned_runner")
                else:
                    clear_board_slot_state(state, board, cable, reason="running_slot_disabled_requeue", job_id=job_id)
            retry_count = int(job.get("system_retry_count", 0) or 0) + 1
            job["abandoned_run_generation"] = int(job.get("run_generation", 0) or 0)
            _strict_clear_active_fields(job)
            for k in ("planned_board", "planned_instance_id", "planned_jtag_cable", "planned_lock_key", "planned_start_at", "planned_start_ts"):
                job.pop(k, None)
            job["status"] = "queued"
            job["system_retry_count"] = retry_count
            job["last_system_recovery_at"] = now_iso()
            job["last_system_recovery_reason"] = f"running slot disabled: {key}"
            job["message"] = f"slot {cable or key} disabled; automatically requeued for another enabled board"
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            with QUEUE_JOB_THREADS_LOCK:
                QUEUE_JOB_THREADS.pop(str(job_id), None)
            changed = True
            continue

        # Strongest recovery: quartus_pgm already succeeded for this same job/cable.
        if u and str(u.get("last_job_id") or "") == str(job_id) and bool(u.get("last_success")):
            test_seconds = int(job.get("test_seconds", 0) or 0)
            _set_lock_testing_in_state(state, board, cable, job_id, test_seconds, reason="watchdog_program_success")
            recovered_result = ok(
                recovered_by="running_success_watchdog",
                success=True,
                selected_board=board,
                ai_selected_board=board,
                selected_jtag_cable=cable,
                jtag_cable=cable,
                selected_instance_id=job.get("selected_instance_id") or job.get("jtag_instance") or job.get("planned_instance_id") or "",
                jtag_instance=job.get("jtag_instance") or job.get("selected_instance_id") or job.get("planned_instance_id") or "",
                selected_lock_key=key,
                jtag_usage_after=u,
                program_seconds=int(u.get("last_program_seconds", max(0, elapsed)) or max(0, elapsed)),
                test_seconds=test_seconds,
                test_minutes=int(round(test_seconds / 60)) if test_seconds else 0,
                held_for_testing=test_seconds > 0,
                program_result={"success": True, "recovered_by": "usage_log_success"},
            )
            job["message"] = "recovered: programming succeeded; moving to testing"
            jobs[job_id] = job
            _finalize_job(state, job_id, "completed", recovered_result)
            add_history("running_success_watchdog_recovered", board, {"job_id": job_id, "slot": job.get("planned_instance_id", ""), "cable": cable})
            changed = True
            continue

        # v4.13: if the Python runner thread is gone but the job is still marked
        # running, do not wait for the full Quartus timeout. That means finalization
        # was lost/crashed, not that the student SOF failed. Requeue quickly so FIFO
        # continues and the board is not held for minutes with no active worker.
        quick_lost_seconds = int(strict_resource_engine_config().get("runner_lost_quick_requeue_seconds", 15) or 15)
        if elapsed > quick_lost_seconds and not _running_thread_alive(job_id):
            if board and cable:
                clear_board_slot_state(state, board, cable, reason="runner_lost_quick_requeue", job_id=job_id)
            retry_count = int(job.get("system_retry_count", 0) or 0) + 1
            _strict_clear_active_fields(job)
            job["status"] = "queued"
            job["system_retry_count"] = retry_count
            job["last_system_recovery_at"] = now_iso()
            job["last_system_recovery_reason"] = f"runner thread missing after {int(elapsed)}s"
            job["message"] = f"system recovered: runner thread missing; automatically requeued attempt {retry_count}"
            job["estimated_seconds"] = estimate_deploy_seconds(board or job.get("planned_board") or job.get("target_board_hint") or "", int(job.get("test_seconds", 0) or 0))
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            with QUEUE_JOB_THREADS_LOCK:
                QUEUE_JOB_THREADS.pop(str(job_id), None)
            add_history("running_runner_lost_quick_requeued", board, {"job_id": job_id, "elapsed": int(elapsed), "cable": cable, "retry_count": retry_count})
            changed = True
            continue

        # A stale or hung running job must not block FIFO forever. The remote
        # quartus command is also wrapped in a Linux timeout, so after this point
        # it is safe to mark the job failed and free the logical slot.
        expected = int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(board))
        grace = strict_running_watchdog_grace_seconds()
        # Strict engine: the remote command has its own Linux/SSH timeout. The UI/state
        # should not remain running 00:00 for several minutes. Use board-aware expected
        # programming time plus a small grace period.
        # v4.05: never use a short display estimate as the real programming
        # timeout. Older builds could fail/requeue around 60-120s even while
        # Quartus/JTAG was still legitimately working. The hard timeout now
        # follows the configured Quartus timeout plus grace, with extra room
        # for Agilex/Pro boards.
        board_timeout = quartus_program_timeout_for_board(board) if board else program_timeout_default
        copy_timeout = sof_copy_timeout_for_board(board) if board else 60
        hard_timeout = max(expected + min(grace, 60), board_timeout + copy_timeout + min(grace, 60), 90)
        if "agilex" in str(board).lower():
            hard_timeout = max(hard_timeout, 900)
        else:
            hard_timeout = min(hard_timeout, int((strict_resource_engine_config() or {}).get("standard_programming_watchdog_max_seconds", 240) or 240))
        if elapsed > hard_timeout:
            runner_alive = _running_thread_alive(job_id)
            stale_cfg = stale_runner_config_for_board(board)
            force_alive_after = int(stale_cfg.get("force_requeue_after", hard_timeout) or hard_timeout)
            if runner_alive and elapsed <= force_alive_after:
                job["message"] = f"programming still active after {int(elapsed)}s; waiting for quartus_pgm result"
                job["remaining_seconds"] = 1
                jobs[job_id] = job
                changed = True
                continue

            if board and cable:
                if runner_alive:
                    quarantine_old_runner_slot(state, board, cable, job_id, reason="alive_runner_force_requeue")
                else:
                    clear_board_slot_state(state, board, cable, reason="runner_lost_requeue", job_id=job_id)
            retry_count = int(job.get("system_retry_count", 0) or 0) + 1
            job["abandoned_run_generation"] = int(job.get("run_generation", 0) or 0)
            _strict_clear_active_fields(job)
            job["status"] = "queued"
            job["system_retry_count"] = retry_count
            job["last_system_recovery_at"] = now_iso()
            if runner_alive:
                job["last_system_recovery_reason"] = f"stale runner force-requeued after {int(elapsed)}s"
                job["message"] = f"system recovered: programming runner took too long; automatically requeued attempt {retry_count}"
            else:
                job["last_system_recovery_reason"] = f"runner disappeared after {int(elapsed)}s"
                job["message"] = f"system recovered: runner disappeared after {int(elapsed)}s; automatically requeued attempt {retry_count}"
            job["estimated_seconds"] = estimate_deploy_seconds(board or job.get("planned_board") or job.get("target_board_hint") or "", int(job.get("test_seconds", 0) or 0))
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            with QUEUE_JOB_THREADS_LOCK:
                QUEUE_JOB_THREADS.pop(str(job_id), None)
            add_history("running_timeout_watchdog_requeued", board, {"job_id": job_id, "elapsed": int(elapsed), "cable": cable, "retry_count": retry_count, "runner_alive": bool(runner_alive)})
            changed = True

    state["jobs"] = jobs
    active_running = [jid for jid, j in jobs.items() if isinstance(j, dict) and j.get("status") == "running"]
    state["current_jobs"] = active_running
    if state.get("current_job") not in active_running:
        state["current_job"] = active_running[0] if active_running else None
    return changed




def _is_lightweight_tmp_handoff_job(job: Dict[str, Any]) -> bool:
    """True for the new queue mode where full files stay only in Pi temp stage.

    These jobs should never be sent through the old background Quartus-server
    archive thread.  The permanent server history is only the small text record;
    programming uses stage_*.tmp(.gz) files at handoff time.
    """
    policy = str(job.get("archive_policy") or "")
    upload_stage = str(job.get("upload_stage") or "")
    return bool(
        policy.startswith("lightweight_text_record_only")
        or job.get("temporary_stage_cache")
        or job.get("temporary_pi_spool")
        or job.get("no_pi_student_file_storage")
        or upload_stage.startswith("queued_tmp_staged")
        or job.get("stage_manifest_path")
        or job.get("stage_sof_tmp_path")
        or job.get("stage_sof_active_path")
    )


def _path_exists_nonempty(value: Any) -> bool:
    try:
        if not value:
            return False
        p = Path(str(value))
        return p.exists() and p.is_file() and p.stat().st_size >= 0
    except Exception:
        return False


def _has_valid_tmp_stage(job: Dict[str, Any]) -> bool:
    """Return True when a queued temporary stage package is usable."""
    v_ok = _path_exists_nonempty(job.get("stage_verilog_tmp_path")) or _path_exists_nonempty(job.get("stage_verilog_active_path"))
    s_ok = _path_exists_nonempty(job.get("stage_sof_tmp_path")) or _path_exists_nonempty(job.get("stage_sof_active_path"))
    # QSF is optional; .v and .sof are required.
    return bool(v_ok and s_ok)


def _repair_lightweight_stage_to_queued(job_id: str, job: Dict[str, Any], now_ts: Optional[float] = None) -> bool:
    """Mutate a stuck lightweight upload row back to queued when stage files exist."""
    if not _is_lightweight_tmp_handoff_job(job) or not _has_valid_tmp_stage(job):
        return False
    now_ts = now_ts or time.time()
    job["status"] = "queued"
    job["kind"] = "upload"
    job["message"] = "queued; validated lightweight temp stage and ready for next free JTAG slot"
    job["upload_stage"] = "queued_tmp_staged_waiting_for_slot"
    job["upload_files_attached"] = True
    job["upload_files_in_progress"] = False
    job["upload_finished_at"] = job.get("upload_finished_at") or now_iso()
    job["upload_finished_ts"] = float(job.get("upload_finished_ts", 0) or now_ts)
    job["temporary_stage_cache"] = True
    job["temporary_pi_spool"] = True
    job["no_pi_student_file_storage"] = True
    job["pi_file_storage"] = False
    job["archive_policy"] = "lightweight_text_record_only_smallest_tmp_queue_handoff"
    job["history_policy"] = "small_text_record_only"
    job["sof_source"] = "queued_tmp_stage_runtime_passthrough"
    # v5.04: once a lightweight temp-stage package exists, this job must never
    # be driven by the legacy Quartus-server archive retry path again.  Clear
    # stale archive/upload fields so the GUI does not remain stuck at uploading
    # and so Cancel Selected Job remains enabled for the real queued job.
    job["archive_disabled_by_lightweight_queue"] = True
    job["archive_retry_count"] = 0
    job["archive_attempt"] = 0
    job.pop("last_upload_error", None)
    job.pop("archive_thread_last_seen_at", None)
    job.pop("archive_thread_last_seen_ts", None)
    job.pop("archive_thread_started_at", None)
    job["wait_seconds"] = 0
    job["remaining_seconds"] = 0
    job.pop("receive_deadline_ts", None)
    if str(job.get("planned_instance_id") or "") == "uploading":
        job.pop("planned_instance_id", None)
    if str(job.get("planned_slot") or "") == "uploading":
        job.pop("planned_slot", None)
    test_seconds = int(job.get("test_seconds", 0) or 0)
    requested_board = job.get("requested_board") or job.get("target_board_hint") or ""
    job["estimated_seconds"] = estimate_deploy_seconds(requested_board, test_seconds)
    return True

def recover_stuck_upload_jobs(state: Dict[str, Any]) -> bool:
    """Repair prequeued jobs left in receiving/uploading.

    Large SOF uploads are allowed to take time, but they should never remain in
    uploading forever. If both files already exist on the Pi, promote the job to
    queued. If the receive deadline passed and files are incomplete, fail it so
    it does not block the queue display.
    """
    changed = False
    now_ts = time.time()
    jobs = state.setdefault("jobs", {})
    uploads_dir = BASE_DIR / load_config().get("uploads_dir", "uploads")

    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l not in ("receiving", "uploading", "queued"):
            continue

        # v5.02: repair the exact state seen in the GUI: history is written,
        # stage_*.tmp(.gz) files exist, but an old archive retry thread changed
        # the row back to uploading.  Put it back into the FIFO queue and allow
        # normal cancellation/dispatch.
        if _repair_lightweight_stage_to_queued(job_id, job, now_ts):
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            changed = True
            add_history("stuck_upload_auto_validated_to_queued", job.get("requested_board") or job.get("target_board_hint") or "", {"job_id": job_id, "stage_manifest_path": job.get("stage_manifest_path", ""), "auto": True})
            continue

        if status_l == "queued":
            continue

        deadline = float(job.get("receive_deadline_ts", 0) or 0)
        if not deadline:
            deadline = now_ts + strict_upload_timeout_seconds()
            job["receive_deadline_ts"] = deadline
            job["upload_deadline_at"] = iso_from_ts(deadline)
            changed = True

        # v4.04 no-Pi-storage recovery: if the files were already archived on the
        # Quartus server, the upload is complete even if an older state snapshot
        # says receiving/uploading. Never fail this as an upload timeout.
        archived_v = str(job.get("archived_verilog_path") or job.get("verilog_path") or "")
        archived_sof = str(job.get("archived_sof_path") or job.get("remote_sof") or job.get("sof_path") or "")
        no_pi_mode = bool(job.get("pi_file_storage") is False or job.get("no_pi_student_file_storage") or str(job.get("archive_policy") or "").startswith("quartus_server_history"))
        if no_pi_mode and archived_v.lower().endswith((".v", ".sv")) and archived_sof.lower().endswith(".sof"):
            job["kind"] = "server_paths"
            job["status"] = "queued"
            job["message"] = "queued; recovered from Quartus server archive after burst upload"
            job["verilog_path"] = archived_v
            job["sof_path"] = archived_sof
            job["remote_sof"] = archived_sof
            job["upload_files_attached"] = True
            job["upload_files_in_progress"] = False
            job["pi_file_storage"] = False
            job["no_pi_student_file_storage"] = True
            job["upload_finished_at"] = job.get("upload_finished_at") or now_iso()
            job["upload_finished_ts"] = float(job.get("upload_finished_ts", 0) or now_ts)
            job.pop("receive_deadline_ts", None)
            if str(job.get("planned_instance_id") or "") == "uploading":
                job.pop("planned_instance_id", None)
            test_seconds = int(job.get("test_seconds", 0) or 0)
            requested_board = job.get("requested_board") or job.get("target_board_hint") or ""
            job["estimated_seconds"] = estimate_deploy_seconds(requested_board, test_seconds)
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            changed = True
            add_history("upload_watchdog_recovered_server_archive", job.get("requested_board") or job.get("target_board_hint") or "", {"job_id": job_id, "archived_sof_path": archived_sof})
            continue

        # v3.97: while /queue/<job_id>/upload_files is actively saving the .v/.sof,
        # do not let the upload watchdog promote the job to queued from a half-saved
        # directory. That race could let the worker start programming before
        # upload_files finalizes, causing a 400 BAD REQUEST in the GUI.
        upload_in_progress = bool(job.get("upload_files_in_progress"))
        upload_started = float(job.get("upload_files_request_started_ts", 0) or 0)
        upload_stage = str(job.get("upload_stage") or "").lower()
        if upload_stage in ("received_temp_spool", "archiving_to_server", "archive_retry"):
            spool = {
                "spool_dir": job.get("spool_dir", ""),
                "spool_verilog_path": job.get("spool_verilog_path", ""),
                "spool_sof_path": job.get("spool_sof_path", ""),
                "verilog_filename": job.get("verilog_filename", ""),
                "sof_filename": job.get("sof_filename", ""),
                "verilog_size_bytes": job.get("verilog_size_bytes", 0),
                "sof_size_bytes": job.get("sof_size_bytes", 0),
                "verilog_code": job.get("verilog_code", ""),
            }
            v_path_spool = Path(str(spool.get("spool_verilog_path") or ""))
            s_path_spool = Path(str(spool.get("spool_sof_path") or ""))
            v_ok = is_valid_verilog_file_path(v_path_spool)
            s_ok = is_valid_sof_file_path(s_path_spool)

            # v4.41: in the current lightweight mode, a completed HTTP upload does
            # not need to be archived to the Quartus server before queueing.  A
            # cancel/resubmit race could leave the second job forever in
            # "uploading" while the old archive-retry path waited.  If the .v and
            # .sof are present in the temporary spool, seal the normal queued
            # .v.tmp/.sof.tmp stage immediately and let the FIFO worker run it.
            lightweight_mode = (
                str(job.get("archive_policy") or "") == "lightweight_text_record_only_staged_tmp_queue_handoff"
                or bool(job.get("temporary_pi_spool") and job.get("no_pi_student_file_storage"))
            )
            if lightweight_mode and v_ok and s_ok:
                staged = create_staged_tmp_files_for_job(job_id, job, spool)
                if staged.get("success"):
                    job.update({
                        "status": "queued",
                        "kind": "upload",
                        "message": "queued; recovered sealed upload stage after cancel/resubmit",
                        "upload_stage": "queued_tmp_staged_waiting_for_slot",
                        "upload_files_attached": True,
                        "upload_files_in_progress": False,
                        "upload_finished_at": job.get("upload_finished_at") or now_iso(),
                        "upload_finished_ts": float(job.get("upload_finished_ts", 0) or now_ts),
                        "stage_dir": staged.get("stage_dir", ""),
                        "stage_manifest_path": staged.get("stage_manifest_path", ""),
                        "stage_verilog_tmp_path": staged.get("stage_verilog_tmp_path", ""),
                        "stage_sof_tmp_path": staged.get("stage_sof_tmp_path", ""),
                        "stage_verilog_active_path": staged.get("stage_verilog_active_path", ""),
                        "stage_sof_active_path": staged.get("stage_sof_active_path", ""),
                        "stage_sof_tmp_compressed": staged.get("stage_sof_tmp_compressed", False),
                        "stage_sof_tmp_size_bytes": staged.get("stage_sof_tmp_size_bytes", staged.get("sof_size_bytes", 0)),
                        "stage_sof_uncompressed_size_bytes": staged.get("stage_sof_uncompressed_size_bytes", staged.get("sof_size_bytes", 0)),
                        "staging_policy": "compressed_sof_tmp_activate_on_slot_handoff" if staged.get("stage_sof_tmp_compressed") else "tmp_files_activate_on_slot_handoff",
                        "verilog_local_path": "",
                        "sof_local_path": "",
                        "verilog_filename": spool.get("verilog_filename", job.get("verilog_filename", "")),
                        "sof_filename": spool.get("sof_filename", job.get("sof_filename", "")),
                        "filename": spool.get("verilog_filename", job.get("filename", "design.v")),
                        "verilog_size_bytes": spool.get("verilog_size_bytes", staged.get("verilog_size_bytes", 0)),
                        "sof_size_bytes": spool.get("sof_size_bytes", staged.get("sof_size_bytes", 0)),
                        "verilog_code": spool.get("verilog_code", job.get("verilog_code", "")),
                        "pi_file_storage": False,
                        "temporary_pi_spool": True,
                        "temporary_stage_cache": True,
                        "no_pi_student_file_storage": True,
                        "archive_policy": "lightweight_text_record_only_smallest_tmp_queue_handoff",
                        "history_policy": "small_text_record_only",
                        "sof_source": "queued_tmp_stage_runtime_passthrough",
                        "wait_seconds": 0,
                    })
                    job.pop("receive_deadline_ts", None)
                    if str(job.get("planned_instance_id") or "") == "uploading":
                        job.pop("planned_instance_id", None)
                    test_seconds = int(job.get("test_seconds", 0) or 0)
                    requested_board = job.get("requested_board") or job.get("target_board_hint") or ""
                    job["estimated_seconds"] = estimate_deploy_seconds(requested_board, test_seconds)
                    jobs[job_id] = job
                    if job_id not in state.setdefault("queue", []):
                        state["queue"].append(job_id)
                    try:
                        job["temporary_spool_cleanup_after_stage"] = cleanup_temporary_spool_for_job(job)
                    except Exception:
                        pass
                    changed = True
                    add_history("upload_watchdog_recovered_lightweight_stage", requested_board, {"job_id": job_id, "stage_manifest_path": job.get("stage_manifest_path", "")})
                    continue
                else:
                    job["last_upload_error"] = staged.get("error", "queued staging failed")

            # Legacy full-archive path fallback.  Keep it only for old jobs that
            # really require a Quartus-server archive before queueing.
            if v_ok and s_ok and not _archive_thread_alive(job_id):
                start_archive_spooled_upload_thread(job_id, job, spool)
                job["archive_restart_count"] = int(job.get("archive_restart_count", 0) or 0) + 1
                changed = True
            job["wait_seconds"] = 0
            job["remaining_seconds"] = max(0, int(deadline - now_ts)) if deadline else 0
            job["planned_instance_id"] = "uploading"
            job["message"] = "upload received; archiving to Quartus server in background" if _archive_thread_alive(job_id) else "upload staged; waiting to restart Quartus-server archive"
            jobs[job_id] = job
            continue
        if upload_in_progress and upload_started and now_ts <= deadline:
            job["wait_seconds"] = 0
            job["remaining_seconds"] = max(0, int(deadline - now_ts))
            job["planned_instance_id"] = "uploading"
            job["message"] = "receiving upload from GUI; waiting for HTTP body to finish"
            jobs[job_id] = job
            continue

        job_dir = uploads_dir / str(job_id)
        v_path = Path(str(job.get("verilog_local_path") or ""))
        s_path = Path(str(job.get("sof_local_path") or ""))

        # v4.09: Path("") is the current directory (".") and .exists() is True.
        # Only real files with the expected suffix may be recovered/promoted.
        if (not is_valid_verilog_file_path(v_path)) and job_dir.exists():
            candidates = sorted(list(job_dir.glob("*.v")) + list(job_dir.glob("*.sv")))
            if candidates:
                v_path = candidates[0]
        if (not is_valid_sof_file_path(s_path)) and job_dir.exists():
            candidates = sorted(job_dir.glob("*.sof"))
            if candidates:
                s_path = candidates[0]

        if is_valid_verilog_file_path(v_path) and is_valid_sof_file_path(s_path):
            # Compatibility only for older Pi-local uploads. No SOF validation here;
            # Quartus decides whether the SOF is valid when programming.
            job["kind"] = job.get("kind") or "upload"
            job["verilog_local_path"] = str(v_path)
            job["sof_local_path"] = str(s_path)
            job["filename"] = v_path.name
            job["status"] = "queued"
            job["message"] = "queued; recovered Pi-local upload files; Quartus will decide SOF validity"
            job["upload_finished_at"] = job.get("upload_finished_at") or now_iso()
            job["upload_finished_ts"] = float(job.get("upload_finished_ts", 0) or now_ts)
            job["sof_source"] = "raspberry_pi_local_upload_passthrough"
            job.pop("receive_deadline_ts", None)
            if str(job.get("planned_instance_id") or "") == "uploading":
                job.pop("planned_instance_id", None)
            test_seconds = int(job.get("test_seconds", 0) or 0)
            requested_board = job.get("requested_board") or ""
            job["estimated_seconds"] = estimate_deploy_seconds(requested_board, test_seconds)
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            changed = True
            add_history("upload_watchdog_promoted_to_queued", job.get("requested_board") or "", {"job_id": job_id, "verilog": v_path.name, "sof": s_path.name})
            continue

        if deadline and now_ts > deadline:
            # v4.05: an upload timeout is a system/network wait condition, not
            # a student programming failure. When users submit several SOF files
            # back-to-back, the Quartus-server archive copy can be queued behind
            # another copy. Keep the job visible and extend the receive window.
            extension = strict_upload_timeout_seconds()
            new_deadline = now_ts + extension
            job["status"] = status_l if status_l in ("receiving", "uploading") else "receiving"
            job["receive_deadline_ts"] = new_deadline
            job["upload_deadline_at"] = iso_from_ts(new_deadline)
            job["upload_timeout_extensions"] = int(job.get("upload_timeout_extensions", 0) or 0) + 1
            job["remaining_seconds"] = int(extension)
            job["wait_seconds"] = 0
            job["planned_instance_id"] = "uploading"
            job["message"] = f"upload/archive still waiting; timeout extended automatically ({job['upload_timeout_extensions']})"
            jobs[job_id] = job
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            changed = True
            add_history("upload_watchdog_extended_not_failed", job.get("requested_board") or job.get("target_board_hint") or "", {"job_id": job_id, "extensions": job.get("upload_timeout_extensions", 0)})
            continue

        # Keep the display honest while the upload is still active.
        if deadline:
            job["remaining_seconds"] = max(0, int(deadline - now_ts))
        job["wait_seconds"] = 0
        job["planned_instance_id"] = "uploading"
        job["message"] = "receiving upload or archiving to Quartus server; not runnable yet"
        jobs[job_id] = job

    state["jobs"] = jobs
    return changed






def recover_staged_tmp_jobs(state: Dict[str, Any]) -> bool:
    """Recover any job that has .v.tmp/.sof.tmp staging files but is not queued.

    This is the safety net for GUI disconnects, stream reconnects, or a controller
    restart after the HTTP upload succeeded.  If the stage cache exists, the job
    is preserved and the FIFO worker can start it when a slot opens.
    """
    changed = False
    jobs = state.setdefault("jobs", {})
    queue = state.setdefault("queue", [])
    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status_l = str(job.get("status") or "").lower()
        if status_l not in ("receiving", "uploading", "queued"):
            continue
        stage_ok, stage_reason = staged_job_files_ready(job)
        if stage_ok and job.get("temporary_stage_cache"):
            if status_l != "queued":
                job["status"] = "queued"
                job["message"] = "queued; recovered staged .v.tmp/.sof.tmp package"
                job["upload_stage"] = job.get("upload_stage") or "queued_tmp_staged_waiting_for_slot"
                job["upload_files_in_progress"] = False
                job.pop("receive_deadline_ts", None)
                if str(job.get("planned_instance_id") or "") == "uploading":
                    job.pop("planned_instance_id", None)
                changed = True
            if job_id not in queue:
                queue.append(job_id)
                changed = True
            jobs[job_id] = job
        elif job.get("temporary_stage_cache") and status_l in ("queued", "uploading"):
            job["last_stage_wait_reason"] = stage_reason
            jobs[job_id] = job
    return changed

def recover_orphan_uploaded_jobs(state: Dict[str, Any]) -> bool:
    """Recover a recently uploaded job directory if a stale background save hid it.

    If the GUI successfully uploaded files but a race/daily rollover removed the job
    from board_state.json, the files still exist under uploads/<job_id>. This function
    recreates a safe queued job for recent orphan folders.
    """
    cfg = load_config()
    seconds = int(((cfg.get("strict_resource_engine", {}) or {}).get("orphan_upload_recovery_seconds", 900)) or 900)
    if seconds <= 0:
        return False
    uploads_dir = BASE_DIR / cfg.get("uploads_dir", "uploads")
    if not uploads_dir.exists():
        return False
    now_ts = time.time()
    jobs = state.setdefault("jobs", {})
    queue = state.setdefault("queue", [])
    changed = False
    for folder in uploads_dir.iterdir():
        try:
            if not folder.is_dir():
                continue
            job_id = folder.name.strip()
            if job_id in jobs:
                continue
            if not re.fullmatch(r"[0-9a-fA-F]{8,16}", job_id):
                continue
            age = now_ts - folder.stat().st_mtime
            if age < 0 or age > seconds:
                continue
            v_candidates = sorted(list(folder.glob("*.v")) + list(folder.glob("*.sv")))
            s_candidates = sorted(folder.glob("*.sof"))
            if not v_candidates or not s_candidates:
                continue
            v_path = v_candidates[0]
            s_path = s_candidates[0]
            local_sof_verify = validate_local_sof_file(s_path, compute_hash=True)
            if not local_sof_verify.get("success"):
                continue
            test_minutes = default_test_minutes()
            test_seconds = int(test_minutes * 60)
            created_ts = folder.stat().st_mtime
            job = {
                "job_id": job_id,
                "status": "queued",
                "kind": "upload",
                "verilog_local_path": str(v_path),
                "sof_local_path": str(s_path),
                "filename": v_path.name,
                "requested_board": "",
                "client_hostname": "recovered_upload",
                "student_ip": "recovered_upload",
                "student": "recovered_upload",
                "created_at": iso_from_ts(created_ts),
                "created_ts": created_ts,
                "message": "queued; recovered from uploaded files after queue-state race",
                "estimated_seconds": estimate_deploy_seconds("", test_seconds),
                "test_minutes": test_minutes,
                "test_seconds": test_seconds,
                "priority": priority_value_from_role("Student"),
                "priority_label": "Student",
                "priority_role": "Student",
                "cancel_token": uuid.uuid4().hex,
                "upload_files_attached": True,
                "sof_verification": local_sof_verify,
                "sof_source": "raspberry_pi_orphan_upload_verified",
                "recovered_orphan_upload": True,
            }
            jobs[job_id] = job
            if job_id not in queue:
                queue.append(job_id)
            changed = True
        except Exception:
            continue
    if changed:
        state["jobs"] = jobs
        state["queue"] = queue
        history = state.setdefault("history", [])
        history.append({"time": now_iso(), "event": "orphan_upload_recovery", "board": "", "details": {"mode": "recovered_recent_upload_dirs"}})
        if len(history) > 200:
            del history[:-200]
    return changed

def queue_job_runner(job_id: str, job_snapshot: Dict[str, Any]) -> None:
    """Run one queued programming job without blocking the FIFO scheduler."""
    try:
        result = process_queued_job(dict(job_snapshot))

        state_check = load_state()
        current_check = state_check.setdefault("jobs", {}).get(job_id, {})
        if isinstance(current_check, dict) and not runner_generation_matches(current_check, job_snapshot):
            current_check.setdefault("late_runner_results", []).append(result if isinstance(result, dict) else {"result": str(result)})
            state_check["jobs"][job_id] = current_check
            save_state_preserving_concurrent_jobs(state_check)
            return

        if should_requeue_for_system_transient(result):
            state = load_state()
            job = state.setdefault("jobs", {}).get(job_id, dict(job_snapshot))
            if str(job.get("status") or "").lower() == "running" and runner_generation_matches(job, job_snapshot):
                board = job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or ""
                cable = job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or ""
                if board and cable:
                    clear_board_slot_state(state, board, cable, reason="transient_system_requeue", job_id=job_id)
                _strict_clear_active_fields(job)
                job["status"] = "queued"
                job["system_retry_count"] = int(job.get("system_retry_count", 0) or 0) + 1
                job["last_system_recovery_at"] = now_iso()
                job["last_system_recovery_reason"] = result.get("error") or result.get("fallback_reason") or "transient system error"
                job["message"] = f"system recovered: transient programming/copy problem; automatically requeued attempt {job['system_retry_count']}"
                state["jobs"][job_id] = job
                if job_id not in state.setdefault("queue", []):
                    state["queue"].append(job_id)
                state = annotate_queue_assignments(state)
                active_running = [jid for jid, j in state.get("jobs", {}).items() if isinstance(j, dict) and j.get("status") == "running"]
                state["current_jobs"] = active_running
                state["current_job"] = active_running[0] if active_running else None
                save_state_preserving_concurrent_jobs(state)
                add_history("queue_transient_system_requeue", job.get("requested_board") or job.get("planned_board") or "", {"job_id": job_id, "reason": job.get("last_system_recovery_reason", "")})
            return

        if should_requeue_for_busy_resources(result):
            state = load_state()
            job = state.setdefault("jobs", {}).get(job_id, dict(job_snapshot))
            if str(job.get("status") or "").lower() == "running" and runner_generation_matches(job, job_snapshot):
                job["status"] = "queued"
                job["message"] = "waiting for the pre-assigned JTAG board/test timer to finish"
                job["last_wait_reason"] = result.get("error") or result.get("fallback_reason") or "matching JTAG busy"
                job["last_wait_at"] = now_iso()
                job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                state["jobs"][job_id] = job
                if job_id not in state.setdefault("queue", []):
                    state["queue"].append(job_id)
                state = annotate_queue_assignments(state)
                active_running = [jid for jid, j in state.get("jobs", {}).items() if isinstance(j, dict) and j.get("status") == "running"]
                state["current_jobs"] = active_running
                state["current_job"] = active_running[0] if active_running else None
                save_state_preserving_concurrent_jobs(state)
                add_history("queue_wait_for_jtag", job.get("requested_board") or job.get("planned_board") or "", {"job_id": job_id, "reason": job.get("last_wait_reason", "")})
            return

        state = load_state()
        current = state.setdefault("jobs", {}).get(job_id, dict(job_snapshot))
        current_status = str(current.get("status") or "").lower()
        if current_status != "running" or not runner_generation_matches(current, job_snapshot):
            if isinstance(result, dict):
                current.setdefault("late_runner_results", []).append(result)
                state["jobs"][job_id] = current
                save_state_preserving_concurrent_jobs(state)
            return

        _finalize_job(state, job_id, "completed" if result.get("success") else "failed", result)
        state = annotate_queue_assignments(state)
        active_running = [jid for jid, j in state.get("jobs", {}).items() if isinstance(j, dict) and j.get("status") == "running"]
        state["current_jobs"] = active_running
        state["current_job"] = active_running[0] if active_running else None
        save_state_preserving_concurrent_jobs(state)
        # v4.54: refresh the shared SSE payload immediately after a job enters
        # testing/failed/completed so the GUI does not wait for the next slow tick.
        try:
            update_queue_stream_broadcast_once("job_finalized_immediate_notify")
        except Exception:
            pass
        add_history("queue_finish", result.get("selected_board") or result.get("board") or current.get("planned_board") or "", {"job_id": job_id, "success": result.get("success", False)})
    except Exception as e:
        try:
            state = load_state()
            job = state.setdefault("jobs", {}).get(job_id, dict(job_snapshot))
            if str(job.get("status") or "").lower() == "running" and runner_generation_matches(job, job_snapshot):
                board = job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or ""
                cable = job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or ""
                if board and cable:
                    clear_board_slot_state(state, board, cable, reason="runner_exception_requeue", job_id=job_id)
                _strict_clear_active_fields(job)
                job["status"] = "queued"
                job["system_retry_count"] = int(job.get("system_retry_count", 0) or 0) + 1
                job["last_system_recovery_at"] = now_iso()
                job["last_system_recovery_reason"] = str(e)
                job["message"] = f"system recovered: runner exception; automatically requeued attempt {job['system_retry_count']}"
                state.setdefault("jobs", {})[job_id] = job
                if job_id not in state.setdefault("queue", []):
                    state["queue"].append(job_id)
                state = annotate_queue_assignments(state)
                save_state_preserving_concurrent_jobs(state)
        except Exception:
            pass
    finally:
        with QUEUE_JOB_THREADS_LOCK:
            QUEUE_JOB_THREADS.pop(str(job_id), None)


def wake_queue_worker(reason: str = "") -> None:
    """Wake the FIFO scheduler when a queue mutation may have made work runnable.

    The queue maintenance supervisor also waits on this event.  That keeps the idle
    controller quiet, but lets cancellations/uploads/requeues trigger repair
    immediately instead of waiting for the next fixed polling tick.
    """
    try:
        QUEUE_WORKER_WAKE_EVENT.set()
    except Exception:
        pass
    try:
        AUTO_REPAIR_WAKE_EVENT.set()
    except Exception:
        pass


def auto_repair_sleep_seconds() -> float:
    """Configured idle delay for the queue maintenance supervisor.

    automatic_repair.interval_seconds is the readable primary setting.  The old
    instant_programming.auto_repair_sleep_seconds key is still accepted for
    backward compatibility with existing config files.
    """
    cfg = load_config()
    repair_cfg = cfg.get("automatic_repair", {}) or {}
    legacy_cfg = cfg.get("instant_programming", {}) or {}
    value = repair_cfg.get("interval_seconds", legacy_cfg.get("auto_repair_sleep_seconds", 1.0))
    try:
        return max(0.1, float(value or 1.0))
    except Exception:
        return 1.0


def auto_repair_wait() -> None:
    """Wait without busy polling; wake early when queue state mutates."""
    try:
        AUTO_REPAIR_WAKE_EVENT.wait(auto_repair_sleep_seconds())
    finally:
        AUTO_REPAIR_WAKE_EVENT.clear()


def queue_state_has_active_work(state: Dict[str, Any]) -> bool:
    """Return True only when the scheduler has visible active/queued work to service."""
    jobs = state.get("jobs", {}) or {}
    active_statuses = {"receiving", "uploading", "queued", "running", "testing"}
    for job in jobs.values():
        if isinstance(job, dict) and str(job.get("status") or "").lower() in active_statuses:
            return True
    return False


def queue_worker_wait(active: bool) -> None:
    cfg = load_config().get("instant_programming", {}) or {}
    active_sleep = float(cfg.get("queue_worker_sleep_seconds", 0.05) or 0.05)
    idle_sleep = float(cfg.get("queue_worker_idle_sleep_seconds", 2.0) or 2.0)
    timeout = active_sleep if active else max(active_sleep, idle_sleep)
    try:
        QUEUE_WORKER_WAKE_EVENT.wait(timeout)
    finally:
        QUEUE_WORKER_WAKE_EVENT.clear()


def start_queue_job_thread(job_id: str, job: Dict[str, Any]) -> bool:
    """Start a job runner once. Returns False if one is already active."""
    jid = str(job_id)
    with QUEUE_JOB_THREADS_LOCK:
        t = QUEUE_JOB_THREADS.get(jid)
        if t and t.is_alive():
            return False
        t = threading.Thread(target=queue_job_runner, args=(jid, dict(job)), daemon=True, name=f"queue_job_{jid}")
        QUEUE_JOB_THREADS[jid] = t
        t.start()
        return True

def queue_worker_loop() -> None:
    global QUEUE_WORKER_HEARTBEAT_TS
    while True:
        try:
            QUEUE_WORKER_HEARTBEAT_TS = time.time()
            maybe_cleanup_orphan_state_tmp_files("queue_worker_cycle")
            cleanup_daily_state_rollover()
            cleanup_expired_locks()
            state = load_state()

            changed = False
            if recover_staged_tmp_jobs(state):
                changed = True
            if recover_stuck_upload_jobs(state):
                changed = True
            if recover_orphan_uploaded_jobs(state):
                changed = True
            if recover_stuck_running_jobs(state):
                changed = True
            if requeue_jobs_assigned_to_disabled_slots(state, reason="queue_worker_disabled_slot"):
                changed = True
            if repair_active_slot_conflicts(state):
                changed = True
            if strict_resource_reconcile(state, reason="queue_worker_cycle"):
                changed = True
            if changed:
                state = annotate_queue_assignments(state)
                save_state_preserving_concurrent_jobs(state)

            state = load_state()
            if strict_resource_reconcile(state, reason="queue_worker_pre_plan"):
                save_state_preserving_concurrent_jobs(state)
                state = load_state()
            if not queue_state_has_active_work(state):
                queue_worker_wait(active=False)
                continue
            state = annotate_queue_assignments(state)
            for jid in sorted_queued_job_ids(state):
                if priority_label_from_value(state.get("jobs", {}).get(jid, {}).get("priority_label") or state.get("jobs", {}).get(jid, {}).get("priority")) == "Teacher":
                    state = apply_teacher_override_for_job(state, jid)
                    state = annotate_queue_assignments(state)
                    break
            save_state_preserving_concurrent_jobs(state)

            starts_this_cycle = 0
            cfg_runtime = load_config()
            max_parallel_jobs = int(cfg_runtime.get("max_parallel_programming_jobs", 1) or 1)
            if bool((cfg_runtime.get("strict_resource_engine", {}) or {}).get("serial_safe_programming", True)):
                max_parallel_jobs = 1
            max_starts_per_cycle = max(1, min(max_parallel_jobs, int(cfg_runtime.get("max_starts_per_cycle", max_parallel_jobs) or max_parallel_jobs)))
            while starts_this_cycle < max_starts_per_cycle and _running_thread_count() < max_parallel_jobs:
                state = load_state()
                next_id = first_runnable_queued_job_id(state)
                if not next_id:
                    break

                job = state.setdefault("jobs", {}).get(next_id, {})
                upload_ready, upload_ready_reason = upload_job_ready_to_program(job)
                if not upload_ready:
                    # This should be rare, but prevents a queued upload placeholder from
                    # being started as a programming job before the real .v/.sof are ready.
                    job["status"] = "uploading"
                    job["message"] = upload_ready_reason
                    job["planned_instance_id"] = "uploading"
                    job["wait_seconds"] = 0
                    job["remaining_seconds"] = max(0, int(float(job.get("receive_deadline_ts", time.time()) or time.time()) - time.time()))
                    state["jobs"][next_id] = job
                    save_state_preserving_concurrent_jobs(state)
                    break

                job, assigned_now, assign_reason = assign_immediate_free_slot_for_job(job, state)
                if not assigned_now:
                    job["status"] = "queued"
                    job["message"] = f"waiting: {assign_reason}"
                    job["last_wait_reason"] = assign_reason
                    job["last_wait_at"] = now_iso()
                    job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                    state["jobs"][next_id] = job
                    state = annotate_queue_assignments(state)
                    save_state_preserving_concurrent_jobs(state)
                    break

                ok_slot, wait_reason = hard_slot_available_for_job(job, state)
                if not ok_slot:
                    job["status"] = "queued"
                    job["message"] = f"waiting: {wait_reason}"
                    job["last_wait_reason"] = wait_reason
                    job["last_wait_at"] = now_iso()
                    job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                    state["jobs"][next_id] = job
                    state = annotate_queue_assignments(state)
                    save_state_preserving_concurrent_jobs(state)
                    break

                if job.get("temporary_stage_cache") and not (is_valid_verilog_file_path(job.get("verilog_local_path")) and is_valid_sof_file_path(job.get("sof_local_path"))):
                    job, stage_ok, stage_reason = activate_staged_job_files(next_id, job)
                    if not stage_ok:
                        job["status"] = "queued"
                        job["message"] = f"waiting: staged files not ready to activate ({stage_reason})"
                        job["last_wait_reason"] = stage_reason
                        job["last_wait_at"] = now_iso()
                        job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                        state["jobs"][next_id] = job
                        state = annotate_queue_assignments(state)
                        save_state_preserving_concurrent_jobs(state)
                        break

                board_for_timer = job.get("planned_board") or queue_target_board_hint(job, load_config()) or job.get("requested_board") or ""
                job = freeze_job_programming_target(job)
                target_check = validate_locked_programming_target(job.get("locked_target_board") or board_for_timer, job.get("locked_target_jtag_cable") or job.get("planned_jtag_cable") or "", job_id=next_id)
                if not target_check.get("success"):
                    # Do not start quartus_pgm against a mismatched or disabled cable.
                    # Clear stale slot fields and let the planner choose another valid slot.
                    _strict_clear_active_fields(job)
                    job["status"] = "queued"
                    job["message"] = target_check.get("error", "programming target validation failed; replanning")
                    job["last_wait_reason"] = job["message"]
                    job["target_validation_failed"] = True
                    state["jobs"][next_id] = job
                    state = annotate_queue_assignments(state)
                    save_state_preserving_concurrent_jobs(state)
                    break
                job["locked_target_validation"] = target_check
                program_seconds = int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(board_for_timer))
                job.pop("dispatch_not_before_ts", None)
                job["status"] = "running"
                job["started_at"] = now_iso()
                job["started_ts"] = time.time()
                job["run_generation"] = int(job.get("run_generation", 0) or 0) + 1
                job["program_estimated_seconds"] = program_seconds
                job["running_phase"] = "programming"
                # Running timer is programming-only. Test time starts after quartus_pgm succeeds.
                job["estimated_seconds"] = program_seconds
                job["remaining_seconds"] = program_seconds
                planned = job.get("queue_position_message") or "planned slot"
                job["message"] = f"programming after parallel FIFO assignment: {planned}"
                state["jobs"][next_id] = job

                running_ids = [jid for jid, j in state.get("jobs", {}).items() if isinstance(j, dict) and j.get("status") == "running"]
                state["current_jobs"] = running_ids
                state["current_job"] = running_ids[0] if running_ids else None
                save_state_preserving_concurrent_jobs(state)

                if start_queue_job_thread(next_id, job):
                    starts_this_cycle += 1
                    add_history("queue_start_async", job.get("requested_board") or job.get("planned_board") or job.get("board") or "", {"job_id": next_id, "kind": job.get("kind"), "planned_instance": job.get("planned_instance_id", ""), "planned_cable": job.get("planned_jtag_cable", "")})
                else:
                    break

            queue_worker_wait(active=True)
        except Exception as e:
            try:
                add_history("queue_worker_error", "", {"error": str(e)})
            except Exception:
                pass
            time.sleep(1.0)



def dispatch_ready_queued_jobs_once(reason: str = "internal_dispatch_once") -> Dict[str, Any]:
    """Start ready queued jobs from normal controller flow.

    This is an internal safety path, not a manual repair command.  It uses the
    same assignment, stage activation, target validation, and queue_job_runner
    path as the background queue worker.  The purpose is to prevent a valid job
    from sitting forever in `queued` with Wait=00:00 when the queue worker is
    alive but misses the wake event or a stale planning cycle.
    """
    started: List[str] = []
    skipped: List[Dict[str, Any]] = []
    with QUEUE_DISPATCH_ONCE_LOCK:
        try:
            cfg_runtime = load_config()
            max_parallel_jobs = int(cfg_runtime.get("max_parallel_programming_jobs", 1) or 1)
            if bool((cfg_runtime.get("strict_resource_engine", {}) or {}).get("serial_safe_programming", True)):
                max_parallel_jobs = 1
            max_starts = max(1, min(max_parallel_jobs, int(cfg_runtime.get("max_starts_per_cycle", max_parallel_jobs) or max_parallel_jobs)))

            while len(started) < max_starts and _running_thread_count() < max_parallel_jobs:
                def promote_one(state: Dict[str, Any]):
                    if strict_resource_reconcile(state, reason=reason + "_pre_reconcile"):
                        pass
                    state = annotate_queue_assignments(state)
                    next_id = first_runnable_queued_job_id(state)
                    if not next_id:
                        return ok(started=False, reason="no_runnable_queued_job")
                    job = dict(state.setdefault("jobs", {}).get(next_id, {}) or {})
                    if not job or str(job.get("status") or "").lower() != "queued":
                        return ok(started=False, reason="selected_job_not_queued", job_id=next_id, status=job.get("status"))

                    upload_ready, upload_ready_reason = upload_job_ready_to_program(job)
                    if not upload_ready:
                        # Keep it visible and cancellable, but do not lie that it is runnable.
                        job["status"] = "uploading"
                        job["message"] = upload_ready_reason
                        job["planned_instance_id"] = "uploading"
                        job["wait_seconds"] = 0
                        job["remaining_seconds"] = max(0, int(float(job.get("receive_deadline_ts", time.time()) or time.time()) - time.time()))
                        job["last_dispatch_block_reason"] = upload_ready_reason
                        state.setdefault("jobs", {})[next_id] = job
                        return ok(started=False, reason=upload_ready_reason, job_id=next_id)

                    job, assigned_now, assign_reason = assign_immediate_free_slot_for_job(job, state)
                    if not assigned_now:
                        job["status"] = "queued"
                        job["message"] = f"waiting: {assign_reason}"
                        job["last_wait_reason"] = assign_reason
                        job["last_wait_at"] = now_iso()
                        job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                        state.setdefault("jobs", {})[next_id] = job
                        state = annotate_queue_assignments(state)
                        return ok(started=False, reason=assign_reason, job_id=next_id)

                    ok_slot, wait_reason = hard_slot_available_for_job(job, state)
                    if not ok_slot:
                        job["status"] = "queued"
                        job["message"] = f"waiting: {wait_reason}"
                        job["last_wait_reason"] = wait_reason
                        job["last_wait_at"] = now_iso()
                        job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                        state.setdefault("jobs", {})[next_id] = job
                        state = annotate_queue_assignments(state)
                        return ok(started=False, reason=wait_reason, job_id=next_id)

                    if job.get("temporary_stage_cache") and not (is_valid_verilog_file_path(job.get("verilog_local_path")) and is_valid_sof_file_path(job.get("sof_local_path"))):
                        job, stage_ok, stage_reason = activate_staged_job_files(next_id, job)
                        if not stage_ok:
                            job["status"] = "queued"
                            job["message"] = f"waiting: staged files not ready to activate ({stage_reason})"
                            job["last_wait_reason"] = stage_reason
                            job["last_wait_at"] = now_iso()
                            job["wait_count"] = int(job.get("wait_count", 0) or 0) + 1
                            job["last_stage_activation_failed_at"] = now_iso()
                            state.setdefault("jobs", {})[next_id] = job
                            state = annotate_queue_assignments(state)
                            return ok(started=False, reason=stage_reason, job_id=next_id)

                    board_for_timer = job.get("planned_board") or queue_target_board_hint(job, load_config()) or job.get("requested_board") or ""
                    job = freeze_job_programming_target(job)
                    target_check = validate_locked_programming_target(job.get("locked_target_board") or board_for_timer, job.get("locked_target_jtag_cable") or job.get("planned_jtag_cable") or "", job_id=next_id)
                    if not target_check.get("success"):
                        _strict_clear_active_fields(job)
                        job["status"] = "queued"
                        job["message"] = target_check.get("error", "programming target validation failed; replanning")
                        job["last_wait_reason"] = job["message"]
                        job["target_validation_failed"] = True
                        state.setdefault("jobs", {})[next_id] = job
                        state = annotate_queue_assignments(state)
                        return ok(started=False, reason=job["message"], job_id=next_id)

                    program_seconds = int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(board_for_timer))
                    job.pop("dispatch_not_before_ts", None)
                    job["status"] = "running"
                    job["started_at"] = now_iso()
                    job["started_ts"] = time.time()
                    job["run_generation"] = int(job.get("run_generation", 0) or 0) + 1
                    job["program_estimated_seconds"] = program_seconds
                    job["running_phase"] = "programming"
                    job["estimated_seconds"] = program_seconds
                    job["remaining_seconds"] = program_seconds
                    job["message"] = f"programming after automatic FIFO dispatch: {job.get('queue_position_message') or 'planned slot'}"
                    job["dispatch_reason"] = reason
                    state.setdefault("jobs", {})[next_id] = job

                    running_ids = [jid for jid, j in state.get("jobs", {}).items() if isinstance(j, dict) and j.get("status") == "running"]
                    state["current_jobs"] = running_ids
                    state["current_job"] = running_ids[0] if running_ids else None
                    return ok(started=True, job_id=next_id, job_snapshot=dict(job), job=public_queue_job(job))

                result = update_state_atomic(promote_one)
                if not isinstance(result, dict) or not result.get("success"):
                    skipped.append({"reason": "dispatch_state_update_failed", "result": result})
                    break
                if not result.get("started"):
                    skipped.append({"reason": result.get("reason", "not_started"), "job_id": result.get("job_id", "")})
                    break

                jid = str(result.get("job_id") or "")
                snapshot = dict(result.get("job_snapshot") or {})
                if jid and start_queue_job_thread(jid, snapshot):
                    started.append(jid)
                    try:
                        add_history("queue_start_internal_dispatch", snapshot.get("requested_board") or snapshot.get("planned_board") or snapshot.get("board") or "", {"job_id": jid, "reason": reason, "planned_instance": snapshot.get("planned_instance_id", ""), "planned_cable": snapshot.get("planned_jtag_cable", "")})
                    except Exception:
                        pass
                else:
                    skipped.append({"reason": "runner_already_active_or_missing_job_id", "job_id": jid})
                    break

            return ok(started_count=len(started), started_job_ids=started, skipped=skipped, reason=reason)
        except Exception as e:
            try:
                add_history("internal_dispatch_once_error", "", {"reason": reason, "error": str(e)})
            except Exception:
                pass
            return fail("internal dispatch failed", reason=reason, error=str(e), exception_type=type(e).__name__)

def automatic_queue_repair_once(reason: str = "automatic_repair", force_plan: bool = True) -> Dict[str, Any]:
    """Run automatic queue maintenance from normal controller events.

    This is internal controller logic used by startup, queue snapshots,
    uploads, cancellations, and JTAG enable/disable actions. Users do not
    run a separate helper script.
    """
    try:
        cleanup_daily_state_rollover()
        cleanup_expired_locks()
    except Exception:
        pass

    state = load_state()
    changed = False
    try:
        if recover_staged_tmp_jobs(state):
            changed = True
        if recover_stuck_upload_jobs(state):
            changed = True
        if recover_orphan_uploaded_jobs(state):
            changed = True
        if recover_stuck_running_jobs(state):
            changed = True
        if requeue_jobs_assigned_to_disabled_slots(state, reason=reason + "_disabled_slot"):
            changed = True
        if repair_active_slot_conflicts(state):
            changed = True
        if strict_resource_reconcile(state, reason=reason):
            changed = True
        if force_plan:
            before = json.dumps({
                "queue": state.get("queue", []),
                "jobs": {jid: {"status": j.get("status"), "slot": j.get("planned_instance_id"), "jtag": j.get("planned_jtag_cable")} for jid, j in (state.get("jobs", {}) or {}).items() if isinstance(j, dict)}
            }, sort_keys=True, ensure_ascii=False)
            state = annotate_queue_assignments(state)
            after = json.dumps({
                "queue": state.get("queue", []),
                "jobs": {jid: {"status": j.get("status"), "slot": j.get("planned_instance_id"), "jtag": j.get("planned_jtag_cable")} for jid, j in (state.get("jobs", {}) or {}).items() if isinstance(j, dict)}
            }, sort_keys=True, ensure_ascii=False)
            if before != after:
                changed = True
        if changed:
            state["last_automatic_repair"] = {
                "at": now_iso(),
                "ts": time.time(),
                "reason": reason,
                "force_plan": bool(force_plan),
            }
            save_state_preserving_concurrent_jobs(state)
        dispatch_result = {"success": True, "started_count": 0, "started_job_ids": []}
        try:
            if pending_queued_job_count() > 0 and _running_thread_count() == 0:
                dispatch_result = dispatch_ready_queued_jobs_once(reason + "_dispatch")
        except Exception as dispatch_error:
            dispatch_result = {"success": False, "error": str(dispatch_error)}
        return {"success": True, "changed": bool(changed), "reason": reason, "dispatch": dispatch_result}
    except Exception as e:
        try:
            add_history("automatic_queue_repair_error", "", {"reason": reason, "error": str(e)})
        except Exception:
            pass
        return {"success": False, "changed": False, "reason": reason, "error": str(e)}


def auto_repair_worker_loop() -> None:
    global AUTO_REPAIR_HEARTBEAT_TS
    while True:
        try:
            AUTO_REPAIR_HEARTBEAT_TS = time.time()
            ensure_queue_worker(force_restart_if_stalled=True)
            automatic_queue_repair_once("auto_repair_background", force_plan=True)
            auto_repair_wait()
        except Exception:
            time.sleep(1.0)


def ensure_auto_repair_worker() -> None:
    global AUTO_REPAIR_WORKER_STARTED, AUTO_REPAIR_WORKER_THREAD
    if AUTO_REPAIR_WORKER_THREAD and AUTO_REPAIR_WORKER_THREAD.is_alive():
        AUTO_REPAIR_WORKER_STARTED = True
        return
    t = threading.Thread(target=auto_repair_worker_loop, daemon=True, name="auto_queue_repair_supervisor")
    AUTO_REPAIR_WORKER_THREAD = t
    AUTO_REPAIR_WORKER_STARTED = True
    t.start()

def pending_queued_job_count() -> int:
    try:
        return len(sorted_queued_job_ids(load_state()))
    except Exception:
        return 0


def ensure_queue_worker(force_restart_if_stalled: bool = False) -> None:
    global QUEUE_WORKER_STARTED, QUEUE_WORKER_THREAD
    if QUEUE_WORKER_THREAD and QUEUE_WORKER_THREAD.is_alive():
        QUEUE_WORKER_STARTED = True
        if force_restart_if_stalled:
            try:
                age = time.time() - float(QUEUE_WORKER_HEARTBEAT_TS or 0)
                stale_after = int((load_config().get("strict_resource_engine", {}) or {}).get("queue_worker_stale_restart_seconds", 8) or 8)
                if pending_queued_job_count() > 0 and _running_thread_count() == 0 and QUEUE_WORKER_HEARTBEAT_TS and age > stale_after:
                    t = threading.Thread(target=queue_worker_loop, daemon=True, name="queue_scheduler_recovery")
                    QUEUE_WORKER_THREAD = t
                    t.start()
            except Exception:
                pass
        return
    t = threading.Thread(target=queue_worker_loop, daemon=True, name="queue_scheduler")
    QUEUE_WORKER_THREAD = t
    t.start()
    QUEUE_WORKER_STARTED = True


def enqueue_job(job: Dict[str, Any]) -> Dict[str, Any]:
    role = job.get("priority_label") or job.get("priority_role") or job.get("priority", "Student")
    job["priority"] = priority_value_from_role(role)
    job["priority_label"] = priority_label_from_value(role)
    job["priority_role"] = job["priority_label"]
    job.setdefault("student", job.get("client_hostname") or "unknown")
    job.setdefault("cancel_token", uuid.uuid4().hex)
    attach_submission_signature(job, job)

    def mutate(state: Dict[str, Any]):
        cleanup_daily_state_rollover_in_state(state)
        duplicate = find_active_duplicate_submission(state, job)
        if duplicate:
            return duplicate_submission_response(duplicate[0], duplicate[1])
        state.setdefault("jobs", {})[job["job_id"]] = job
        if job["job_id"] not in state.setdefault("queue", []):
            state["queue"].append(job["job_id"])
        state = annotate_queue_assignments(state)
        state = apply_teacher_override_for_job(state, job["job_id"])
        state = annotate_queue_assignments(state)
        updated_job = state.setdefault("jobs", {}).get(job["job_id"], job)

        history = state.setdefault("history", [])
        history.append({"time": now_iso(), "event": "queue_add", "board": updated_job.get("requested_board") or updated_job.get("planned_board") or "", "details": {"job_id": job["job_id"], "kind": job.get("kind"), "source_mode": job.get("source_mode") or job.get("submit_mode") or "", "priority": job.get("priority"), "student": job.get("student"), "major": job.get("major", ""), "planned_instance": updated_job.get("planned_instance_id", ""), "wait_seconds": updated_job.get("wait_seconds", 0)}})
        if len(history) > 200:
            del history[:-200]

        return ok(job_id=updated_job["job_id"], cancel_token=updated_job["cancel_token"], status="queued", queue_length=len(sorted_queued_job_ids(state)), job=public_queue_job(updated_job), queue_plan=state.get("queue_plan", {}))

    result = update_state_atomic(mutate)
    try:
        if isinstance(result, dict) and result.get("success"):
            hist_cfg = (load_config().get("server_history", {}) or {})
            if bool(hist_cfg.get("record_on_queue_accept", False)):
                latest = load_state().get("jobs", {}).get(job.get("job_id", ""), {})
                if latest:
                    write_job_history_to_server_async(job.get("job_id", ""), latest, "job_queued")
            ensure_queue_worker(force_restart_if_stalled=True)
            wake_queue_worker("enqueue_job")
            automatic_queue_repair_once("enqueue_job_immediate_dispatch", force_plan=True)
    except Exception:
        pass
    return result


# =========================
# GPIO / HAT helpers
# =========================
def gpio_enabled() -> bool:
    cfg = load_config()
    return bool(cfg.get("use_gpio", False)) and OutputDevice is not None


def get_out(pin: Optional[int], active_high: bool = True):
    if pin is None:
        return None
    if not gpio_enabled():
        return None
    pin = int(pin)
    if pin not in GPIO_OUTPUTS:
        GPIO_OUTPUTS[pin] = OutputDevice(pin, active_high=active_high, initial_value=False)
    return GPIO_OUTPUTS[pin]


def get_in(pin: Optional[int]):
    if pin is None:
        return None
    if not gpio_enabled() or InputDevice is None:
        return None
    pin = int(pin)
    if pin not in GPIO_INPUTS:
        GPIO_INPUTS[pin] = InputDevice(pin)
    return GPIO_INPUTS[pin]


def set_led(board_cfg: Dict[str, Any], busy: Optional[bool] = None, ready: Optional[bool] = None) -> None:
    active_high = bool(board_cfg.get("active_high", True))
    if busy is not None:
        dev = get_out(board_cfg.get("busy_led_pin"), active_high)
        if dev:
            dev.on() if busy else dev.off()
    if ready is not None:
        dev = get_out(board_cfg.get("ready_led_pin"), active_high)
        if dev:
            dev.on() if ready else dev.off()


def power_set(board: str, on: bool) -> Dict[str, Any]:
    cfg = load_config()
    boards = cfg.get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    b = boards[board]
    dry_run = bool(cfg.get("dry_run", True))
    active_high = bool(b.get("active_high", True))
    if not dry_run:
        dev = get_out(b.get("power_relay_pin"), active_high)
        if dev:
            dev.on() if on else dev.off()
    state = load_state()
    state.setdefault("power", {})[board] = "on" if on else "off"
    save_state(state)
    set_led(b, busy=None, ready=on)
    add_history("power_on" if on else "power_off", board)
    return ok(board=board, power_state="on" if on else "off", dry_run=dry_run)


def reset_board(board: str) -> Dict[str, Any]:
    cfg = load_config()
    boards = cfg.get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    b = boards[board]
    dry_run = bool(cfg.get("dry_run", True))
    active_high = bool(b.get("active_high", True))
    if not dry_run:
        dev = get_out(b.get("reset_pin"), active_high)
        if dev:
            dev.on()
            time.sleep(0.25)
            dev.off()
    add_history("reset", board)
    return ok(board=board, reset=True, dry_run=dry_run)


def physical_status(board: str, board_cfg: Dict[str, Any]) -> str:
    cfg = load_config()
    if cfg.get("dry_run", True) or not cfg.get("use_gpio", False):
        return "ok_simulated"
    dev = get_in(board_cfg.get("status_pin"))
    if dev is None:
        return "unknown"
    try:
        active_high = bool(board_cfg.get("active_high", True))
        value = bool(dev.value)
        present = value if active_high else not value
        return "ok" if present else "not_present"
    except Exception:
        return "error"


# =========================
# SSH / Quartus server
# =========================
def server_info() -> Dict[str, Any]:
    info = dict(load_config().get("quartus_server", {}) or {})
    ssh_key_path = get_quartus_ssh_key_path("").strip()
    if ssh_key_path:
        info["ssh_key"] = ssh_key_path
    return info


def connect_server() -> paramiko.SSHClient:
    info = server_info()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_key = str(info.get("ssh_key") or "").strip()
    if not ssh_key:
        raise RuntimeError(
            "Quartus SSH key path is not configured. On the Raspberry Pi, run: "
            "python3 UADY_PI.py --setup"
        )
    ssh.connect(
        info.get("host"),
        username=info.get("user"),
        key_filename=ssh_key,
        timeout=int(info.get("ssh_timeout_seconds", 20)),
        look_for_keys=False,
        allow_agent=False,
    )
    return ssh


def run_remote(cmd: str, timeout: int = 60) -> Tuple[int, str, str]:
    """Run a remote command with a hard client-side timeout.

    Paramiko exec_command(timeout=...) only sets socket timeouts; stdout.read()
    can still block while a remote Quartus process is stuck. This polling version
    returns code 124 on timeout and closes the SSH channel so queue runner threads
    do not live forever.
    """
    ssh = connect_server()
    chan = None
    out_parts: List[str] = []
    err_parts: List[str] = []
    deadline = time.time() + max(1, int(timeout or 60))
    try:
        transport = ssh.get_transport()
        if transport is None:
            return 255, "", "SSH transport is not available"
        chan = transport.open_session()
        chan.settimeout(1.0)
        chan.exec_command(cmd)

        while True:
            try:
                while chan.recv_ready():
                    out_parts.append(chan.recv(65536).decode("utf-8", errors="ignore"))
                while chan.recv_stderr_ready():
                    err_parts.append(chan.recv_stderr(65536).decode("utf-8", errors="ignore"))
            except Exception:
                pass

            if chan.exit_status_ready():
                code = chan.recv_exit_status()
                try:
                    while chan.recv_ready():
                        out_parts.append(chan.recv(65536).decode("utf-8", errors="ignore"))
                    while chan.recv_stderr_ready():
                        err_parts.append(chan.recv_stderr(65536).decode("utf-8", errors="ignore"))
                except Exception:
                    pass
                return int(code), "".join(out_parts), "".join(err_parts)

            if time.time() > deadline:
                try:
                    chan.close()
                except Exception:
                    pass
                return 124, "".join(out_parts), "".join(err_parts) + f"\n[timeout] remote command exceeded {int(timeout or 60)} seconds"

            time.sleep(0.05)
    finally:
        try:
            if chan is not None:
                chan.close()
        except Exception:
            pass
        ssh.close()



def run_remote_readonly(cmd: str, timeout: int = 60) -> Tuple[int, str, str]:
    """Run remote command and read stdout/stderr without relying on recv_exit_status.
    This matches the original working GUI behavior and avoids some jtagd/quartus_pgm
    cases where status collection hangs even when output is available.
    """
    ssh = connect_server()
    try:
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        return 0, out, err
    finally:
        ssh.close()

def parse_quartus_cables(text: str) -> List[str]:
    """Parse Quartus/JTAG output using the original working app behavior
    plus normal USB-Blaster cable parsing.

    Some server setups print board/hardware names like DE-SoC/DE10/Agilex;
    others print cables like "1) USB-Blaster [USB-0]". Accept both so the
    Raspberry Pi availability layer matches the original GUI detection.
    """
    cables: List[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        low = raw.lower()
        item = ""

        # Original working GUI parser looked for board names.
        if "de-soc" in low or "de1" in low or "agilex" in low or "de-10" in low or "de10" in low:
            try:
                item = raw.split(None, 1)[1].strip()
            except Exception:
                item = raw

        # Standard Quartus list parser.
        elif ")" in raw and ("USB" in raw or "Blaster" in raw):
            item = raw.split(")", 1)[1].strip()

        elif "usb-blaster" in low or "usb-blasterii" in low:
            item = raw

        if item and item not in cables:
            cables.append(item)
    return cables


def board_aliases(board_name: str, board_cfg: Dict[str, Any]) -> List[str]:
    """
    Config-driven JTAG aliases.
    No board names are hardcoded here. To add another board family or rename a
    board, update config_pi_hat.json only.
    """
    aliases = [board_name]
    if board_cfg.get("jtag_cable"):
        aliases.append(str(board_cfg.get("jtag_cable", "")))
    aliases.extend(board_cfg.get("jtag_aliases", []) or [])
    aliases.extend(board_cfg.get("jtag_match", []) or [])

    out = []
    for a in aliases:
        a = str(a).strip()
        if a and a not in out:
            out.append(a)
    return out

def quartus_exe(family: str) -> str:
    info = server_info()
    if family == "pro":
        return info.get("quartus_pro")
    return info.get("quartus_standard")


def project_path(family: str) -> str:
    info = server_info()
    if family == "pro":
        return info.get("pro_project_path")
    return info.get("standard_project_path")


def log_path(family: str) -> str:
    info = server_info()
    if family == "pro":
        return info.get("pro_log_file")
    return info.get("standard_log_file")




def jtag_configuration_problem() -> Dict[str, Any]:
    """Return a non-empty diagnostic dict when Pi-side JTAG setup is incomplete.

    v4.46 security follow-up:
    The shipped config no longer contains lab host/user/path defaults. That is
    intentional, but it means a new Raspberry Pi install must have the private
    Quartus-server settings written once with UADY_PI.py --setup. The older separate helper scripts still exist for advanced/manual repairs.
    """
    try:
        info = server_info()
    except Exception as e:
        return {
            "error": f"Could not read Raspberry Pi private JTAG setup: {e}",
            "missing": ["pi_private_config"],
        }

    missing = []
    if not str(info.get("host") or "").strip():
        missing.append("quartus_server.host")
    if not str(info.get("user") or "").strip():
        missing.append("quartus_server.user")
    if not str(info.get("ssh_key") or "").strip():
        missing.append("quartus_server.ssh_key_path")
    if not str(info.get("quartus_standard") or "").strip() and not str(info.get("quartus_pro") or "").strip():
        missing.append("quartus_server.quartus_standard_or_quartus_pro")
    if missing:
        return {
            "error": "Quartus/JTAG server is not configured in Raspberry Pi private storage.",
            "missing": missing,
            "fix": "On the Raspberry Pi, run python3 UADY_PI.py --setup, then restart the controller. From Windows you can also run python UADY_SETUP.py and choose Full setup.",
            "storage_note": "These values are intentionally not stored in config_pi_hat.json or the GUI folder.",
        }
    return {}


def jtag_not_configured_response(source: str, cache_seconds: int, cache_used: bool = False) -> Dict[str, Any]:
    problem = jtag_configuration_problem()
    info = server_info()
    data = {
        "success": False,
        "configured": False,
        "timestamp": now_iso(),
        "server_host": info.get("host"),
        "cables": [],
        "results": [],
        "errors": [problem] if problem else [],
        "cache_used": cache_used,
        "cache_empty": True,
        "live_poll_seconds": cache_seconds,
        "source": source,
    }
    try:
        adaptive_runtime_config(refresh=True, jtag_data=data, source=source)
    except Exception:
        pass
    return data

def _discover_jtag_live_scan(cache_seconds: int = 5, source: str = "jtag_live_scan") -> Dict[str, Any]:
    """Perform the real SSH quartus_pgm -l scan.

    This is separated from discover_jtag() so /boards and GUI live refresh can
    return cached data instantly while a background thread refreshes the cache.
    """
    families = []
    if server_info().get("quartus_standard"):
        families.append("standard")
    if server_info().get("quartus_pro"):
        families.append("pro")

    if not families:
        return jtag_not_configured_response(source=source, cache_seconds=cache_seconds, cache_used=False)

    results = []
    all_cables = []
    errors = []
    timeout = int((load_config().get("jtag_nonblocking", {}) or {}).get("live_scan_timeout_seconds", 25) or 25)
    for fam in families:
        exe = quartus_exe(fam)
        cmd = f"{exe} -l"
        try:
            code, out, err = run_remote_readonly(cmd, timeout=timeout)
            cables = parse_quartus_cables((out or "") + "\n" + (err or ""))
            for c in cables:
                if c not in all_cables:
                    all_cables.append(c)
            results.append({"family": fam, "command": cmd, "returncode": code, "cables": cables, "stdout": out, "stderr": err})
        except Exception as e:
            errors.append({"family": fam, "command": cmd, "error": str(e)})

    data = {
        "success": len(errors) == 0 or len(all_cables) > 0,
        "timestamp": now_iso(),
        "server_host": server_info().get("host"),
        "cables": all_cables,
        "results": results,
        "errors": errors,
        "cache_used": False,
        "live_poll_seconds": cache_seconds,
        "source": source,
    }
    JTAG_CACHE["time"] = time.time()
    JTAG_CACHE["data"] = data
    try:
        adaptive_runtime_config(refresh=True, jtag_data=data, source=source)
    except Exception:
        pass
    return data


def _start_jtag_async_refresh(reason: str = "background") -> bool:
    """Start a single background JTAG refresh without blocking the HTTP caller."""
    global JTAG_ASYNC_REFRESH_THREAD, JTAG_ASYNC_REFRESH_LAST_TS
    try:
        cfg = (load_config().get("jtag_nonblocking", {}) or {})
        if not bool(cfg.get("async_refresh_enabled", True)):
            return False
        min_gap = float(cfg.get("async_refresh_min_gap_seconds", 8) or 8)
        now_ts = time.time()
        with JTAG_ASYNC_REFRESH_LOCK:
            if JTAG_ASYNC_REFRESH_THREAD and JTAG_ASYNC_REFRESH_THREAD.is_alive():
                return False
            if now_ts - float(JTAG_ASYNC_REFRESH_LAST_TS or 0) < min_gap:
                return False
            JTAG_ASYNC_REFRESH_LAST_TS = now_ts
            def _worker():
                global JTAG_DISCOVERY_IN_PROGRESS
                acquired = JTAG_DISCOVERY_LOCK.acquire(blocking=False)
                if not acquired:
                    return
                JTAG_DISCOVERY_IN_PROGRESS = True
                try:
                    _discover_jtag_live_scan(cache_seconds=int(load_config().get("jtag_cache_seconds", 5)), source=f"async_{reason}")
                except Exception as e:
                    try:
                        cached = JTAG_CACHE.get("data") or {}
                        cached = dict(cached)
                        cached["async_refresh_error"] = str(e)
                        cached["async_refresh_error_at"] = now_iso()
                        JTAG_CACHE["data"] = cached
                    except Exception:
                        pass
                finally:
                    JTAG_DISCOVERY_IN_PROGRESS = False
                    try:
                        JTAG_DISCOVERY_LOCK.release()
                    except Exception:
                        pass
            t = threading.Thread(target=_worker, daemon=True, name="jtag_async_cache_refresh")
            JTAG_ASYNC_REFRESH_THREAD = t
            t.start()
            return True
    except Exception:
        return False


def discover_jtag(force: bool = False) -> Dict[str, Any]:
    """JTAG discovery with nonblocking cache behavior.

    v4.29 fix:
    - /boards and GUI auto-refresh no longer run SSH/quartus_pgm -l directly.
    - Non-force reads return cached JTAG data instantly, even if stale.
    - If cache is stale, a background refresh starts and the caller still returns.
    - Manual force=1 may still do a live scan, but with shorter timeout.
    This prevents GUI JTAG poll timeouts from blocking queue stream/dispatch.
    """
    global JTAG_DISCOVERY_IN_PROGRESS
    cfg = load_config()
    cache_seconds = int(cfg.get("jtag_cache_seconds", 5) or 5)
    stale_ok_seconds = int((cfg.get("jtag_nonblocking", {}) or {}).get("stale_cache_ok_seconds", 300) or 300)
    now = time.time()
    cached = JTAG_CACHE.get("data")
    cache_age = now - float(JTAG_CACHE.get("time", 0) or 0)

    setup_problem = jtag_configuration_problem()
    if setup_problem:
        return jtag_not_configured_response(
            source="jtag_setup_missing" if not force else "manual_force_jtag_setup_missing",
            cache_seconds=cache_seconds,
            cache_used=not force,
        )

    # Fast path for GUI polling and queue planning: never block on SSH.
    if not force and cached:
        data = dict(cached)
        data["cache_used"] = True
        data["cache_age_seconds"] = int(cache_age)
        data["live_poll_seconds"] = cache_seconds
        data["nonblocking_jtag"] = True
        if cache_age >= cache_seconds:
            data["async_refresh_started"] = _start_jtag_async_refresh("cache_stale")
        return data

    if not force and not cached:
        # First request after restart should still return quickly. The bash startup
        # prewarm normally fills the cache before Flask starts; if it did not, start
        # async refresh and return an empty-but-successful structure so /boards does
        # not hang the GUI or queue stream.
        started = _start_jtag_async_refresh("empty_cache")
        return {
            "success": True,
            "timestamp": now_iso(),
            "server_host": server_info().get("host"),
            "cables": [],
            "results": [],
            "errors": [],
            "cache_used": True,
            "cache_empty": True,
            "async_refresh_started": started,
            "nonblocking_jtag": True,
            "live_poll_seconds": cache_seconds,
        }

    # Manual force path. Single-flight protection still applies.
    if JTAG_DISCOVERY_IN_PROGRESS and cached:
        data = dict(cached)
        data["cache_used"] = True
        data["scan_skipped"] = "another_jtag_scan_in_progress"
        data["cache_age_seconds"] = int(cache_age)
        data["live_poll_seconds"] = cache_seconds
        return data

    acquired = JTAG_DISCOVERY_LOCK.acquire(blocking=False)
    if not acquired:
        if cached:
            data = dict(cached)
            data["cache_used"] = True
            data["scan_skipped"] = "jtag_lock_busy"
            data["cache_age_seconds"] = int(cache_age)
            data["live_poll_seconds"] = cache_seconds
            return data
        acquired = JTAG_DISCOVERY_LOCK.acquire(timeout=1)
        if not acquired:
            return {
                "success": True,
                "timestamp": now_iso(),
                "server_host": server_info().get("host"),
                "cables": [],
                "results": [],
                "errors": [{"error": "JTAG scan busy and no cache available"}],
                "cache_used": True,
                "cache_empty": True,
                "live_poll_seconds": cache_seconds,
            }

    JTAG_DISCOVERY_IN_PROGRESS = True
    try:
        return _discover_jtag_live_scan(cache_seconds=cache_seconds, source="manual_force_jtag_scan" if force else "jtag_discovery")
    finally:
        JTAG_DISCOVERY_IN_PROGRESS = False
        try:
            JTAG_DISCOVERY_LOCK.release()
        except RuntimeError:
            pass




def active_programming_jobs_for_prewarm() -> List[str]:
    """Return job ids currently using quartus_pgm/programming.

    Testing reservations are intentionally not treated as active programming; the
    prewarm daemon only lists cables and wakes jtagd.  Actual quartus_pgm program
    commands still serialize through the job runner logic.
    """
    try:
        state = load_state()
        active: List[str] = []
        for jid, job in (state.get("jobs", {}) or {}).items():
            if not isinstance(job, dict):
                continue
            status = str(job.get("status") or "").lower()
            phase = str(job.get("running_phase") or "").lower()
            if status == "running" and phase not in ("testing", "reserved_testing"):
                active.append(str(jid))
            elif phase in ("programming", "jtag_warmup", "copy_sof", "sof_copy"):
                active.append(str(jid))
        return active
    except Exception:
        return []


def jtag_prewarm_daemon_config() -> Dict[str, Any]:
    cfg = load_config()
    p = cfg.get("jtag_prewarm_daemon", {}) or {}
    def b(key: str, default: bool) -> bool:
        return bool(p.get(key, default))
    def i(key: str, default: int) -> int:
        try:
            return max(1, int(p.get(key, default)))
        except Exception:
            return int(default)
    def f(key: str, default: float) -> float:
        try:
            return max(0.0, float(p.get(key, default)))
        except Exception:
            return float(default)
    return {
        "enabled": b("enabled", True),
        "interval_seconds": i("interval_seconds", 45),
        "startup_iterations": i("startup_iterations", 3),
        "startup_delay_seconds": f("startup_delay_seconds", 2.0),
        "timeout_seconds": i("timeout_seconds", 40),
        "pause_when_programming": b("pause_when_programming", True),
        "run_jtagconfig": b("run_jtagconfig", True),
        "refresh_cache": b("refresh_cache", True),
    }


def jtag_prewarm_once(reason: str = "manual") -> Dict[str, Any]:
    """Warm the Quartus/JTAG server before a job is queued.

    It runs the non-programming discovery commands only:
      - quartus_pgm -l for each configured Quartus family
      - jtagconfig when available

    This keeps jtagd/cable enumeration ready, while avoiding a conflict with an
    active programming command.
    """
    global JTAG_PREWARM_LAST_RESULT, JTAG_PREWARM_HEARTBEAT_TS
    pcfg = jtag_prewarm_daemon_config()
    if not pcfg.get("enabled"):
        return ok(enabled=False, skipped=True, reason="jtag prewarm disabled")
    active = active_programming_jobs_for_prewarm()
    if pcfg.get("pause_when_programming") and active:
        res = ok(skipped=True, reason="active programming in progress", active_programming_jobs=active)
        JTAG_PREWARM_LAST_RESULT = res
        return res
    acquired = JTAG_PREWARM_LOCK.acquire(blocking=False)
    if not acquired:
        res = ok(skipped=True, reason="another prewarm cycle already running")
        JTAG_PREWARM_LAST_RESULT = res
        return res
    try:
        JTAG_PREWARM_HEARTBEAT_TS = time.time()
        info = server_info()
        families: List[str] = []
        if info.get("quartus_standard"):
            families.append("standard")
        if info.get("quartus_pro"):
            families.append("pro")
        results: List[Dict[str, Any]] = []
        all_cables: List[str] = []
        errors: List[Dict[str, Any]] = []
        timeout = int(pcfg.get("timeout_seconds", 40) or 40)
        for fam in families:
            exe = quartus_exe(fam)
            cmd_parts = [f"{shlex.quote(exe)} -l"]
            if pcfg.get("run_jtagconfig"):
                cmd_parts.append("(jtagconfig 2>/dev/null || true)")
            cmd_parts.append(f"{shlex.quote(exe)} -l")
            cmd = "; ".join(cmd_parts)
            try:
                code, out, err = run_remote(cmd, timeout=timeout)
                cables = parse_quartus_cables((out or "") + "\n" + (err or ""))
                for c in cables:
                    if c not in all_cables:
                        all_cables.append(c)
                results.append({
                    "family": fam,
                    "command": cmd,
                    "returncode": code,
                    "cables": cables,
                    "stdout_tail": _tail_text(out, 1600),
                    "stderr_tail": _tail_text(err, 1600),
                })
            except Exception as e:
                errors.append({"family": fam, "error": str(e)})
        data = {
            "success": bool(all_cables) or not errors,
            "timestamp": now_iso(),
            "reason": reason,
            "prewarm_daemon": True,
            "families": families,
            "cables": all_cables,
            "results": results,
            "errors": errors,
            "active_programming_jobs": active,
        }
        if pcfg.get("refresh_cache") and all_cables:
            cache_data = {
                "success": True,
                "timestamp": data["timestamp"],
                "server_host": server_info().get("host"),
                "cables": all_cables,
                "results": results,
                "errors": errors,
                "cache_used": False,
                "live_poll_seconds": int(load_config().get("jtag_cache_seconds", 5)),
                "source": "jtag_prewarm_daemon",
            }
            JTAG_CACHE["time"] = time.time()
            JTAG_CACHE["data"] = cache_data
            try:
                adaptive_runtime_config(refresh=True, jtag_data=cache_data, source="jtag_prewarm_daemon")
            except Exception:
                pass
        JTAG_PREWARM_LAST_RESULT = data
        return data
    finally:
        try:
            JTAG_PREWARM_LOCK.release()
        except Exception:
            pass


def jtag_prewarm_worker_loop() -> None:
    global JTAG_PREWARM_HEARTBEAT_TS
    pcfg = jtag_prewarm_daemon_config()
    # Multiple startup passes are intentional: this gives jtagd and USB-Blaster
    # cables time to settle before the first classroom upload.
    for n in range(int(pcfg.get("startup_iterations", 3) or 3)):
        try:
            JTAG_PREWARM_HEARTBEAT_TS = time.time()
            jtag_prewarm_once(f"startup_prewarm_{n + 1}")
        except Exception as e:
            JTAG_PREWARM_LAST_RESULT.update({"success": False, "error": str(e), "reason": "startup_exception"})
        time.sleep(float(pcfg.get("startup_delay_seconds", 2.0) or 2.0))
    while True:
        try:
            pcfg = jtag_prewarm_daemon_config()
            JTAG_PREWARM_HEARTBEAT_TS = time.time()
            if pcfg.get("enabled", True):
                jtag_prewarm_once("periodic_idle_prewarm")
            time.sleep(float(pcfg.get("interval_seconds", 45) or 45))
        except Exception as e:
            JTAG_PREWARM_LAST_RESULT.update({"success": False, "error": str(e), "reason": "worker_exception"})
            time.sleep(30.0)


def ensure_jtag_prewarm_worker() -> None:
    global JTAG_PREWARM_WORKER_STARTED, JTAG_PREWARM_WORKER_THREAD
    pcfg = jtag_prewarm_daemon_config()
    if not pcfg.get("enabled", True):
        JTAG_PREWARM_WORKER_STARTED = False
        return
    if JTAG_PREWARM_WORKER_THREAD and JTAG_PREWARM_WORKER_THREAD.is_alive():
        JTAG_PREWARM_WORKER_STARTED = True
        return
    t = threading.Thread(target=jtag_prewarm_worker_loop, daemon=True, name="jtag_prewarm_daemon")
    JTAG_PREWARM_WORKER_THREAD = t
    JTAG_PREWARM_WORKER_STARTED = True
    t.start()


def _norm_token_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()


def cable_matches(configured: str, detected: List[str]) -> Tuple[bool, str]:
    """Match one configured alias to detected JTAG cable names.

    This function is generic. Board-specific names belong in config_pi_hat.json
    under each board's jtag_aliases list, not in Python code.
    """
    configured_raw = (configured or "").strip()
    if not configured_raw:
        return False, ""

    configured_norm = configured_raw.lower()
    configured_tokens = _norm_token_text(configured_raw).split()

    for cable in detected:
        cable_raw = (cable or "").strip()
        c_norm = cable_raw.lower()
        cable_tokens = _norm_token_text(cable_raw).split()

        if configured_norm == c_norm:
            return True, cable

        # Exact token containment: "DE10 Agilex" matches "DE10 Agilex 1 7 1".
        # It avoids accidental substring matches.
        if configured_tokens and all(tok in cable_tokens for tok in configured_tokens):
            return True, cable

        # Safe phrase containment only for aliases of length >= 4.
        # Short aliases like "DE1" are avoided because they can collide with other names.
        # v4.24: never let generic USB-Blaster match USB-BlasterII.
        if len(configured_norm) >= 4 and configured_norm in c_norm:
            if "usb-blaster" in configured_norm and "usb-blasterii" in c_norm and "usb-blasterii" not in configured_norm:
                continue
            return True, cable

    return False, ""


def cable_matches_any(configured_values: List[str], detected: List[str]) -> Tuple[bool, str]:
    for configured in configured_values:
        ok, cable = cable_matches(configured, detected)
        if ok:
            return True, cable
    return False, ""


# =========================
# Board availability / locks
# =========================
def lock_board(board: str, owner: str = "api", expected_seconds: Optional[int] = None, job_id: str = "") -> Dict[str, Any]:
    cleanup_expired_locks()
    boards = load_config().get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    state = load_state()
    current = state.setdefault("locks", {}).get(board, {})
    if current.get("busy") and not is_lock_expired(current):
        return fail("Board is already busy", board=board, lock=current)
    state["locks"][board] = {"busy": True, "owner": owner, "locked_at": time.time(), "locked_at_iso": now_iso(), "expected_seconds": int(expected_seconds or 0), "job_id": job_id}
    save_state(state)
    set_led(boards[board], busy=True, ready=False)
    add_history("lock", board, {"owner": owner, "job_id": job_id, "expected_seconds": int(expected_seconds or 0)})
    return ok(board=board, busy=True, expected_seconds=int(expected_seconds or 0), job_id=job_id)


def release_board(board: str, reason: str = "released") -> Dict[str, Any]:
    boards = load_config().get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    state = load_state()
    state.setdefault("locks", {})[board] = {"busy": False, "released_at": time.time(), "released_at_iso": now_iso(), "reason": reason}
    save_state(state)
    set_led(boards[board], busy=False, ready=True)
    add_history("release", board, {"reason": reason})
    return ok(board=board, busy=False, reason=reason)



def instance_lock_key(board: str, cable: str) -> str:
    cable = (cable or "").strip()
    return f"{board}::{cable}" if cable else board


def _usage_entry_for(board: str, cable: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return persistent usage data for one physical JTAG cable."""
    if state is None:
        state = load_state()
    key = instance_lock_key(board, cable)
    usage = state.setdefault("jtag_usage", {})
    raw = usage.get(key, {}) or {}
    return {
        "lock_key": key,
        "program_count": int(raw.get("program_count", 0) or 0),
        "success_count": int(raw.get("success_count", 0) or 0),
        "fail_count": int(raw.get("fail_count", 0) or 0),
        "total_program_seconds": float(raw.get("total_program_seconds", 0.0) or 0.0),
        "last_used_ts": float(raw.get("last_used_ts", 0.0) or 0.0),
        "last_used_at": raw.get("last_used_at", ""),
        "last_job_id": raw.get("last_job_id", ""),
        "last_success": raw.get("last_success", None),
    }


def record_jtag_usage(board: str, cable: str, job_id: str, success: bool, start_ts: float, end_ts: Optional[float] = None, sof_name: str = "") -> Dict[str, Any]:
    """Record one completed programming attempt for one physical JTAG cable."""
    end_ts = float(end_ts or time.time())
    start_ts = float(start_ts or end_ts)
    duration = max(0.0, end_ts - start_ts)
    key = instance_lock_key(board, cable)
    state = load_state()
    usage = state.setdefault("jtag_usage", {})
    entry = dict(usage.get(key, {}) or {})
    entry["board"] = board
    entry["detected_cable"] = cable
    entry["lock_key"] = key
    entry["program_count"] = int(entry.get("program_count", 0) or 0) + 1
    if success:
        entry["success_count"] = int(entry.get("success_count", 0) or 0) + 1
    else:
        entry["fail_count"] = int(entry.get("fail_count", 0) or 0) + 1
    entry["total_program_seconds"] = float(entry.get("total_program_seconds", 0.0) or 0.0) + duration
    entry["last_program_seconds"] = duration
    entry["last_used_ts"] = end_ts
    entry["last_used_at"] = now_iso()
    entry["last_job_id"] = job_id
    entry["last_success"] = bool(success)
    entry["last_sof_name"] = Path(sof_name).name if sof_name else ""
    usage[key] = entry
    save_state(state)
    add_history("jtag_usage_update", board, {
        "job_id": job_id,
        "detected_cable": cable,
        "success": bool(success),
        "duration_seconds": int(duration),
        "program_count": entry["program_count"],
        "total_program_seconds": int(entry["total_program_seconds"]),
    })
    return entry


def select_balanced_jtag_instance(scored_instances: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]]) -> Tuple[int, str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Pick the best AI-scored board, then balance between equal-scoring physical JTAG cables.

    Priority:
    1. Highest AI board score.
    2. Lowest program_count, so unused JTAG cables are used first.
    3. Lowest total_program_seconds, so after all are used it picks the least-used-by-time cable.
    4. Oldest last_used_ts.
    5. Lowest raw JTAG order for deterministic tie-break.
    """
    if not scored_instances:
        raise ValueError("No scored JTAG instances to select.")

    best_score = max(item[0] for item in scored_instances)
    candidates = [item for item in scored_instances if item[0] == best_score]
    state = load_state()

    def balance_key(item):
        _score, board, inst, _bcfg = item
        cable = inst.get("detected_cable", "")
        usage = _usage_entry_for(board, cable, state)
        return (
            usage.get("program_count", 0),
            usage.get("total_program_seconds", 0.0),
            usage.get("last_used_ts", 0.0),
            int(inst.get("raw_index", 9999) or 9999),
            board,
            cable,
        )

    candidates.sort(key=balance_key)
    score, board, inst, bcfg = candidates[0]
    usage = _usage_entry_for(board, inst.get("detected_cable", ""), state)
    return score, board, inst, bcfg, usage


def mark_queue_job_jtag(job_id: str, board: str, cable: str, instance_id: str = "", lock_key: str = "", ai_result: Optional[Dict[str, Any]] = None) -> None:
    """Update a running queue job as soon as AI chooses a physical JTAG cable."""
    if not job_id:
        return
    try:
        state = load_state()
        jobs = state.setdefault("jobs", {})
        if job_id not in jobs:
            return
        job = jobs[job_id]
        job["selected_board"] = board
        job["ai_selected_board"] = board
        job["selected_jtag_cable"] = cable
        job["jtag_cable"] = cable
        job["selected_instance_id"] = instance_id
        job["jtag_instance"] = instance_id
        job["selected_lock_key"] = lock_key or instance_lock_key(board, cable)
        # v4.24: freeze the exact Quartus/JTAG target used by this run.
        # This prevents a later live scan from mixing DE10-Agilex with a DE1-SoC cable.
        cfg = load_config()
        bcfg = (cfg.get("board_catalog", {}) or {}).get(board, {}) or {}
        job["locked_target_board"] = board
        job["locked_target_jtag_cable"] = cable
        job["locked_target_instance_id"] = instance_id
        job["locked_target_lock_key"] = lock_key or instance_lock_key(board, cable)
        job["locked_target_device_index"] = str((ai_result or {}).get("selected_device_index") or bcfg.get("jtag_device_index", ""))
        job["locked_target_quartus_family"] = str((ai_result or {}).get("selected_quartus_family") or bcfg.get("quartus_family", "standard"))
        job["locked_target_policy"] = "v4.25_exact_target_with_warmup"
        job["message"] = f"running on {instance_id or 'JTAG'} | {cable}"
        if ai_result:
            job["selection_reason"] = ai_result.get("reason", "")
        jobs[job_id] = job
        save_state(state)
    except Exception:
        pass



def lock_instance(board: str, cable: str, owner: str = "api", expected_seconds: Optional[int] = None, job_id: str = "") -> Dict[str, Any]:
    """Lock one physical JTAG instance instead of the whole board family."""
    cleanup_expired_locks()
    boards = load_config().get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    if not cable:
        return fail("No JTAG cable was selected for this board instance.", board=board)

    key = instance_lock_key(board, cable)
    state = load_state()
    current = state.setdefault("locks", {}).get(key, {})
    if current.get("busy") and not is_lock_expired(current):
        if job_id and str(current.get("job_id") or "") == str(job_id):
            return ok(board=board, detected_cable=cable, lock_key=key, busy=True, expected_seconds=int(current.get("expected_seconds", expected_seconds or 0) or 0), job_id=job_id, reused_existing_lock=True)
        return fail("Board instance is already busy", board=board, detected_cable=cable, lock=current)

    state["locks"][key] = {
        "busy": True,
        "owner": owner,
        "locked_at": time.time(),
        "locked_at_iso": now_iso(),
        "expected_seconds": int(expected_seconds or 0),
        "job_id": job_id,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
    }
    save_state(state)
    set_led(boards[board], busy=True, ready=False)
    add_history("lock_instance", board, {"owner": owner, "job_id": job_id, "expected_seconds": int(expected_seconds or 0), "detected_cable": cable, "lock_key": key})
    return ok(board=board, detected_cable=cable, lock_key=key, busy=True, expected_seconds=int(expected_seconds or 0), job_id=job_id)


def release_instance(board: str, cable: str, reason: str = "released") -> Dict[str, Any]:
    boards = load_config().get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    key = instance_lock_key(board, cable)
    state = load_state()
    state.setdefault("locks", {})[key] = {
        "busy": False,
        "released_at": time.time(),
        "released_at_iso": now_iso(),
        "reason": reason,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
    }
    save_state(state)
    set_led(boards[board], busy=False, ready=True)
    add_history("release_instance", board, {"reason": reason, "detected_cable": cable, "lock_key": key})
    return ok(board=board, detected_cable=cable, lock_key=key, busy=False, reason=reason)




def hold_instance_for_testing(board: str, cable: str, job_id: str, test_seconds: int, reason: str = "student_test_time") -> Dict[str, Any]:
    """Keep a successfully programmed physical JTAG instance locked for student testing."""
    boards = load_config().get("board_catalog", {})
    if board not in boards:
        return fail(f"Board not configured: {board}", board=board)
    key = instance_lock_key(board, cable)
    test_seconds = max(0, int(test_seconds or 0))
    if test_seconds <= 0:
        return release_instance(board, cable, reason="no_test_timer")

    now_ts = time.time()
    state = load_state()
    state.setdefault("locks", {})[key] = {
        "busy": True,
        "owner": f"testing:{job_id}",
        "locked_at": now_ts,
        "locked_at_iso": now_iso(),
        "expected_seconds": test_seconds,
        "job_id": job_id,
        "board": board,
        "detected_cable": cable,
        "lock_key": key,
        "phase": "testing",
        "reason": reason,
        "test_seconds": test_seconds,
        "test_minutes": int(round(test_seconds / 60)),
        "test_end_ts": now_ts + test_seconds,
        "test_end_at": iso_from_ts(now_ts + test_seconds),
    }
    save_state(state)
    set_led(boards[board], busy=True, ready=False)
    add_history("test_timer_start", board, {
        "job_id": job_id,
        "detected_cable": cable,
        "lock_key": key,
        "test_seconds": test_seconds,
        "test_minutes": int(round(test_seconds / 60)),
        "test_end_at": iso_from_ts(now_ts + test_seconds),
    })
    return ok(
        board=board,
        detected_cable=cable,
        lock_key=key,
        busy=True,
        phase="testing",
        reason=reason,
        test_seconds=test_seconds,
        test_minutes=int(round(test_seconds / 60)),
        test_end_ts=now_ts + test_seconds,
        test_end_at=iso_from_ts(now_ts + test_seconds),
        job_id=job_id,
    )


def active_job_slot_claims(state: Dict[str, Any], allow_job_id: str = "") -> Dict[str, Dict[str, Any]]:
    """
    Build a hard slot-claim map from active jobs.

    Locks alone are not enough: if a lock is stale/missing but the jobs table
    still says a job is running/testing on DE1-SoC-2, that slot must still be
    treated as occupied. This prevents two active jobs from sharing one physical FPGA.
    """
    now_ts = time.time()
    claims: Dict[str, Dict[str, Any]] = {}
    jobs = state.get("jobs", {}) or {}
    allow_job_id = str(allow_job_id or "")

    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        if allow_job_id and jid == allow_job_id:
            continue

        status = str(job.get("status") or "").lower()
        if status not in ("running", "testing"):
            continue

        board = (
            job.get("selected_board")
            or job.get("ai_selected_board")
            or job.get("planned_board")
            or job.get("requested_board")
            or ""
        )
        cable = (
            job.get("jtag_cable")
            or job.get("selected_jtag_cable")
            or job.get("planned_jtag_cable")
            or ""
        )
        lock_key = job.get("selected_lock_key") or job.get("planned_lock_key") or ""
        if board and cable:
            lock_key = instance_lock_key(board, cable)
        if not lock_key:
            continue

        # Estimate remaining occupancy.
        remaining = 0
        if status == "testing":
            end_ts = float(job.get("test_end_ts", 0) or 0)
            if end_ts:
                remaining = max(0, int(end_ts - now_ts))
            else:
                remaining = int(job.get("remaining_seconds", 0) or job.get("test_seconds", 0) or 0)
        else:
            started = float(job.get("started_ts", 0) or 0)
            # v4.16: running slot claim is programming-only; testing creates its own lock.
            expected = int(job.get("program_estimated_seconds", 0) or estimate_program_seconds(str(board)) or 60)
            if started and expected:
                remaining = max(1, expected - int(now_ts - started))
            else:
                remaining = max(1, expected or 60)

        claim = {
            "job_id": jid,
            "status": status,
            "board": board,
            "detected_cable": cable,
            "lock_key": lock_key,
            "remaining_seconds": int(max(0, remaining)),
            "started_ts": float(job.get("started_ts", job.get("created_ts", 0)) or 0),
            "created_ts": float(job.get("created_ts", 0) or 0),
        }

        # If two jobs already claim the same slot, keep the oldest/testing claim
        # as the blocker. The repair function below cleans up the extra active rows.
        existing = claims.get(lock_key)
        if not existing:
            claims[lock_key] = claim
        else:
            existing_rank = (0 if existing.get("status") == "testing" else 1, existing.get("started_ts", 0) or existing.get("created_ts", 0))
            new_rank = (0 if status == "testing" else 1, claim.get("started_ts", 0) or claim.get("created_ts", 0))
            if new_rank < existing_rank:
                claims[lock_key] = claim

    return claims


def active_claim_for_slot(state: Dict[str, Any], board: str, cable: str, allow_job_id: str = "") -> Dict[str, Any]:
    if not board or not cable:
        return {}
    return active_job_slot_claims(state, allow_job_id=allow_job_id).get(instance_lock_key(board, cable), {})


def repair_active_slot_conflicts(state: Dict[str, Any]) -> bool:
    """
    Self-heal bad state where two active jobs are assigned to the same slot.

    Keep the oldest/testing job as the slot owner. Return newer duplicate active
    jobs to queued, so the GUI will show Wait instead of two active rows on one board.
    """
    jobs = state.setdefault("jobs", {})
    by_key: Dict[str, list] = {}

    for jid, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").lower()
        if status not in ("running", "testing"):
            continue
        board = job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or job.get("requested_board") or ""
        cable = job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or ""
        key = job.get("selected_lock_key") or job.get("planned_lock_key") or ""
        if board and cable:
            key = instance_lock_key(board, cable)
        if not key:
            continue
        by_key.setdefault(key, []).append((jid, job))

    changed = False
    for key, items in by_key.items():
        if len(items) <= 1:
            continue

        def keep_rank(item):
            jid, job = item
            status = str(job.get("status") or "").lower()
            # Keep testing before running; then oldest started/created.
            return (
                0 if status == "testing" else 1,
                float(job.get("started_ts", job.get("created_ts", 0)) or 0),
                float(job.get("created_ts", 0) or 0),
            )

        items.sort(key=keep_rank)
        keeper_id, keeper = items[0]
        for victim_id, victim in items[1:]:
            if victim_id == state.get("current_job"):
                # If the worker is already programming it, do not pretend we killed Quartus.
                # Mark it as waiting_requested; the worker will requeue/finalize safely.
                victim["last_wait_reason"] = "hard_slot_guard_detected_conflict_with_" + keeper_id
                victim["message"] = f"hard slot guard: duplicate active slot with {keeper_id}; waiting for safe worker boundary"
                jobs[victim_id] = victim
                continue

            victim["status"] = "queued"
            victim["message"] = f"hard slot guard: slot already occupied by {keeper_id}; returned to queue"
            victim["last_wait_reason"] = "slot_conflict_guard"
            victim["last_wait_at"] = now_iso()
            victim["wait_count"] = int(victim.get("wait_count", 0) or 0) + 1
            victim["started_at"] = ""
            victim["started_ts"] = 0
            victim["finished_at"] = ""
            victim["finished_ts"] = 0
            for k in ("selected_board", "ai_selected_board", "selected_jtag_cable", "jtag_cable", "selected_instance_id", "jtag_instance", "selected_lock_key", "test_timer", "held_for_testing"):
                victim.pop(k, None)
            jobs[victim_id] = victim
            if victim_id not in state.setdefault("queue", []):
                state["queue"].append(victim_id)
            changed = True

    state["jobs"] = jobs
    return changed


def hard_slot_available_for_job(job: Dict[str, Any], state: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Final hard gate before a queued job is allowed to transition to running.
    """
    board = job.get("planned_board") or queue_target_board_hint(job, load_config()) or job.get("requested_board") or ""
    cable = job.get("planned_jtag_cable") or job.get("selected_jtag_cable") or job.get("jtag_cable") or ""
    job_id = job.get("job_id", "")

    if not board or not cable:
        return False, "no planned physical JTAG slot"

    key = instance_lock_key(board, cable)
    lock = state.setdefault("locks", {}).get(key, {})
    if lock.get("busy") and not is_lock_expired(lock) and str(lock.get("job_id") or "") != str(job_id):
        return False, f"planned slot busy by lock {lock.get('job_id', '')}"

    claim = active_claim_for_slot(state, board, cable, allow_job_id=job_id)
    if claim:
        return False, f"planned slot occupied by active job {claim.get('job_id')} ({claim.get('status')})"

    return True, "slot available"

def instance_lock_timing(board: str, cable: str, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    """Return busy flag, lock object, and timing for an instance."""
    locks = state.setdefault("locks", {})
    key = instance_lock_key(board, cable)
    lock = locks.get(key, {})

    # Backward compatibility: an old manual board lock still blocks all instances of that board.
    board_lock = locks.get(board, {})
    if board_lock.get("busy") and not is_lock_expired(board_lock):
        lock = board_lock

    busy = bool(lock.get("busy")) and not is_lock_expired(lock)
    timing = timing_fields(float(lock.get("locked_at", 0) or 0), int(lock.get("expected_seconds", 0) or 0), active=busy)

    active_claim = active_claim_for_slot(state, board, cable)
    if active_claim:
        busy = True
        if not lock:
            lock = {
                "busy": True,
                "owner": f"active_job:{active_claim.get('job_id')}",
                "job_id": active_claim.get("job_id", ""),
                "board": board,
                "detected_cable": cable,
                "lock_key": instance_lock_key(board, cable),
                "phase": active_claim.get("status", ""),
                "source": "active_job_slot_claim",
            }
        timing["remaining_seconds"] = max(int(timing.get("remaining_seconds", 0) or 0), int(active_claim.get("remaining_seconds", 0) or 0))
        timing["expected_seconds"] = max(int(timing.get("expected_seconds", 0) or 0), int(active_claim.get("remaining_seconds", 0) or 0))
    return busy, lock, timing



def _jtag_alias_match_score(alias: str, cable: str) -> int:
    """Return a match score for one alias against one detected cable.

    v4.24: avoid the dangerous USB-Blaster/USB-BlasterII collision.  Earlier
    versions could match the generic alias "USB-Blaster" against
    "USB-BlasterII", which can make an Agilex cable look like a DE1 cable or
    let the runner rebuild the target with the wrong Quartus/JTAG family.
    """
    alias_raw = str(alias or "").strip()
    cable_raw = str(cable or "").strip()
    if not alias_raw or not cable_raw:
        return 0
    a = alias_raw.lower()
    c = cable_raw.lower()
    a_tokens = _norm_token_text(alias_raw).split()
    c_tokens = _norm_token_text(cable_raw).split()

    # Exact full string is strongest.
    if a == c:
        return 1000 + len(a)

    # Prevent USB-Blaster from matching USB-BlasterII by substring.  This is the
    # most important cable-family safety rule for this lab.
    if a == "usb-blaster" and "usb-blasterii" in c:
        return 0
    if a == "usb blaster" and "blasterii" in c_tokens:
        return 0

    # Exact token containment, e.g. DE10 Agilex -> DE10 Agilex [1-7.1].
    if a_tokens and all(tok in c_tokens for tok in a_tokens):
        return 500 + sum(len(tok) for tok in a_tokens)

    # Safe phrase containment.  Do not let generic USB-Blaster collide with II.
    if len(a) >= 4 and a in c:
        if "usb-blaster" in a and "usb-blasterii" in c and "usb-blasterii" not in a:
            return 0
        return 200 + len(a)

    return 0


def infer_board_type_from_jtag_cable(cable: str) -> str:
    """
    Infer board type from config_pi_hat.json aliases using best-match scoring.

    v4.24 change: evaluate every board and choose the strongest alias match
    instead of returning the first match. This prevents a generic alias from one
    board family from stealing another board's JTAG cable.
    """
    cfg = load_config()
    catalog = cfg.get("board_catalog", {}) or {}
    best_board = "Unknown"
    best_score = 0
    tied = False
    for board_name, board_cfg in catalog.items():
        board_score = 0
        for alias in board_aliases(board_name, board_cfg):
            board_score = max(board_score, _jtag_alias_match_score(alias, cable))
        if board_score > best_score:
            best_score = board_score
            best_board = board_name
            tied = False
        elif board_score and board_score == best_score:
            tied = True
    return "Unknown" if tied or best_score <= 0 else best_board


def locked_target_from_job(job: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the single physical target a job is allowed to use."""
    cfg = cfg or load_config()
    catalog = cfg.get("board_catalog", {}) or {}
    board = str(job.get("locked_target_board") or job.get("planned_board") or job.get("selected_board") or job.get("ai_selected_board") or queue_target_board_hint(job, cfg) or "").strip()
    cable = str(job.get("locked_target_jtag_cable") or job.get("planned_jtag_cable") or job.get("selected_jtag_cable") or job.get("jtag_cable") or "").strip()
    bcfg = catalog.get(board, {}) or {}
    device_index = str(job.get("locked_target_device_index") or bcfg.get("jtag_device_index", "")).strip()
    quartus_family = str(job.get("locked_target_quartus_family") or bcfg.get("quartus_family", "standard")).strip()
    instance_id = str(job.get("locked_target_instance_id") or job.get("planned_instance_id") or job.get("selected_instance_id") or job.get("jtag_instance") or "").strip()
    lock_key = str(job.get("locked_target_lock_key") or job.get("planned_lock_key") or (instance_lock_key(board, cable) if board and cable else "")).strip()
    return {
        "board": board,
        "jtag_cable": cable,
        "device_index": device_index,
        "quartus_family": quartus_family,
        "instance_id": instance_id,
        "lock_key": lock_key,
        "board_cfg": bcfg,
    }


def freeze_job_programming_target(job: Dict[str, Any]) -> Dict[str, Any]:
    """Freeze board/cable/device-index/quartus-family before the runner starts."""
    target = locked_target_from_job(job)
    board = target.get("board", "")
    cable = target.get("jtag_cable", "")
    if board:
        job["locked_target_board"] = board
    if cable:
        job["locked_target_jtag_cable"] = cable
    if target.get("instance_id"):
        job["locked_target_instance_id"] = target.get("instance_id")
    if target.get("lock_key"):
        job["locked_target_lock_key"] = target.get("lock_key")
    if target.get("device_index"):
        job["locked_target_device_index"] = target.get("device_index")
    if target.get("quartus_family"):
        job["locked_target_quartus_family"] = target.get("quartus_family")
    job["locked_target_policy"] = "v4.25_board_cable_device_quartus_family_frozen_before_warmup_quartus_pgm"
    return job


def validate_locked_programming_target(board: str, cable: str, job_id: str = "") -> Dict[str, Any]:
    """Safety gate before programming without blocking FIFO dispatch.

    v4.28: do not force a live JTAG scan during queued -> running dispatch.
    If the live scan/cache is busy or empty, allow dispatch and let the v4.25
    JTAG warmup/retry prove the cable. Hard-stop only true mismatches/disabled
    targets so jobs do not sit queued forever with Wait=00:00.
    """
    cfg = load_config()
    catalog = cfg.get("board_catalog", {}) or {}
    if board not in catalog:
        return fail("Programming target safety stop: board is not configured", board=board, jtag_cable=cable, system_requeue=True, target_validation_failed=True)
    if not cable:
        return fail("Programming target safety stop: no locked JTAG cable was selected", board=board, system_requeue=True, target_validation_failed=True)
    inferred = infer_board_type_from_jtag_cable(cable)
    if inferred != board:
        return fail(
            f"Programming target safety stop: selected board {board} but locked JTAG cable is {cable} ({inferred}). Replanning instead of running quartus_pgm.",
            board=board,
            jtag_cable=cable,
            inferred_board=inferred,
            system_requeue=True,
            target_validation_failed=True,
        )
    bcfg = catalog.get(board, {}) or {}
    idx = str(bcfg.get("jtag_device_index", "")).strip()
    if not idx:
        return fail("Programming target safety stop: configured JTAG device index is empty", board=board, jtag_cable=cable, system_requeue=True, target_validation_failed=True)
    key = instance_lock_key(board, cable)
    disabled = (load_state().get("disabled_jtag", {}) or {}).get(key, {})
    if disabled:
        return fail("Programming target safety stop: selected JTAG slot is disabled; replanning", board=board, jtag_cable=cable, disabled_info=disabled, system_requeue=True, target_validation_failed=True)

    detected: List[str] = []
    jtag_note = "not_checked"
    try:
        jtag = discover_jtag(force=False)
        detected = [str(x or "").strip() for x in (jtag.get("cables", []) or []) if str(x or "").strip()]
        if detected:
            if cable not in detected:
                return fail(
                    "Programming target safety stop: locked JTAG cable is not currently detected; replanning",
                    board=board,
                    jtag_cable=cable,
                    detected_cables=detected,
                    system_requeue=True,
                    target_validation_failed=True,
                )
            jtag_note = "detected_in_cache_or_live_scan"
        else:
            jtag_note = str(jtag.get("scan_skipped") or "no_detected_cables_available_to_dispatch")
    except Exception as e:
        jtag_note = f"jtag_validation_degraded:{e}"

    return ok(
        board=board,
        jtag_cable=cable,
        device_index=idx,
        quartus_family=str(bcfg.get("quartus_family", "standard")),
        lock_key=key,
        target_validation="passed_degraded_ok_to_dispatch" if not detected else "passed",
        detected_cables=detected,
        jtag_validation_note=jtag_note,
    )


def board_status(force_jtag: bool = False, allow_job_id: str = "") -> Dict[str, Any]:
    cleanup_expired_locks()
    cfg = load_config()
    state = load_state()
    jtag = discover_jtag(force=force_jtag)
    detected = jtag.get("cables", [])
    boards_out: Dict[str, Any] = {}
    instances: List[Dict[str, Any]] = []
    raw_instances: List[Dict[str, Any]] = []

    catalog = cfg.get("board_catalog", {})

    # Build raw real-time JTAG instances from quartus_pgm -l.
    # Board type is inferred from config aliases, not Python hardcoding.
    for raw_idx, cable in enumerate(detected, start=1):
        inferred_board = infer_board_type_from_jtag_cable(cable)
        b = catalog.get(inferred_board, {})
        busy, lock, lock_timing = instance_lock_timing(inferred_board, cable, state) if inferred_board in catalog else (False, {}, {})
        lock_key = instance_lock_key(inferred_board, cable) if inferred_board in catalog else ""
        if allow_job_id and inferred_board in catalog:
            # A job may be marked running before it calls AI selection. Do not let its
            # own active claim block itself, but still block every other active job.
            claim = active_claim_for_slot(state, inferred_board, cable, allow_job_id=allow_job_id)
            if claim:
                busy = True
                lock = lock or {"source": "active_job_slot_claim", "job_id": claim.get("job_id", "")}
                lock_timing["remaining_seconds"] = max(int(lock_timing.get("remaining_seconds", 0) or 0), int(claim.get("remaining_seconds", 0) or 0))
            elif lock.get("job_id") == allow_job_id:
                busy = False
        disabled_info = state.setdefault("disabled_jtag", {}).get(lock_key, {}) if inferred_board in catalog else {}
        manually_disabled = bool(disabled_info)
        enabled = (bool(b.get("enabled", True)) and not manually_disabled) if inferred_board in catalog else False
        pstatus = physical_status(inferred_board, b) if inferred_board in catalog else "unknown"
        physical_ok = pstatus in ("ok", "ok_simulated", "unknown")
        usage = _usage_entry_for(inferred_board, cable, state) if inferred_board in catalog else {}
        raw_instances.append({
            "instance_id": f"JTAG-{raw_idx}",
            "board": inferred_board,
            "enabled": enabled,
            "jtag_detected": True,
            "available": enabled and physical_ok and (not busy) and inferred_board != "Unknown",
            "busy": busy,
            "manual_disabled": manually_disabled,
            "disabled_info": disabled_info,
            "lock": lock,
            "busy_seconds_elapsed": lock_timing.get("elapsed_seconds", 0),
            "busy_seconds_remaining": lock_timing.get("remaining_seconds", 0),
            "busy_seconds_expected": lock_timing.get("expected_seconds", 0),
            "physical_status": pstatus,
            "power_state": state.get("power", {}).get(inferred_board, "unknown") if inferred_board in catalog else "unknown",
            "detected_cable": cable,
            "quartus_family": b.get("quartus_family", "unknown") if inferred_board in catalog else "unknown",
            "jtag_device_index": str(b.get("jtag_device_index", "")) if inferred_board in catalog else "",
            "features": b.get("features", []) if inferred_board in catalog else [],
            "source": "quartus_pgm -l",
            "raw_index": raw_idx,
            "lock_key": lock_key,
            "program_count": usage.get("program_count", 0),
            "success_count": usage.get("success_count", 0),
            "fail_count": usage.get("fail_count", 0),
            "total_program_seconds": int(usage.get("total_program_seconds", 0) or 0),
            "last_used_at": usage.get("last_used_at", ""),
            "last_used_ts": usage.get("last_used_ts", 0),
            "last_job_id": usage.get("last_job_id", ""),
        })

    for name, b in catalog.items():
        enabled = bool(b.get("enabled", True))
        matching = [inst for inst in raw_instances if inst.get("board") == name]
        detected_cables = [inst.get("detected_cable") for inst in matching if inst.get("detected_cable")]
        available_instances = [inst for inst in matching if inst.get("available")]
        busy_instances = [inst for inst in matching if inst.get("busy")]
        pstatus = physical_status(name, b)
        power_state = state.get("power", {}).get(name, "unknown")
        selected_instance = available_instances[0] if available_instances else (matching[0] if matching else {})
        detected_cable = selected_instance.get("detected_cable", "")

        boards_out[name] = {
            "enabled": enabled,
            "quartus_family": b.get("quartus_family"),
            "jtag_cable": b.get("jtag_cable"),
            "jtag_aliases": b.get("jtag_aliases", []),
            "jtag_device_index": str(b.get("jtag_device_index", "")),
            "jtag_detected": bool(matching),
            "detected_cable": detected_cable,
            "detected_cables": detected_cables,
            "detected_count": len(detected_cables),
            "available": bool(available_instances),
            "available_count": len(available_instances),
            "busy": bool(matching) and not bool(available_instances),
            "busy_count": len(busy_instances),
            "lock": selected_instance.get("lock", {}),
            "busy_seconds_elapsed": selected_instance.get("busy_seconds_elapsed", 0),
            "busy_seconds_remaining": selected_instance.get("busy_seconds_remaining", 0),
            "busy_seconds_expected": selected_instance.get("busy_seconds_expected", 0),
            "physical_status": pstatus,
            "power_state": power_state,
            "features": b.get("features", []),
            "selected_instance_id": selected_instance.get("instance_id", ""),
            "selected_jtag_cable": detected_cable,
            "instances": matching,
        }

        for idx, inst in enumerate(matching, start=1):
            item = dict(inst)
            item["instance_id"] = f"{name}-{idx}"
            instances.append(item)

    return ok(
        boards=boards_out,
        board_instances=instances,
        raw_jtag_instances=raw_instances,
        real_time_source="quartus_pgm -l",
        jtag=jtag,
        queue=queue_snapshot(fast=True),
        timestamp=now_iso(),
    )


def extract_features(verilog_code: str) -> List[str]:
    """
    Detect board requirements from the Verilog/SystemVerilog text.

    This function is required by select_board_ai(). In v3.43 it was accidentally
    removed while changing the controller to dynamic physical JTAG instances.
    """
    text = verilog_code.lower()

    patterns = {
        # Common/simple board features
        "switches": [r"\bsw\s*(?:\[|\(|,|;)", r"\bsw\[\d+\]", r"\bswitch"],
        "leds": [r"\bledr\s*(?:\[|\(|,|;)", r"\bledr\[\d+\]", r"\bleds?\b"],
        "hex": [r"\bhex\d*\b", r"seven", r"7[-_ ]?segment"],
        "keys": [r"\bkey\s*(?:\[|\(|,|;)", r"\bkey\[\d+\]", r"\bpush"],
        "gpio": [r"\bgpio\b", r"jp1", r"jp2"],
        "counter": [r"counter", r"always\s*@\s*\(\s*posedge", r"posedge"],
        "simple_io": [r"\bassign\s+", r"\binput\s+", r"\boutput\s+"],

        # DE10/Agilex-style user I/O names from the QSF template
        "de10_user_io": [
            r"\bbutton[01]\b", r"\bsw[01]\b", r"\bled[0-3]\b",
            r"\bled_bracket\b", r"\bled_bracket[0-3]\b", r"\bcpu_reset_n\b"
        ],
        "de10_buttons": [r"\bbutton[01]\b", r"\bcpu_reset_n\b"],
        "de10_switches": [r"\bsw[01]\b"],
        "de10_leds": [r"\bled[0-3]\b", r"\bled_bracket\b", r"\bled_bracket[0-3]\b"],
        "de10_gpio": [r"\bgpio_p[0-3]\b", r"\bgpio_clk[01]\b"],

        # Agilex/DE10 clock, management, memory, and high-speed interfaces
        "agilex_clocks": [
            r"\bclk_30m72\b", r"\bclk_50_b3a\b", r"\bclk_50_b3c\b",
            r"\bclk_100_b2a_p\b", r"\bclk_from_si5397a_p[01]\b",
            r"\bosc_clk_1\b", r"\bufl_clkin\b"
        ],
        "si5397": [r"\bsi5397a_[a-z0-9_]*\b", r"\bsi5397_[a-z0-9_]*\b"],
        "info_spi": [r"\binfo_spi_", r"\binfo_spi\b"],
        "pcie": [
            r"\bpcie_", r"\bpci_exp", r"\bpcie_rx_[np]\d+\b", r"\bpcie_tx_[np]\d+\b",
            r"\bpcie_refclk_[np]\d+\b", r"\bpcie_perst_n\b", r"\bpcie_clkreq_n\b"
        ],
        "ddr4": [r"\bddr4[a-d]_", r"\bddr4\b", r"\bemif\b", r"\bmemory\b", r"\bmem_"],
        "qsfp": [r"\bqsfpdda_", r"\bqsfpddb_", r"\bqsfpddrsv_", r"\bqsfp\b"],
        "high_speed": [
            r"\btransceiver\b", r"\bxcvr\b", r"\bserdes\b", r"\bgigabit\b",
            r"\bethernet\b", r"\bqsfp\b", r"\bpcie\b"
        ],
        "transceivers": [r"\btransceiver\b", r"\bxcvr\b", r"\bserdes\b", r"\bpcie_rx", r"\bpcie_tx", r"\bqsfp"],
        "advanced": [r"\bpll\b", r"\bjesd\b", r"\baurora\b", r"\bdma\b", r"\baxi\b"],
    }

    found = []
    for feat, regs in patterns.items():
        if any(re.search(p, text) for p in regs):
            found.append(feat)
    return found


def score_board(required: List[str], board_features: List[str], board_name: str) -> int:
    """
    Score a board using mostly config features.
    Board-specific Verilog signals such as SI5397A or LED_BRACKET get extra weight
    so a simple DE10 user-I/O design does not get sent to DE1-SoC.
    """
    req = set(required)
    bset = set(board_features)
    lname = board_name.lower()

    score = len(req & bset) * 2

    advanced_req = bool(req & {"pcie", "ddr4", "qsfp", "high_speed", "transceivers", "advanced"})
    de10_req = bool(req & {
        "de10_user_io", "de10_buttons", "de10_switches", "de10_leds", "de10_gpio",
        "agilex_clocks", "si5397", "info_spi", "pcie", "ddr4", "qsfp", "high_speed", "transceivers"
    })
    de1_req = bool(req & {"switches", "leds", "hex", "keys", "gpio", "simple_io", "counter"})

    # Strong board-specific protection for DE10/Agilex-style signal names.
    if de10_req and ("agilex" in lname or "de10" in lname):
        score += 12
    if de10_req and not ("agilex" in lname or "de10" in lname):
        score -= 12

    if advanced_req and ("agilex" in lname or "de10" in lname):
        score += 8
    if advanced_req and not ("agilex" in lname or "de10" in lname):
        score -= 8

    # Generic/simple names prefer DE1 only when no DE10-specific signal was detected.
    if de1_req and not de10_req and "de1" in lname:
        score += 4
    if de1_req and not de10_req and ("agilex" in lname or "de10" in lname):
        score -= 1

    return score


def confidence_from_score(score: int, required: List[str]) -> str:
    if score >= 8:
        return "high"
    if score >= 3:
        return "medium"
    if required and score > 0:
        return "low"
    return "low"


def confidence_percent_from_score(score: int, required: List[str]) -> int:
    """
    Lightweight numeric confidence for the fast automation path.

    This does not override deterministic safety rules. Conflicts and unknown board
    designs still block Auto. It only avoids expensive extra evaluation when the
    heuristic score is already strongly aligned with one available board.
    """
    if score >= 12:
        return 95
    if score >= 8:
        return 90
    if score >= 5:
        return 85
    if score >= 3:
        return 75
    if required and score > 0:
        return 60
    return 0



def ai_minimum_program_percent() -> int:
    ai_cfg = load_config().get("ai", {})
    return int(ai_cfg.get("minimum_confidence_percent_to_program", 85) or 85)


def ai_fast_decision_threshold_percent() -> int:
    ai_cfg = load_config().get("ai", {})
    # Fast threshold is allowed to be lower than the programming threshold.
    return int(ai_cfg.get("fast_decision_threshold_percent", ai_minimum_program_percent()) or ai_minimum_program_percent())


def confidence_ok(conf: str, percent: int = 0) -> bool:
    """Return True when the design is safe enough to auto-program.

    Numeric confidence is the main path for fast AI decisions. The older
    low/medium/high label remains as a fallback when percent is not available.
    """
    ai_cfg = load_config().get("ai", {})
    minimum = ai_cfg.get("minimum_confidence_to_program", "medium")
    min_percent = ai_minimum_program_percent()
    if int(percent or 0) > 0:
        return int(percent or 0) >= min_percent
    rank = {"low": 1, "medium": 2, "high": 3}
    return rank.get(conf, 0) >= rank.get(minimum, 2)



# =========================
# Optional Ollama/Qwen classifier
# =========================


def ollama_qwen_config() -> Dict[str, Any]:
    cfg = load_config()
    ai = cfg.get("ai", {}) or {}
    q = ai.get("ollama_qwen", {}) or {}
    selection_mode = str(ai.get("selection_mode", "ai_only" if ai.get("ai_only_no_fallback", False) else "hybrid") or "hybrid").lower()
    return {
        "enabled": bool(q.get("enabled", False)),
        "provider": str(ai.get("provider", q.get("provider", "qwen_prompt_only_strict")) or "qwen_prompt_only_strict"),
        "selection_mode": selection_mode,
        "base_url": str(q.get("base_url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434").rstrip("/"),
        "model": str(q.get("model", "qwen2.5-coder:1.5b") or "qwen2.5-coder:1.5b"),
        "timeout_seconds": float(q.get("timeout_seconds", 120) or 120),
        "max_verilog_chars": int(q.get("max_verilog_chars", 2400) or 2400),
        "max_qsf_chars": int(q.get("max_qsf_chars", 1200) or 1200),
        "num_predict": int(q.get("num_predict", 220) or 220),
        "num_ctx": int(q.get("num_ctx", 4096) or 4096),
        "keep_alive": q.get("keep_alive", "30m"),
        "temperature": float(q.get("temperature", 0.0) or 0.0),
        "seed": int(q.get("seed", 42) or 42),
        "structured_output": bool(q.get("structured_output", True)),
        "compact_context": bool(q.get("compact_context", True)),
        "preload_on_startup": bool(q.get("preload_on_startup", True)),
        "minimum_confidence_percent": int(q.get("minimum_confidence_percent", 85) or 85),
        "prompt_file": str(q.get("prompt_file", "ollama_fpga_classifier_prompt.txt") or "ollama_fpga_classifier_prompt.txt"),
        "prompt_only_strict": bool(q.get("prompt_only_strict", True)),
        "validate_evidence_exact_tokens": bool(q.get("validate_evidence_exact_tokens", True)),
        "extractor_binary": str(q.get("extractor_binary", "fpga_signal_extractor") or "fpga_signal_extractor"),
        "auto_build_extractor": bool(q.get("auto_build_extractor", True)),
        "retry_invalid_output_once": bool(q.get("retry_invalid_output_once", True)),
        "ai_only_no_fallback": selection_mode == "ai_only",
    }

def ollama_qwen_enabled_for(local_classification: Dict[str, Any]) -> bool:
    q = ollama_qwen_config()
    if not q.get("enabled"):
        return False
    provider = str(q.get("provider") or "").lower()
    if provider not in ("ollama", "ollama_qwen", "qwen", "hybrid_qwen", "hybrid_ollama", "hybrid", "qwen_prompt_only", "prompt_only_qwen", "qwen_prompt_only_strict"):
        return False
    if q.get("use_for_ambiguous_only"):
        return str(local_classification.get("target_board") or "") not in ("DE1-SoC", "DE10-Agilex")
    return True


def extract_json_object_from_text(text: str) -> Dict[str, Any]:
    """Parse Qwen JSON and salvage required scalar fields if output was truncated."""
    raw = str(text or "").strip()
    if not raw:
        return {}

    raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass

    # Qwen can occasionally hit its output-token limit after already emitting
    # the four fields required for a safe decision. Salvage only those explicit
    # scalar values. This does not select a board; it preserves Qwen's selection.
    salvaged: Dict[str, Any] = {}
    patterns = {
        "target_board": r'"target_board"\s*:\s*"([^"]+)"',
        "confidence_percent": r'"confidence_percent"\s*:\s*(\d{1,3})',
        "confidence_score": r'"confidence_score"\s*:\s*(\d{1,3})',
        "safe_to_program": r'"safe_to_program"\s*:\s*(true|false)',
        "decision_type": r'"decision_type"\s*:\s*"([^"]+)"',
        "reason": r'"reason"\s*:\s*"([^"]{0,240})"',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, raw, flags=re.I)
        if not match:
            continue
        value: Any = match.group(1)
        if key in ("confidence_percent", "confidence_score"):
            value = int(value)
        elif key == "safe_to_program":
            value = str(value).lower() == "true"
        salvaged[key] = value

    if salvaged:
        salvaged["_salvaged_from_truncated_json"] = True
    return salvaged


def normalize_ollama_board_name(value: str) -> str:
    v = str(value or "").strip().lower().replace("_", "-").replace(" ", "")
    if not v:
        return ""
    if v in ("de1-soc", "de1soc", "de1", "cyclonev", "cyclone-v"):
        return "DE1-SoC"
    if v in ("de10-agilex", "de10agilex", "de10", "agilex", "agilex7", "agilex-7"):
        return "DE10-Agilex"
    if "conflict" in v or "unsafe" in v:
        return "Conflict - unsafe to program"
    if "unknown" in v or "ambiguous" in v or "manual" in v or v == "none":
        return "Ambiguous - manual selection required"
    return ""


def load_ollama_qwen_prompt_template() -> Tuple[str, str]:
    """Load the editable AI prompt used by Qwen.

    v4.35: FPGA_HARDWARE_CLASSIFIER_PROMPT_v3_84.txt is now the real prompt
    sent to Ollama/Qwen.  The Python/C classifier files are evidence scanners;
    the prompt file is what tells Qwen how to reason over that evidence.
    """
    q = ollama_qwen_config()
    prompt_name = str(q.get("prompt_file") or "FPGA_HARDWARE_CLASSIFIER_PROMPT_v3_84.txt")
    p = BASE_DIR / prompt_name
    if not p.exists():
        # Fallback keeps the system alive if the prompt file was accidentally removed.
        fallback = """You are an FPGA board classifier. Return JSON only with target_board, confidence_percent, decision_type, safe_to_program, and reason. Do not repeat evidence. Choose DE1-SoC, DE10-Agilex, Conflict - unsafe to program, or Ambiguous - manual selection required.\n\nFilename:\n{filename}\n\nDeterministic scanner context JSON:\n{local_classifier_json}\n\nOptional QSF text:\n{qsf_text}\n\nVerilog/SystemVerilog code:\n{verilog_code}"""
        return fallback, str(p)
    return p.read_text(encoding="utf-8", errors="replace"), str(p)


def render_prompt_template(template: str, values: Dict[str, str]) -> str:
    """Safely render prompt placeholders without breaking on JSON braces."""
    out = str(template or "")
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def qwen_prompt_only_mode() -> bool:
    q = ollama_qwen_config()
    provider = str(q.get("provider") or "").lower()
    return bool(q.get("prompt_only_strict")) or provider in ("qwen_prompt_only", "prompt_only_qwen", "qwen_prompt_only_strict")



def fast_local_bypass_for_exclusive_tokens() -> bool:
    """Allow obvious board-specific designs to skip Qwen and save seconds.

    Qwen is still used for ambiguous/unknown/conflicting files. This keeps the
    strict prompt behavior where it matters, but avoids spending model time on
    simple designs like LEDR/HEX/PCIE where the local scanner is already
    decisive.
    """
    try:
        return bool((load_config().get("ai", {}) or {}).get("fast_local_bypass_for_exclusive_tokens", False))
    except Exception:
        return False


def local_classifier_is_strong_safe(local: Dict[str, Any]) -> bool:
    if not isinstance(local, dict):
        return False
    board = str(local.get("target_board") or "")
    if board not in ("DE1-SoC", "DE10-Agilex"):
        return False
    if local.get("safe_to_program") is False:
        return False
    decision = str(local.get("decision_type") or "").strip()
    allowed = set(str(x) for x in ((load_config().get("ai", {}) or {}).get("fast_local_bypass_decisions", []) or []))
    if not allowed:
        allowed = {"exclusive_token_or_qsf_rule", "exclusive_token_or_strong_width_rule", "c_exclusive_token_or_qsf_rule", "c_exclusive_token_or_strong_width_rule"}
    return decision in allowed


def normalize_token_for_prompt_validation(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "", str(token or "")).upper()


def strip_verilog_comments_and_strings_for_prompt_validation(text: str) -> str:
    """Remove Verilog/SystemVerilog comments and strings for the exact-token truth gate.

    Qwen is instructed to ignore comments/strings. This helper makes the backend
    validation follow the same safety rule, so a token that only appears inside a
    comment or string cannot be used as proof for automatic programming.
    """
    s = str(text or "")
    # Remove block comments first, then line comments.
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"//.*", " ", s)
    # Remove double-quoted string literals, respecting simple escapes.
    s = re.sub(r'"(?:\\.|[^"\\])*"', ' ', s)
    return s


def _normalized_extracted_evidence_tokens(extracted_evidence: Optional[Dict[str, Any]]) -> List[str]:
    """Return only literal values emitted by the signal extractor.

    This is an anti-hallucination allow-list. It does not map a token to a board
    and therefore cannot make the board-family decision.
    """
    data = extracted_evidence if isinstance(extracted_evidence, dict) else {}
    values: List[str] = []

    for item in data.get("verilog_ports") or []:
        if isinstance(item, dict):
            values.append(str(item.get("name") or ""))
    values.extend(str(x or "") for x in (data.get("verilog_signals") or []))
    values.extend(str(x or "") for x in (data.get("qsf_targets") or []))

    metadata = {
        "QSF_FAMILY": str(data.get("qsf_family") or ""),
        "QSF_DEVICE": str(data.get("qsf_device") or ""),
        "QSF_BOARD": str(data.get("qsf_board") or ""),
    }
    for label, value in metadata.items():
        if value:
            values.append(value)
            values.append(f"{label}:{value}")

    out: List[str] = []
    seen = set()
    for value in values:
        norm = normalize_token_for_prompt_validation(value)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def qwen_exact_evidence_present(
    result: Dict[str, Any],
    verilog_code: str,
    qsf_text: str = "",
    extracted_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verify that Qwen cited evidence actually emitted by the extractor.

    Qwen remains the only board selector. This function only rejects invented
    evidence. The previous implementation compared labels such as
    ``QSF_DEVICE:5C...`` against raw QSF text and could falsely reject a correct
    result because the raw file does not contain the synthetic ``QSF_DEVICE:``
    prefix.
    """
    board = str(result.get("target_board") or "")
    if board not in ("DE1-SoC", "DE10-Agilex", "Conflict - unsafe to program"):
        return ok(valid=True, reason="manual/ambiguous board does not require positive evidence")

    verilog_clean = strip_verilog_comments_and_strings_for_prompt_validation(verilog_code or "")
    raw_norm = normalize_token_for_prompt_validation(f"{verilog_clean}\n{qsf_text or ''}")
    allowed = set(_normalized_extracted_evidence_tokens(extracted_evidence))

    evidence: List[Dict[str, Any]] = []
    for key in ("core_evidence", "qsf_evidence", "conflict_evidence"):
        items = result.get(key)
        if isinstance(items, list):
            evidence.extend([x for x in items if isinstance(x, dict)])
    if not evidence:
        # v5.2: core evidence is authoritative output from the C extractor and is
        # intentionally ephemeral. Qwen selects the board only; it is not required
        # to repeat the evidence in its response. The extractor output is already
        # available in memory for this request and is discarded after classification.
        if allowed:
            return ok(
                valid=True,
                evidence_source="c_extractor_ephemeral",
                evidence_object_count=0,
                extracted_token_count=len(allowed),
            )
        return fail(
            "Qwen result rejected: the C extractor produced no usable evidence",
            valid=False,
        )

    missing: List[str] = []
    present: List[str] = []
    for ev in evidence:
        token = str(ev.get("exact_text") or ev.get("token_path") or ev.get("token") or ev.get("name") or "").strip()
        if not token:
            continue

        candidates = [token]
        if ":" in token:
            candidates.append(token.split(":", 1)[1].strip())

        matched = False
        for candidate in candidates:
            norm = normalize_token_for_prompt_validation(candidate)
            if not norm:
                continue
            if norm in allowed or norm in raw_norm:
                matched = True
                break

        if matched:
            present.append(str(ev.get("token") or token))
        else:
            missing.append(str(ev.get("token") or token))

    if missing and not present:
        return fail(
            "Qwen prompt-only result rejected: evidence tokens were not found in extracted Verilog/QSF evidence",
            valid=False,
            missing_evidence=missing[:20],
        )
    return ok(valid=True, present_evidence=present[:20], missing_evidence=missing[:20])

def _compact_unique(items: List[str], limit: int = 120) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        value = re.sub(r"\s+", " ", str(item or "")).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= max(1, int(limit)):
            break
    return out


EXTRACTOR_BINARY_PATH = BASE_DIR / "fpga_signal_extractor"
EXTRACTOR_SOURCE_PATH = BASE_DIR / "fpga_signal_extractor.c"
EXTRACTOR_BUILD_SCRIPT_PATH = BASE_DIR / "BUILD_SIGNAL_EXTRACTOR.sh"
_SIGNAL_EXTRACTOR_BUILD_LOCK = threading.Lock()
_SIGNAL_EXTRACTOR_BUILD_STATUS: Dict[str, Any] = {"attempted": False, "ready": False}

DE1_EXACT_SIGNAL_HINTS = {
    "HEX0", "HEX1", "HEX2", "HEX3", "HEX4", "HEX5",
    "LEDR", "KEY", "SW",
    "CLOCK_50", "CLOCK2_50", "CLOCK3_50", "CLOCK4_50",
    "GPIO_0", "GPIO_1",
    "VGA_R", "VGA_G", "VGA_B", "VGA_HS", "VGA_VS", "VGA_CLK", "VGA_SYNC_N", "VGA_BLANK_N",
    "AUD_ADCDAT", "AUD_DACDAT", "AUD_BCLK", "AUD_XCK", "AUD_ADCLRCK", "AUD_DACLRCK",
    "TD_DATA", "TD_CLK27", "TD_HS", "TD_VS", "TD_RESET_N",
    "PS2_CLK", "PS2_DAT", "PS2_CLK2", "PS2_DAT2",
    "IRDA_RXD", "IRDA_TXD",
    "ADC_CS_N", "ADC_DIN", "ADC_DOUT", "ADC_SCLK",
}
DE1_PREFIX_HINTS = ("HPS_", "DRAM_")
DE10_EXACT_SIGNAL_HINTS = {
    "BUTTON0", "BUTTON1", "CPU_RESET_N",
    "SW0", "SW1",
    "LED0", "LED1", "LED2", "LED3",
    "LED_BRACKET0", "LED_BRACKET1", "LED_BRACKET2", "LED_BRACKET3",
    "GPIO_P0", "GPIO_P1", "GPIO_P2", "GPIO_P3",
    "GPIO_CLK0", "GPIO_CLK1",
    "CLK_50_B2C", "CLK_50_B3A", "CLK_50_B3C", "CLK_100_B2A_P",
    "UFL_CLKIN_P", "UFL_CLKIN_N",
    "SI5340A_I2C_SCL", "SI5340A_I2C_SDA", "SI5340A_LOL", "SI5340A_LOS_XAXB",
    "INFO_SPI_SCLK", "INFO_SPI_MOSI", "INFO_SPI_MISO", "INFO_SPI_CS_N",
}
DE10_PREFIX_HINTS = ("PCIE_", "QSFPDDA_", "QSFPDDB_", "DDRA_", "DDRB_", "DDRC_", "DDRD_", "SI5340A_", "INFO_SPI_", "M10_SYS_")
_RESERVED_PORT_WORDS = {
    "INPUT", "OUTPUT", "INOUT", "WIRE", "REG", "LOGIC", "SIGNED", "UNSIGNED", "MODULE",
    "BEGIN", "END", "IF", "ELSE", "CASE", "ENDCASE", "ALWAYS", "ASSIGN", "PARAMETER",
    "LOCALPARAM", "GENERATE", "ENDGENERATE", "FOR", "WHILE", "OR", "AND", "NOT",
}


def _normalize_signal_name(value: str) -> str:
    name = str(value or "").strip().strip(",;")
    if not name:
        return ""
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1].strip()
    name = name.strip('"').strip("'")
    if not name:
        return ""
    name = name.split("=")[0].strip()
    name = name.split(".")[-1].strip()
    name = re.sub(r"\\$", "", name)
    name = re.sub(r"\[[^\]]+\]", "", name)
    name = re.sub(r"\([^)]*\)$", "", name)
    name = name.strip()
    if not name:
        return ""
    return name.upper()


def _width_from_range_token(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]", text)
    if not m:
        return None
    try:
        a = int(m.group(1))
        b = int(m.group(2))
        return abs(a - b) + 1
    except Exception:
        return None


def _classify_exclusive_hit(token: str) -> Optional[str]:
    t = _normalize_signal_name(token)
    if not t:
        return None
    if t in DE1_EXACT_SIGNAL_HINTS or any(t.startswith(prefix) for prefix in DE1_PREFIX_HINTS):
        return "DE1-SoC"
    if t in DE10_EXACT_SIGNAL_HINTS or any(t.startswith(prefix) for prefix in DE10_PREFIX_HINTS):
        return "DE10-Agilex"
    return None


def _extract_verilog_ports_python(verilog_code: str) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, int]]:
    clean = strip_verilog_comments_and_strings_for_prompt_validation(verilog_code or "")
    ports: List[Dict[str, Any]] = []
    seen = set()
    widths: Dict[str, int] = {}
    patterns = [
        r"(?im)^\s*(input|output|inout)\b([^;\n]*)",
        r"(?is)(?:\(|,)\s*(input|output|inout)\b([^;\)]{0,300})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, clean):
            direction = str(match.group(1) or "").lower()
            body = str(match.group(2) or "")
            width = _width_from_range_token(body)
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?", body):
                norm = _normalize_signal_name(token)
                if not norm or norm in _RESERVED_PORT_WORDS:
                    continue
                if norm.startswith(("NEGEDGE", "POSEDGE")):
                    continue
                key = (direction, norm, width or 1)
                if key in seen:
                    continue
                seen.add(key)
                ports.append({"name": norm, "dir": direction, "width": int(width or 1)})
                widths[norm] = max(widths.get(norm, 0), int(width or 1))
                if len(ports) >= 64:
                    break
            if len(ports) >= 64:
                break
        if len(ports) >= 64:
            break
    port_names = _compact_unique([p["name"] for p in ports], 96)
    return ports[:64], port_names, widths


def _extract_qsf_evidence_python(qsf_text: str) -> Dict[str, Any]:
    family = ""
    device = ""
    board = ""
    targets: List[str] = []
    for raw_line in str(qsf_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if not family and "FAMILY" in upper:
            m = re.search(r"FAMILY\s+\"([^\"]+)\"", line, flags=re.I)
            if not m:
                m = re.search(r"FAMILY\s+(\S+)", line, flags=re.I)
            family = (m.group(1) if m else line.split()[-1]).strip().strip('"')
        if not device and re.search(r"\bDEVICE\b", upper):
            m = re.search(r"\bDEVICE\s+(\S+)", line, flags=re.I)
            if m:
                device = m.group(1).strip().strip('"')
        if not board and re.search(r"\bBOARD\b", upper):
            m = re.search(r"\bBOARD\s+\"([^\"]+)\"", line, flags=re.I)
            if not m:
                m = re.search(r"\bBOARD\s+(\S+)", line, flags=re.I)
            if m:
                board = m.group(1).strip().strip('"')
        m = re.search(r"(?:^|\s)-to\s+(\{[^}]+\}|\"[^\"]+\"|\S+)", line, flags=re.I)
        if m:
            raw_target = m.group(1).strip()
            if raw_target.startswith("{") and raw_target.endswith("}"):
                raw_items = [item for item in re.split(r"[\s,]+", raw_target[1:-1]) if item]
            else:
                raw_items = [raw_target]
            for item in raw_items:
                norm = _normalize_signal_name(item)
                if norm:
                    targets.append(norm)
    return {
        "family": family,
        "device": device,
        "board": board,
        "targets": _compact_unique(targets, 128),
    }


def _build_extracted_evidence(verilog_code: str, qsf_text: str = "") -> Dict[str, Any]:
    """Python evidence extractor used only when the C binary is unavailable.

    This function intentionally does not score, label, or choose a board. It only
    returns normalized Verilog ports and QSF metadata for Qwen.
    """
    ports, port_names, widths = _extract_verilog_ports_python(verilog_code)
    qsf = _extract_qsf_evidence_python(qsf_text)
    return {
        "extractor_engine": "python_signal_evidence_only",
        "verilog_ports": ports[:48],
        "verilog_signals": port_names[:64],
        "signal_widths": {k: int(v) for k, v in widths.items()},
        "qsf_family": str(qsf.get("family") or ""),
        "qsf_device": str(qsf.get("device") or ""),
        "qsf_board": str(qsf.get("board") or ""),
        "qsf_targets": list(qsf.get("targets", []))[:96],
    }

def _sanitize_extracted_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize extractor output without adding any board classification."""
    data = evidence if isinstance(evidence, dict) else {}
    ports = []
    for item in data.get("verilog_ports") or []:
        if not isinstance(item, dict):
            continue
        name = _normalize_signal_name(item.get("name", ""))
        direction = str(item.get("dir") or item.get("direction") or "").lower()
        if direction not in ("input", "output", "inout"):
            direction = "input"
        width = item.get("width", 1)
        try:
            width = max(1, int(width or 1))
        except Exception:
            width = 1
        if name:
            ports.append({"name": name, "dir": direction, "width": width})

    widths = {}
    for key, value in (data.get("signal_widths") or {}).items():
        name = _normalize_signal_name(key)
        try:
            width = max(1, int(value or 1))
        except Exception:
            width = 1
        if name:
            widths[name] = width

    qsf_targets = _compact_unique(
        [_normalize_signal_name(x) for x in (data.get("qsf_targets") or []) if _normalize_signal_name(x)],
        96,
    )
    verilog_signals = _compact_unique(
        [_normalize_signal_name(x) for x in (data.get("verilog_signals") or []) if _normalize_signal_name(x)],
        64,
    )
    return {
        "extractor_engine": str(data.get("extractor_engine") or "unknown"),
        "verilog_ports": ports[:48],
        "verilog_signals": verilog_signals,
        "signal_widths": widths,
        "qsf_family": str(data.get("qsf_family") or ""),
        "qsf_device": str(data.get("qsf_device") or ""),
        "qsf_board": str(data.get("qsf_board") or ""),
        "qsf_targets": qsf_targets,
    }

def ensure_signal_extractor_binary() -> Optional[Path]:
    global _SIGNAL_EXTRACTOR_BUILD_STATUS
    binary_path = EXTRACTOR_BINARY_PATH
    if binary_path.is_file() and os.access(binary_path, os.X_OK):
        _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": True, "path": str(binary_path)}
        return binary_path

    q = ollama_qwen_config()
    if not q.get("auto_build_extractor", True):
        _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": False, "reason": "auto_build_extractor disabled"}
        return None

    if not EXTRACTOR_SOURCE_PATH.is_file():
        _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": False, "reason": "extractor source missing"}
        return None

    gcc = shutil.which("gcc")
    if not gcc:
        _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": False, "reason": "gcc not installed"}
        return None

    with _SIGNAL_EXTRACTOR_BUILD_LOCK:
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": True, "path": str(binary_path)}
            return binary_path
        try:
            subprocess.run(
                [
                    gcc, "-O2", "-std=c99", "-Wall", "-Wextra",
                    str(EXTRACTOR_SOURCE_PATH), "-o", str(binary_path),
                ],
                cwd=str(BASE_DIR),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            try:
                os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass
            _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": True, "path": str(binary_path)}
            return binary_path
        except Exception as e:
            _SIGNAL_EXTRACTOR_BUILD_STATUS = {"attempted": True, "ready": False, "reason": str(e)}
            return None


def extract_board_signal_evidence(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    extractor_started = time.monotonic()
    q = ollama_qwen_config()
    max_verilog_chars = int(q.get("max_verilog_chars", 2400) or 2400)
    max_qsf_chars = int(q.get("max_qsf_chars", 1200) or 1200)
    verilog_limited = str(verilog_code or "")[: max(512, max_verilog_chars * 8)]
    qsf_limited = str(qsf_text or "")[: max(256, max_qsf_chars * 8)]
    binary = ensure_signal_extractor_binary()
    if binary:
        temp_v = None
        temp_q = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".v", delete=False) as fv:
                fv.write(verilog_limited)
                temp_v = fv.name
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".qsf", delete=False) as fq:
                fq.write(qsf_limited)
                temp_q = fq.name
            completed = subprocess.run(
                [str(binary), temp_v, temp_q, filename or "uploaded.v"],
                cwd=str(BASE_DIR),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            data = extract_json_object_from_text(completed.stdout)
            if isinstance(data, dict) and data:
                data["extractor_engine"] = str(data.get("extractor_engine") or "c_keyword_extractor")
                data["extractor_stderr_tail"] = str(completed.stderr or "")[-400:]
                sanitized = _sanitize_extracted_evidence(data)
                sanitized["_extractor_wall_ms"] = round(
                    (time.monotonic() - extractor_started) * 1000.0, 3
                )
                return sanitized
        except Exception:
            pass
        finally:
            for path in (temp_v, temp_q):
                try:
                    if path and os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass
    sanitized = _sanitize_extracted_evidence(
        _build_extracted_evidence(verilog_limited, qsf_limited)
    )
    sanitized["_extractor_wall_ms"] = round(
        (time.monotonic() - extractor_started) * 1000.0, 3
    )
    return sanitized


def build_ollama_qwen_prompt(verilog_code: str, filename: str = "", qsf_text: str = "") -> Tuple[str, Dict[str, Any]]:
    """Build a stateless, manual-grounded prompt from current C evidence only.

    Exact QSF device/family/board identity is later enforced by
    ``fpga_classifier_policy.enforce_grounding``.  QSF-only template targets are
    intentionally omitted from the model prompt so unused board-template pins
    cannot bias the current classification.
    """
    evidence = extract_board_signal_evidence(verilog_code, qsf_text=qsf_text, filename=filename)

    if ollama_qwen_config().get("log_classifier_timing", True):
        print(
            "[AI EVIDENCE] "
            f"filename={filename!r} "
            f"extractor_ms={float(evidence.get('_extractor_wall_ms', 0.0) or 0.0):.3f} "
            f"qsf_chars={len(qsf_text or '')} "
            f"family={evidence.get('qsf_family')!r} "
            f"device={evidence.get('qsf_device')!r} "
            f"ports={len(evidence.get('verilog_ports') or [])} "
            f"targets={len(evidence.get('qsf_targets') or [])}",
            flush=True,
        )

    has_evidence = any([
        evidence.get("verilog_ports"),
        evidence.get("verilog_signals"),
        evidence.get("qsf_family"),
        evidence.get("qsf_device"),
        evidence.get("qsf_board"),
    ])
    if not has_evidence:
        raise ValueError("C extractor produced no usable evidence")

    profile_catalog = []
    try:
        profile_doc = json.loads((BASE_DIR / "board_profiles.json").read_text(encoding="utf-8"))
        for profile in profile_doc.get("profiles", []):
            if not isinstance(profile, dict) or not profile.get("enabled", True):
                continue
            profile_catalog.append({
                "display_name": profile.get("display_name"),
                "authoritative_devices": profile.get("authoritative_devices", []),
                "authoritative_families": profile.get("authoritative_families", []),
                "board_name_aliases": profile.get("board_name_aliases", []),
                "manual_verified_signals": profile.get("manual_verified_signals", {}),
            })
    except Exception:
        profile_catalog = []

    clean_evidence = {
        k: v for k, v in evidence.items()
        if not str(k).startswith("_")
    }
    rendered = build_grounded_fpga_prompt(clean_evidence, profile_catalog)
    return rendered.strip(), evidence


def ollama_qwen_output_schema() -> Dict[str, Any]:
    """Manual-grounded schema shared with the deterministic evidence guard."""
    return grounded_ollama_json_schema()


def _ollama_duration_ms(data: Dict[str, Any], key: str) -> float:
    try:
        return round(float(data.get(key, 0) or 0) / 1_000_000.0, 2)
    except Exception:
        return 0.0


def ollama_qwen_classify_board(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    """Ask Qwen to analyze current evidence, then apply the grounding guard.

    A single repair attempt is allowed for unresolved inputs. Recognized QSF
    device identity is enforced deterministically after inference, so a model
    hallucination cannot route a job to the wrong supported FPGA family.
    """
    q = ollama_qwen_config()
    if not q.get("enabled"):
        return {"success": False, "provider": "ollama_qwen", "error": "Ollama Qwen disabled"}

    prompt, extracted_evidence = build_ollama_qwen_prompt(
        verilog_code,
        filename=filename,
        qsf_text=qsf_text,
    )
    base_payload: Dict[str, Any] = {
        "model": q.get("model", "qwen2.5-coder:1.5b"),
        "prompt": prompt,
        "stream": False,
        "keep_alive": q.get("keep_alive", "30m"),
        "options": {
            "temperature": float(q.get("temperature", 0.0) or 0.0),
            "num_predict": min(240, max(120, int(q.get("num_predict", 220) or 220))),
            "num_ctx": int(q.get("num_ctx", 4096) or 4096),
            "seed": int(q.get("seed", 42) or 42),
            "top_k": 10,
            "top_p": 0.5,
        },
    }
    if q.get("structured_output", True):
        base_payload["format"] = ollama_qwen_output_schema()
    else:
        base_payload["format"] = "json"

    url = str(q.get("base_url", "http://127.0.0.1:11434")).rstrip("/") + "/api/generate"
    started = time.monotonic()

    def post(payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            req,
            timeout=float(q.get("timeout_seconds", 120) or 120),
        ) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")

    def post_compatible(payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return post(payload)
        except urllib.error.HTTPError as exc:
            # Older Ollama versions may accept JSON mode but not a schema object.
            # This remains the same Qwen-only inference path.
            if int(getattr(exc, "code", 0) or 0) == 400 and isinstance(payload.get("format"), dict):
                compatibility_payload = dict(payload)
                compatibility_payload["format"] = "json"
                return post(compatibility_payload)
            raise

    def log_qwen_decision(result: Dict[str, Any], label: str) -> None:
        if not q.get("log_classifier_decision", True):
            return
        perf = result.get("performance") if isinstance(result.get("performance"), dict) else {}
        print(
            f"[QWEN DECISION] attempt={label} "
            f"target={result.get('target_board')!r} "
            f"confidence={int(result.get('confidence_percent', 0) or 0)} "
            f"safe={bool(result.get('safe_to_program'))} "
            f"decision={result.get('decision_type')!r} "
            f"extractor_ms={float(perf.get('extractor_wall_ms', 0.0) or 0.0):.3f} "
            f"ai_wall_ms={float(perf.get('wall_ms', 0.0) or 0.0):.2f} "
            f"ollama_total_ms={float(perf.get('total_ms', 0.0) or 0.0):.2f} "
            f"load_ms={float(perf.get('load_ms', 0.0) or 0.0):.2f} "
            f"prompt_eval_ms={float(perf.get('prompt_eval_ms', 0.0) or 0.0):.2f} "
            f"generation_ms={float(perf.get('eval_ms', 0.0) or 0.0):.2f} "
            f"prompt_tokens={int(perf.get('prompt_tokens', 0) or 0)} "
            f"output_tokens={int(perf.get('output_tokens', 0) or 0)} "
            f"reason={str(result.get('reason') or '')[:160]!r}",
            flush=True,
        )

    def parse_safe_boolean(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    def build_result(data: Dict[str, Any], prompt_used: str, attempt: int) -> Dict[str, Any]:
        raw = str(data.get("response") or "").strip()
        parsed = extract_json_object_from_text(raw)
        raw_ai_target = str(parsed.get("target_board", "") or "").strip()

        # Manual-grounded guard: exact recognized QSF identity is authoritative,
        # and invented observed fields/signals are rejected for unresolved inputs.
        grounded = enforce_fpga_grounding(parsed, extracted_evidence or {})
        board = normalize_ollama_board_name(grounded.get("target_board", ""))

        try:
            confidence = int(
                grounded.get(
                    "confidence_percent",
                    grounded.get("confidence_score", grounded.get("confidence", 0)),
                )
                or 0
            )
        except Exception:
            confidence = 0
        confidence = max(0, min(100, confidence))

        evidence = parsed.get("core_evidence") if isinstance(parsed.get("core_evidence"), list) else []
        result: Dict[str, Any] = {
            "success": bool(board),
            "provider": "ollama_qwen_grounded_guard",
            "model": q.get("model"),
            "base_url": q.get("base_url"),
            "prompt_file": q.get("prompt_file"),
            "prompt_only_strict": qwen_prompt_only_mode(),
            "target_board": board or "Ambiguous - manual selection required",
            "confidence_percent": confidence,
            "confidence_score": confidence,
            "decision_type": grounded.get("decision_type") or ("conflict" if "Conflict" in board else "ambiguous"),
            "safe_to_program": parse_safe_boolean(grounded.get("safe_to_program", False)),
            "reason_code": str(grounded.get("reason_code") or ""),
            "grounding_guard_applied": bool(grounded.get("guard_applied")),
            "raw_ai_target": raw_ai_target,
            "raw_ai_target_changed_by_guard": bool(
                raw_ai_target and raw_ai_target != str(grounded.get("target_board") or "")
            ),
            # v5.2 privacy/storage policy: do not retain extractor evidence,
            # prompt JSON, AI raw output, or source-file contents in job state.
            "core_evidence": [],
            "weak_evidence": [],
            "classifier_engine": "qwen_manual_grounded_with_authoritative_qsf_guard",
            "evidence_storage": "memory_only_discarded_after_classification",
            "evidence_counts": {
                "verilog_ports": len((extracted_evidence or {}).get("verilog_ports") or []),
                "verilog_signals": len((extracted_evidence or {}).get("verilog_signals") or []),
                "qsf_targets": len((extracted_evidence or {}).get("qsf_targets") or []),
            },
            "detected_ports": [],
            "extracted_ports": [],
            "extracted_widths": [],
            "qsf_evidence": [],
            "conflict_evidence": [],
            "conflicts": [],
            "recommended_action": grounded.get("reason", ""),
            "reason": str(grounded.get("reason") or "Grounded FPGA board decision completed")[:240],
            "attempt": attempt,
            "performance": {
                "wall_ms": round((time.monotonic() - started) * 1000.0, 2),
                "total_ms": _ollama_duration_ms(data, "total_duration"),
                "load_ms": _ollama_duration_ms(data, "load_duration"),
                "prompt_eval_ms": _ollama_duration_ms(data, "prompt_eval_duration"),
                "eval_ms": _ollama_duration_ms(data, "eval_duration"),
                "prompt_tokens": int(data.get("prompt_eval_count", 0) or 0),
                "output_tokens": int(data.get("eval_count", 0) or 0),
                "prompt_chars": len(prompt_used),
                "context_limit": int(q.get("num_ctx", 4096) or 4096),
                "extractor_engine": str((extracted_evidence or {}).get("extractor_engine") or ""),
                "extractor_wall_ms": float(
                    (extracted_evidence or {}).get("_extractor_wall_ms", 0.0) or 0.0
                ),
                "verilog_port_count": len((extracted_evidence or {}).get("verilog_ports") or []),
                "qsf_target_count": len((extracted_evidence or {}).get("qsf_targets") or []),
            },
        }

        # Enforce semantic consistency without choosing the board locally.
        # A valid board selected at high confidence and explicitly marked safe
        # cannot simultaneously be labeled a conflict unless Qwen cites the
        # other board in core evidence.
        selected_board = str(result.get("target_board") or "").strip()
        allowed_target_boards = {"DE1-SoC", "DE10-Agilex", "Ambiguous", "Ambiguous - manual selection required"}
        if selected_board not in allowed_target_boards:
            result["success"] = False
            result["safe_to_program"] = False
            result["decision_type"] = "ambiguous"
            result["reason"] = "AI returned an invalid target_board value"
            result["error"] = result["reason"]
        selected_confidence = int(result.get("confidence_percent", 0) or 0)
        decision_type = str(result.get("decision_type") or "").strip().lower()
        cited_boards = {
            str(item.get("board") or "").strip()
            for item in evidence
            if isinstance(item, dict) and str(item.get("board") or "").strip()
        }
        opposite_board = "DE10-Agilex" if selected_board == "DE1-SoC" else "DE1-SoC"
        actual_cross_board_evidence = opposite_board in cited_boards

        if (
            selected_board in ("DE1-SoC", "DE10-Agilex")
            and selected_confidence >= 90
            and result.get("safe_to_program") is True
            and decision_type == "conflict"
            and not actual_cross_board_evidence
        ):
            result["decision_type_original"] = result.get("decision_type")
            result["decision_type"] = "match"
            result["consistency_normalized"] = True
            result["consistency_reason"] = (
                "Qwen selected one board with >=90% confidence and safe_to_program=true "
                "without citing evidence for the opposite board."
            )

        if parsed.get("_salvaged_from_truncated_json"):
            result["json_salvaged_from_truncated_output"] = True

        if qwen_prompt_only_mode() and q.get("validate_evidence_exact_tokens", True):
            gate = qwen_exact_evidence_present(
                result,
                verilog_code,
                qsf_text=qsf_text,
                extracted_evidence=extracted_evidence,
            )
            result["exact_evidence_validation"] = gate
            if not gate.get("success"):
                result["success"] = False
                result["target_board"] = "Ambiguous - manual selection required"
                result["confidence_percent"] = 0
                result["confidence_score"] = 0
                result["safe_to_program"] = False
                result["reason"] = gate.get("error", "Qwen evidence validation failed")
        return result

    try:
        minimum_confidence = int(q.get("minimum_confidence_percent", 85) or 85)
        with OLLAMA_INFERENCE_LOCK:
            first_data = post_compatible(base_payload)
            first_result = build_result(first_data, prompt, 1)
            log_qwen_decision(first_result, "1")

            first_board = str(first_result.get("target_board") or "")
            first_decision = str(first_result.get("decision_type") or "").strip().lower()
            retry_needed = (
                not first_result.get("success")
                or (
                    first_board in ("DE1-SoC", "DE10-Agilex")
                    and (
                        not first_result.get("safe_to_program")
                        or int(first_result.get("confidence_percent", 0) or 0) < minimum_confidence
                        or first_decision == "conflict"
                    )
                )
            )

            if retry_needed and q.get("retry_invalid_output_once", True):
                repair_prompt = (
                    prompt
                    + "\n\nREPAIR PASS: Re-evaluate the same evidence once. "
                    + "The previous response was missing, malformed, below the safety threshold, "
                    + "or was internally inconsistent. Do not guess. Re-read CURRENT_INPUT_JSON. "
                    + "Copy observed_qsf_device, observed_qsf_family, and observed_qsf_board exactly, "
                    + "and cite only exact current verilog_signals. Return only the required JSON object. "
                    + "A recognized exact qsf_device has priority over every signal name."
                )
                retry_payload = dict(base_payload)
                retry_payload["prompt"] = repair_prompt
                retry_data = post_compatible(retry_payload)
                retry_result = build_result(retry_data, repair_prompt, 2)
                log_qwen_decision(retry_result, "2")
                retry_result["retry_used"] = True
                retry_result["first_attempt"] = {
                    "target_board": first_result.get("target_board"),
                    "confidence_percent": first_result.get("confidence_percent"),
                    "safe_to_program": first_result.get("safe_to_program"),
                    "reason": first_result.get("reason"),
                    "validation_passed": bool((first_result.get("exact_evidence_validation") or {}).get("success")),
                }

                retry_board = str(retry_result.get("target_board") or "")
                retry_is_safe = bool(
                    retry_result.get("success")
                    and retry_board in ("DE1-SoC", "DE10-Agilex")
                    and retry_result.get("safe_to_program")
                    and int(retry_result.get("confidence_percent", 0) or 0) >= minimum_confidence
                )
                if retry_is_safe or not first_result.get("success"):
                    return retry_result

                first_result["retry_used"] = True
                first_result["retry_attempt"] = {
                    "target_board": retry_result.get("target_board"),
                    "confidence_percent": retry_result.get("confidence_percent"),
                    "safe_to_program": retry_result.get("safe_to_program"),
                    "reason": retry_result.get("reason"),
                    "exact_evidence_validation": retry_result.get("exact_evidence_validation"),
                }

            return first_result
    except urllib.error.URLError as exc:
        return {
            "success": False,
            "provider": "ollama_qwen",
            "model": q.get("model"),
            "error": f"Ollama connection failed: {exc}",
            "target_board": "Ambiguous - manual selection required",
            "performance": {
                "wall_ms": round((time.monotonic() - started) * 1000.0, 2),
                "prompt_chars": len(prompt),
                "extractor_engine": str((extracted_evidence or {}).get("extractor_engine") or ""),
            },
            "extracted_evidence": extracted_evidence,
        }
    except Exception as exc:
        return {
            "success": False,
            "provider": "ollama_qwen",
            "model": q.get("model"),
            "error": str(exc),
            "target_board": "Ambiguous - manual selection required",
            "performance": {
                "wall_ms": round((time.monotonic() - started) * 1000.0, 2),
                "prompt_chars": len(prompt),
                "extractor_engine": str((extracted_evidence or {}).get("extractor_engine") or ""),
            },
            "extracted_evidence": extracted_evidence,
        }

def preload_ollama_qwen_model() -> Dict[str, Any]:
    """Load Qwen before the first student job so cold start is not charged to it."""
    global OLLAMA_PRELOAD_STATUS
    q = ollama_qwen_config()
    status: Dict[str, Any] = {
        "attempted": True,
        "success": False,
        "model": q.get("model"),
        "started_at": now_iso() if "now_iso" in globals() else "",
    }
    if not q.get("enabled") or not q.get("preload_on_startup", True):
        status.update({"skipped": True, "reason": "disabled by configuration"})
        OLLAMA_PRELOAD_STATUS = status
        return status

    url = str(q.get("base_url", "http://127.0.0.1:11434")).rstrip("/") + "/api/generate"
    payload = {
        "model": q.get("model", "qwen2.5-coder:1.5b"),
        "prompt": "",
        "stream": False,
        "keep_alive": q.get("keep_alive", "30m"),
    }
    started = time.monotonic()
    try:
        with OLLAMA_INFERENCE_LOCK:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(
                req,
                timeout=min(120.0, float(q.get("timeout_seconds", 120) or 120)),
            ) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        status.update({
            "success": True,
            "wall_ms": round((time.monotonic() - started) * 1000.0, 2),
            "load_ms": _ollama_duration_ms(data, "load_duration"),
        })
    except Exception as e:
        status.update({
            "error": str(e),
            "wall_ms": round((time.monotonic() - started) * 1000.0, 2),
        })
    OLLAMA_PRELOAD_STATUS = status
    return status



def choose_prompt_only_qwen_classifier(verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    """Strict AI-only selector path using extracted board-facing evidence."""
    qwen = ollama_qwen_classify_board(verilog_code, qsf_text=qsf_text, filename=filename)
    if qwen.get("success"):
        qwen["ollama_qwen_used"] = True
        qwen["local_classifier_skipped"] = True
        qwen["provider"] = "qwen_prompt_only_grounded_guard"
    return qwen


def choose_classifier_with_ollama_qwen(local_classification: Dict[str, Any], verilog_code: str, qsf_text: str = "", filename: str = "") -> Dict[str, Any]:
    """Hybrid classifier: deterministic safety guard plus optional Ollama Qwen.

    The deterministic conflict guard always wins. Qwen can provide the old LLM-style
    board selection for ambiguous or non-exclusive designs.
    """
    local = dict(local_classification or {})
    local["provider"] = local.get("provider") or "local_deterministic"

    if local.get("target_board") == "Conflict - unsafe to program":
        local["ollama_qwen_skipped"] = "deterministic_conflict_guard"
        return local
    if not ollama_qwen_enabled_for(local):
        return local

    q = ollama_qwen_config()
    qwen = ollama_qwen_classify_board(verilog_code, qsf_text=qsf_text, filename=filename)
    local["ollama_qwen"] = qwen
    if not qwen.get("success"):
        local["ollama_qwen_error"] = qwen.get("error", "Ollama Qwen did not return a usable board")
        return local if q.get("fallback_to_local_classifier") else qwen

    q_board = qwen.get("target_board", "")
    q_conf = int(qwen.get("confidence_percent", 0) or 0)
    min_conf = int(q.get("minimum_confidence_percent", 85) or 85)
    local_board = local.get("target_board") if local.get("target_board") in ("DE1-SoC", "DE10-Agilex") else ""

    if q_board == "Conflict - unsafe to program":
        # Do not let a possible conflict pass automatically.
        qwen["deterministic_guard"] = local
        return qwen
    if q_board not in ("DE1-SoC", "DE10-Agilex") or q_conf < min_conf:
        local["ollama_qwen_not_used"] = f"Qwen confidence {q_conf}% below threshold {min_conf}% or manual result"
        return local

    if local_board and local_board != q_board:
        policy = str(q.get("disagreement_policy", "deterministic_wins") or "deterministic_wins").lower()
        if policy == "block":
            return {
                "target_board": "Conflict - unsafe to program",
                "decision_type": "local_vs_qwen_disagreement",
                "confidence_score": 0,
                "core_evidence": [
                    {"board": local_board, "token": "local_deterministic", "reason": "Local deterministic classifier selected this board", "confidence": 100},
                    {"board": q_board, "token": "ollama_qwen", "reason": "Ollama Qwen selected a different board", "confidence": q_conf},
                ],
                "reason": f"Safety block: local classifier selected {local_board}, but Ollama Qwen selected {q_board}.",
                "local_classifier": local,
                "ollama_qwen": qwen,
            }
        if policy == "ollama_wins":
            qwen["deterministic_guard"] = local
            qwen["warning"] = f"Ollama Qwen overrode local deterministic board {local_board}; use only for testing."
            return qwen
        local["ollama_qwen_warning"] = f"Qwen selected {q_board}, but deterministic classifier selected {local_board}; deterministic_wins policy kept local result."
        return local

    qwen["deterministic_guard"] = local
    qwen["target_board"] = q_board
    qwen["confidence_score"] = q_conf
    qwen["decision_type"] = qwen.get("decision_type") or "ollama_qwen"
    qwen["ollama_qwen_used"] = True
    return qwen

def select_board_ai(verilog_code: str, filename: str = "", requested_board: Optional[str] = None, force_refresh: bool = True, allow_job_id: str = "", qsf_text: str = "") -> Dict[str, Any]:
    """
    Select from live available physical JTAG instances, not from hardcoded board names.

    Balancing rule:
    - AI first chooses the best board family for the Verilog.
    - Between equal-scoring physical JTAG instances, choose the least-used cable.
    - If every cable has been used, choose the one with the lowest total programming time.
    """
    qwen_cfg = ollama_qwen_config()
    ai_only_no_fallback = bool(qwen_cfg.get("ai_only_no_fallback"))

    if ai_only_no_fallback:
        # AI-only fail-closed mode: Qwen must make the board-family decision.
        # The Python/C classifier and heuristic score_board path are never
        # allowed to choose a board when this mode is enabled.
        if not qwen_cfg.get("enabled") or not qwen_prompt_only_mode():
            return fail(
                "AI-only board selection is enabled, but Ollama/Qwen prompt-only mode is not available.",
                selected_board="None",
                confidence="none",
                safe_to_program=False,
                classifier={
                    "success": False,
                    "provider": "qwen_prompt_only_strict",
                    "error": "AI-only mode requires ai.provider=qwen_prompt_only_strict and ollama_qwen.enabled=true",
                },
            )

        classification = choose_prompt_only_qwen_classifier(
            verilog_code,
            qsf_text=qsf_text,
            filename=filename,
        )

        ai_board = str(classification.get("target_board") or "")
        try:
            ai_confidence = int(
                classification.get(
                    "confidence_percent",
                    classification.get("confidence_score", 0),
                )
                or 0
            )
        except Exception:
            ai_confidence = 0

        minimum_ai_confidence = int(
            qwen_cfg.get("minimum_confidence_percent", 85) or 85
        )

        if (
            not classification.get("success")
            or ai_board not in ("DE1-SoC", "DE10-Agilex")
            or ai_confidence < minimum_ai_confidence
            or classification.get("safe_to_program") is False
        ):
            error_text = (
                classification.get("error")
                or classification.get("reason")
                or "Qwen did not return a safe, confident board selection"
            )
            return fail(
                f"AI board classification failed: {error_text}",
                classifier=classification,
                selected_board="None",
                confidence="insufficient",
                safe_to_program=False,
                required_features=[],
                deterministic_board="",
                selection_mode="ai_only",
            )

        # Mark the successful result explicitly so the downstream selection
        # can only filter physical JTAG instances to the AI-selected family.
        classification["ollama_qwen_used"] = True
        classification["local_classifier_skipped"] = True
        classification["selection_mode"] = "ai_only"
        classification["provider"] = "qwen_prompt_only_grounded_guard"
    elif qwen_prompt_only_mode() and qwen_cfg.get("enabled"):
        # Qwen-first compatibility mode. A local fallback is possible only
        # when explicitly enabled in config.
        classification = choose_prompt_only_qwen_classifier(
            verilog_code,
            qsf_text=qsf_text,
            filename=filename,
        )
    else:
        local_classification = classify_fpga_board(
            verilog_code,
            qsf_text=qsf_text,
            filename=filename,
        )
        classification = choose_classifier_with_ollama_qwen(
            local_classification,
            verilog_code,
            qsf_text=qsf_text,
            filename=filename,
        )
    if classification.get("target_board") == "Conflict - unsafe to program":
        return fail(
            "Board classifier conflict: Verilog/QSF contains hardware-specific tokens for more than one board.",
            classifier=classification,
            selected_board="None",
            confidence="conflict",
            safe_to_program=False,
            required_features=classifier_features(classification),
        )

    # Safety rule for subset/simple I/O:
    # input [1:0] SW, KEY[1:0], BUTTON[1:0], LED[3:0], SW0/SW1, etc. are not
    # enough to auto-select DE10-Agilex because a DE1-SoC user may intentionally
    # expose only part of the DE1 switches/buttons/LEDs. If the user leaves board
    # selection on Auto, ask them to choose. If they explicitly choose DE1-SoC or
    # DE10-Agilex, allow the requested board filter to handle it.
    if (
        classification.get("decision_type") == "ambiguous_subset_width"
        and not bool(classification.get("ollama_qwen_used"))
        and not str(requested_board or "").strip()
    ):
        return fail(
            "Ambiguous subset/simple I/O: ports like SW[1:0], BUTTON[1:0], KEY[1:0], or LED[3:0] are not safe enough for Auto board selection. Choose DE1-SoC or DE10-Agilex manually, or provide a QSF.",
            classifier=classification,
            selected_board="None",
            confidence="ambiguous",
            safe_to_program=False,
            required_features=classifier_features(classification),
            deterministic_board="",
        )

    deterministic_board = classification.get("target_board") if classification.get("target_board") in ("DE1-SoC", "DE10-Agilex") else ""

    status = board_status(force_jtag=force_refresh, allow_job_id=allow_job_id)
    boards = status.get("boards", {})
    instances = status.get("board_instances", [])
    required = extract_features(verilog_code)
    deterministic_features = classifier_features(classification)
    for feat in deterministic_features:
        if feat not in required:
            required.append(feat)

    available_instances = [
        inst for inst in instances
        if inst.get("available") and inst.get("jtag_detected") and not inst.get("busy") and inst.get("board") in boards
    ]

    if deterministic_board:
        available_instances = [inst for inst in available_instances if inst.get("board") == deterministic_board]
        if requested_board and requested_board.strip() in boards and requested_board.strip() != deterministic_board:
            return fail(
                f"Requested board {requested_board} conflicts with deterministic hardware classifier target {deterministic_board}.",
                classifier=classification,
                required_features=required,
                selected_board="None",
                confidence="conflict",
                safe_to_program=False,
            )

    if requested_board:
        requested = requested_board.strip()
        available_instances = [
            inst for inst in available_instances
            if inst.get("board") == requested or inst.get("instance_id") == requested or inst.get("detected_cable") == requested
        ]
        if not available_instances:
            return fail(
                f"Requested board/instance is not available: {requested_board}",
                required_features=required,
                classifier=classification,
                deterministic_board=deterministic_board,
                available_boards=[b for b, st in boards.items() if st.get("available")],
                available_instances=status.get("board_instances", []),
            )

    if not available_instances:
        return fail(
            "No available JTAG board instance is detected and free.",
            required_features=required,
            classifier=classification,
            deterministic_board=deterministic_board,
            available_boards=[],
            available_instances=status.get("board_instances", []),
        )

    scored = []
    for inst in available_instances:
        board = inst.get("board", "")
        bcfg = boards.get(board, {})
        if deterministic_board and board == deterministic_board:
            score = 100
        else:
            score = score_board(required, bcfg.get("features", []), board)
        scored.append((score, board, inst, bcfg))

    score, board, inst, bcfg, usage = select_balanced_jtag_instance(scored)
    score_percent = 100 if deterministic_board else confidence_percent_from_score(score, required)
    if deterministic_board:
        conf = "high"
        safe = True
        ev_tokens = ", ".join(str(ev.get("token", "")) for ev in classification.get("core_evidence", [])[:8])
        provider = str(classification.get("provider") or classification.get("classifier_engine") or "")
        if classification.get("qwen_skipped_for_speed"):
            source_text = "Selected by local fallback because Qwen was unavailable or returned unusable JSON."
        elif "qwen" in provider.lower() or classification.get("ollama_qwen_used"):
            source_text = "Selected by Qwen strict prompt after backend evidence validation."
        else:
            source_text = "Selected by deterministic hardware classifier before AI scoring."
        reason = (
            f"{source_text} "
            f"Target={deterministic_board}, decision={classification.get('decision_type')}, evidence={ev_tokens}. "
            f"Balanced physical instance selected: {inst.get('instance_id')} | {inst.get('detected_cable')}."
        )
    else:
        conf = confidence_from_score(score, required)
        score_percent = confidence_percent_from_score(score, required)
        safe = confidence_ok(conf, score_percent)
        min_percent = ai_minimum_program_percent()
        fast_percent = ai_fast_decision_threshold_percent()
        fast_decision_passed = int(score_percent or 0) >= fast_percent
        reason = (
            "No 100% hardware signature found; selected by fast balanced physical JTAG rotation and feature scoring. "
            f"Board={board}, instance={inst.get('instance_id')}, cable={inst.get('detected_cable')}, "
            f"AI score={score}, confidence_percent={score_percent}, fast_threshold={fast_percent}, "
            f"program_threshold={min_percent}, fast_decision_passed={fast_decision_passed}, "
            f"previous_program_count={usage.get('program_count', 0)}, "
            f"previous_total_program_seconds={int(usage.get('total_program_seconds', 0) or 0)}."
        )
        if not safe:
            reason += " Confidence is below fast programming threshold; programming should be blocked unless forced."

    return ok(
        selected_board=board,
        selected_instance_id=inst.get("instance_id"),
        selected_jtag_cable=inst.get("detected_cable"),
        selected_lock_key=inst.get("lock_key"),
        selected_device_index=str(bcfg.get("jtag_device_index", "")),
        selected_quartus_family=str(bcfg.get("quartus_family", "standard")),
        target_lock_policy="v4.25_exact_target_with_warmup",
        confidence=conf,
        required_features=required,
        safe_to_program=safe,
        reason=reason,
        board_status=bcfg,
        board_instance=inst,
        jtag_usage_before=usage,
        classifier=classification,
        ai_provider=classification.get("provider", "local_deterministic"),
        ollama_qwen_used=bool(classification.get("ollama_qwen_used")),
        decision_type=classification.get("decision_type"),
        classifier_confidence_score=classification.get("confidence_score", 0),
        score_confidence_percent=score_percent,
        fast_confidence_threshold_percent=ai_fast_decision_threshold_percent(),
        minimum_confidence_percent_to_program=ai_minimum_program_percent(),
        deterministic_board=deterministic_board,
        balance_policy="least_program_count_then_lowest_total_program_seconds",
        available_boards=[b for b, st in boards.items() if st.get("available")],
        available_instances=available_instances,
        all_boards=boards,
    )



# =========================
# Remote file helpers
# =========================
def remote_path_allowed(remote_path: str, family: Optional[str] = None) -> bool:
    """Allow files under configured Quartus project roots and the server history archive."""
    if not remote_path or not remote_path.startswith("/"):
        return False
    info = server_info()
    roots = []
    if family == "standard":
        roots.append(info.get("standard_project_path"))
    elif family == "pro":
        roots.append(info.get("pro_project_path"))
    else:
        roots.extend([info.get("standard_project_path"), info.get("pro_project_path")])
    # Archived .v/.sof bundles can live under the private server_history base
    # directory when configured. This lets the Pi stay controller-only.
    try:
        roots.append((load_config().get("server_history", {}) or {}).get("base_dir", ""))
    except Exception:
        pass
    roots = [str(r).rstrip("/") for r in roots if r]
    return any(remote_path == r or remote_path.startswith(r + "/") for r in roots)



# =========================
# Verified SOF / Quartus file helpers
# =========================
def file_verification_config() -> Dict[str, Any]:
    cfg = load_config()
    return cfg.get("quartus_file_verification", {}) or {}


def min_sof_bytes() -> int:
    return max(1, int(file_verification_config().get("min_sof_bytes", 1024) or 1024))


def local_file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def local_file_stable_size(path: Path, delay_seconds: float = 0.20) -> Tuple[bool, int, int]:
    try:
        s1 = int(path.stat().st_size)
        time.sleep(max(0.0, float(delay_seconds)))
        s2 = int(path.stat().st_size)
        return s1 == s2, s1, s2
    except Exception:
        return False, -1, -1


def validate_local_sof_file(path: Path, compute_hash: bool = True) -> Dict[str, Any]:
    """Verify the SOF received by the Pi is complete before it can enter FIFO."""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return fail("SOF file does not exist on Raspberry Pi", path=str(p))
        if p.name.endswith(('.part', '.uploading', '.tmp')):
            return fail("SOF is still a temporary upload file", path=str(p))
        if p.suffix.lower() != ".sof":
            return fail("SOF verification failed: file extension is not .sof", path=str(p))
        stable, s1, s2 = local_file_stable_size(p)
        if not stable:
            return fail("SOF verification failed: file size is still changing", path=str(p), size_first=s1, size_second=s2)
        size = int(p.stat().st_size)
        if size < min_sof_bytes():
            return fail("SOF verification failed: file is too small to be a complete Quartus SOF", path=str(p), size_bytes=size, min_bytes=min_sof_bytes())
        data = {"success": True, "path": str(p), "size_bytes": size, "stable_size": True, "source": "raspberry_pi_local_upload"}
        if compute_hash:
            data["sha256"] = local_file_sha256(p)
        return data
    except Exception as e:
        return fail("SOF verification failed on Raspberry Pi", path=str(path), error=str(e))


def save_filestorage_atomic(fs_obj: Any, dest_path: Path) -> Dict[str, Any]:
    """Save Flask FileStorage to .uploading first, fsync, then atomic rename."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".uploading")
    try:
        if tmp_path.exists():
            tmp_path.unlink()
        fs_obj.save(str(tmp_path))
        with open(tmp_path, "rb") as f:
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(str(tmp_path), str(dest_path))
        return ok(path=str(dest_path), size_bytes=int(dest_path.stat().st_size))
    except Exception as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return fail("atomic upload save failed", path=str(dest_path), error=str(e))


def remote_file_size(remote_path: str) -> int:
    if not remote_path_allowed(remote_path):
        return -1
    q = shlex.quote(remote_path)
    code, out, err = run_remote(f'stat -c %s {q} 2>/dev/null || echo -1', timeout=20)
    try:
        return int(str(out).strip().splitlines()[-1])
    except Exception:
        return -1


def remote_file_sha256(remote_path: str) -> str:
    if not remote_path_allowed(remote_path):
        return ""
    q = shlex.quote(remote_path)
    code, out, err = run_remote(f'sha256sum {q} 2>/dev/null | awk {{\'print $1\'}}', timeout=60)
    if code == 0:
        return str(out).strip().splitlines()[0] if str(out).strip() else ""
    return ""


def verify_remote_sof_ready(remote_path: str, expected_size: int = 0, expected_sha256: str = "") -> Dict[str, Any]:
    """Verify a SOF on the Quartus server before programming; no guessing."""
    try:
        rp = str(remote_path or "").strip()
        if not rp:
            return fail("Remote SOF path is empty")
        if not remote_path_allowed(rp):
            return fail("Remote SOF path is outside allowed Quartus project folders", remote_sof=rp)
        if not rp.lower().endswith(".sof"):
            return fail("Remote SOF verification failed: file extension is not .sof", remote_sof=rp)

        q = shlex.quote(rp)
        # One SSH read checks existence and stable size. This reads the Quartus server's program file, not a guess.
        cmd = (
            f'if [ ! -f {q} ]; then echo MISSING; exit 2; fi; '
            f'S1=$(stat -c %s {q}); sleep 0.20; S2=$(stat -c %s {q}); '
            f'echo "$S1 $S2"'
        )
        code, out, err = run_remote(cmd, timeout=30)
        text = str(out or "").strip()
        if "MISSING" in text or code == 2:
            return fail("Remote SOF file does not exist on Quartus server", remote_sof=rp, stderr=err)
        parts = text.split()
        if len(parts) < 2:
            return fail("Remote SOF verification failed: could not read size from Quartus server", remote_sof=rp, raw=text, stderr=err)
        s1, s2 = int(parts[-2]), int(parts[-1])
        if s1 != s2:
            return fail("Remote SOF verification failed: Quartus server file size is still changing", remote_sof=rp, size_first=s1, size_second=s2)
        if s2 < min_sof_bytes():
            return fail("Remote SOF verification failed: file is too small to be a complete Quartus SOF", remote_sof=rp, size_bytes=s2, min_bytes=min_sof_bytes())
        if expected_size and int(expected_size) != int(s2):
            return fail("Remote SOF verification failed: size mismatch after transfer", remote_sof=rp, expected_size=int(expected_size), remote_size=int(s2))

        result = ok(remote_sof=rp, size_bytes=int(s2), stable_size=True, source="quartus_server_file_stat")
        if expected_sha256 and file_verification_config().get("sha256_enabled", True):
            remote_hash = remote_file_sha256(rp)
            result["sha256"] = remote_hash
            result["expected_sha256"] = expected_sha256
            if not remote_hash:
                return fail("Remote SOF verification failed: could not read sha256sum on Quartus server", remote_sof=rp, size_bytes=int(s2))
            if remote_hash != expected_sha256:
                return fail("Remote SOF verification failed: sha256 mismatch after transfer", remote_sof=rp, expected_sha256=expected_sha256, remote_sha256=remote_hash, size_bytes=int(s2))
        return result
    except Exception as e:
        return fail("Remote SOF verification failed", remote_sof=str(remote_path), error=str(e))


def wait_remote_sof_ready(remote_path: str, expected_size: int = 0, expected_sha256: str = "", timeout_seconds: int = 20) -> Dict[str, Any]:
    deadline = time.time() + max(1, int(timeout_seconds or 20))
    last = {}
    while time.time() <= deadline:
        last = verify_remote_sof_ready(remote_path, expected_size=expected_size, expected_sha256=expected_sha256)
        if last.get("success"):
            return last
        time.sleep(0.35)
    last = dict(last or {})
    last.setdefault("success", False)
    last.setdefault("error", "Remote SOF did not become ready before timeout")
    last["ready_timeout_seconds"] = int(timeout_seconds or 20)
    return last

def remote_file_exists(remote_path: str) -> bool:
    if not remote_path_allowed(remote_path):
        return False
    return remote_file_size(remote_path) >= 0


def read_remote_text(remote_path: str, max_bytes: int = 200000) -> str:
    """Read a text file that already exists on the Quartus server.

    Important: do not embed the path inside a single-quoted python -c command.
    Paths such as /home/... were losing their quotes through the shell and caused:
        SyntaxError: Path(/home/...)
    Using head/cat with shlex.quote is safer for server-side path mode.
    """
    if not remote_path_allowed(remote_path):
        raise ValueError(f"Remote path is outside allowed project folders: {remote_path}")

    qpath = shlex.quote(remote_path)
    max_bytes = max(1, min(int(max_bytes), 1000000))
    # LC_ALL=C avoids locale surprises. head -c limits very large Verilog files.
    cmd = f"LC_ALL=C head -c {max_bytes} {qpath}"
    code, out, err = run_remote(cmd, timeout=30)
    if code != 0:
        raise RuntimeError(err or out or f"Unable to read remote file: {remote_path}")
    return out


def find_server_projects(limit: int = 50, family: str = "standard") -> Dict[str, Any]:
    """Find folders on the Quartus server that contain a Verilog/SystemVerilog file and a .sof file."""
    root = project_path("pro" if family == "pro" else "standard")
    if not root:
        return fail(f"No project path configured for family: {family}")
    limit = max(1, min(int(limit), 200))
    py = """
import os, json
root = ROOT_PLACEHOLDER
limit = LIMIT_PLACEHOLDER
items = []
for dirpath, dirnames, filenames in os.walk(root):
    vfiles = [f for f in filenames if f.lower().endswith((\".v\", \".sv\"))]
    sofs = []
    for base in [dirpath, os.path.join(dirpath, \"output_files\")]:
        if os.path.isdir(base):
            for f in os.listdir(base):
                if f.lower().endswith(\".sof\"):
                    sofs.append(os.path.join(base, f))
    if vfiles and sofs:
        for vf in vfiles:
            items.append({\"project_folder\": dirpath, \"verilog_path\": os.path.join(dirpath, vf), \"sof_path\": sofs[0]})
            if len(items) >= limit:
                print(json.dumps(items)); raise SystemExit
print(json.dumps(items))
""".replace("ROOT_PLACEHOLDER", repr(root)).replace("LIMIT_PLACEHOLDER", str(limit))
    cmd = "python3 - <<'PYSERVERPROJECTS'\n" + py + "\nPYSERVERPROJECTS"
    code, out, err = run_remote(cmd, timeout=60)
    if code != 0:
        return fail(err or out or "Failed to list server projects")
    try:
        return ok(projects=json.loads(out or "[]"), family=family, root=root)
    except Exception as e:
        return fail(f"Could not parse project list: {e}", raw=out)

# =========================
# Programming workflow
# =========================
def cleanup_remote_runtime_sof(remote_sof: str, job_id: str) -> Dict[str, Any]:
    """Delete the temporary SOF copied to the Quartus server after quartus_pgm.

    v4.11 keeps only a small history record, so runtime SOF copies under
    pi_ai_jobs are removed after programming.
    """
    rp = str(remote_sof or "").strip()
    jid = str(job_id or "").strip()
    if not rp or rp.startswith("[dry-run]"):
        return ok(cleanup=False, reason="no runtime remote SOF")
    # v4.32: cached SOFs are intentionally retained for fast repeat programming.
    # They are not permanent history; they are a short-term Quartus-server cache.
    if "/pi_ai_sof_cache/" in rp:
        return ok(cleanup=False, reason="cached SOF retained for instant repeat programming", remote_sof=rp)
    if "/pi_ai_jobs/" not in rp or (jid and jid not in rp):
        return fail("refusing to delete non-runtime SOF path", remote_sof=rp)
    try:
        parent = str(Path(rp).parent)
        cmd = f"rm -f {shlex.quote(rp)}; rmdir {shlex.quote(parent)} 2>/dev/null || true"
        code, out, err = run_remote(cmd, timeout=30)
        return ok(cleanup=(code == 0), returncode=code, stdout=_tail_text(out, 500), stderr=_tail_text(err, 500), remote_sof=rp)
    except Exception as e:
        return fail("remote runtime SOF cleanup failed", error=str(e), remote_sof=rp)


def upload_sof_to_server(local_sof: Path, board: str, job_id: str) -> str:
    """Copy the user's .sof to the Quartus server and use it directly.

    v4.32 instant path:
    - Same SOF content is cached on the Quartus server under pi_ai_sof_cache.
    - If the same .sof is submitted again, the Pi skips SFTP upload entirely.
    - First-time upload still uses .uploading then atomic mv.
    - The cache is temporary operational speed cache, not grading/history storage.
    """
    cfg = load_config()
    b = cfg["board_catalog"][board]
    family = b.get("quartus_family", "standard")
    remote_base = project_path(family)
    local_sof = Path(local_sof)
    safe_name = secure_filename(local_sof.name or "design.sof")

    if not str(local_sof).lower().endswith(".sof"):
        raise RuntimeError(f"Expected a .sof file, got: {local_sof}")
    if not local_sof.exists() or not local_sof.is_file():
        raise RuntimeError(f"Local SOF file does not exist on Pi: {local_sof}")

    # Hashing a typical DE1 SOF is much faster than re-copying it over SSH/SFTP.
    h = hashlib.sha256()
    with open(local_sof, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    sof_hash = h.hexdigest()
    sof_size = int(local_sof.stat().st_size)
    board_safe = secure_filename(str(board or "board"))
    remote_cache_dir = f"{remote_base.rstrip('/')}/pi_ai_sof_cache/{board_safe}"
    remote_sof = f"{remote_cache_dir}/{sof_hash[:16]}_{safe_name}"
    remote_tmp = remote_sof + f".{secure_filename(str(job_id or 'job'))}.uploading"

    base_timeout = int((server_info() or {}).get("sof_copy_timeout_seconds", 180) or 180)
    copy_timeout = max(base_timeout, sof_copy_timeout_for_board(board), 60)
    attempts = int((load_config().get("quartus_server", {}) or {}).get("sof_copy_attempts", 2) or 2)
    attempts = max(1, min(attempts, 4))
    last_error = ""

    # Fast cache check: if the content-addressed SOF already exists with the same size,
    # return immediately and do not upload again.
    try:
        check_cmd = f"test -s {shlex.quote(remote_sof)} && stat -c %s {shlex.quote(remote_sof)} || true"
        code, out, err = run_remote(check_cmd, timeout=8)
        if str(out or "").strip().splitlines()[-1:] == [str(sof_size)]:
            try:
                add_history("sof_cache_hit", board, {"job_id": job_id, "remote_sof": remote_sof, "size": sof_size})
            except Exception:
                pass
            return remote_sof
    except Exception:
        pass

    for attempt in range(1, attempts + 1):
        ssh = None
        try:
            ssh = connect_server()
            try:
                tr = ssh.get_transport()
                if tr:
                    tr.set_keepalive(10)
            except Exception:
                pass
            stdin, stdout, stderr = ssh.exec_command(f'mkdir -p {shlex.quote(remote_cache_dir)}', timeout=min(copy_timeout, 30))
            rc = stdout.channel.recv_exit_status()
            if rc != 0:
                err = stderr.read().decode("utf-8", errors="ignore") if hasattr(stderr, "read") else ""
                raise RuntimeError(f"Unable to create remote SOF cache directory on Quartus server: {err}")
            sftp = ssh.open_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(copy_timeout)
                except Exception:
                    pass
                try:
                    sftp.remove(remote_tmp)
                except Exception:
                    pass
                sftp.put(str(local_sof), remote_tmp)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            mv_cmd = f'mv -f {shlex.quote(remote_tmp)} {shlex.quote(remote_sof)}'
            stdin, stdout, stderr = ssh.exec_command(mv_cmd, timeout=min(copy_timeout, 30))
            rc = stdout.channel.recv_exit_status()
            if rc != 0:
                err = stderr.read().decode("utf-8", errors="ignore") if hasattr(stderr, "read") else ""
                raise RuntimeError(f"Unable to finalize cached remote SOF file after upload: {err}")
            try:
                add_history("sof_cache_store", board, {"job_id": job_id, "remote_sof": remote_sof, "size": sof_size})
            except Exception:
                pass
            return remote_sof
        except Exception as e:
            last_error = str(e)
            try:
                add_history("sof_copy_retry", board, {"job_id": job_id, "attempt": attempt, "attempts": attempts, "error": last_error})
            except Exception:
                pass
            if attempt < attempts:
                time.sleep(min(1.0 * attempt, 2.0))
                continue
            raise RuntimeError(f"SOF copy/cache to Quartus server failed after {attempts} attempt(s) and {copy_timeout}s timeout: {last_error}")
        finally:
            try:
                if ssh:
                    ssh.close()
            except Exception:
                pass

    raise RuntimeError(f"SOF copy/cache to Quartus server failed: {last_error}")


def program_server(board: str, remote_sof: str, job_id: str, preferred_cable: str = "") -> Dict[str, Any]:
    cfg = load_config()
    b = cfg["board_catalog"][board]
    family = b.get("quartus_family", "standard")
    exe = quartus_exe(family)

    # v4.25: use the exact locked physical target, but warm up the JTAG server
    # and retry transient connection/synchronization failures inside the same
    # running job.  The user should not see a false failed/requeued job that only
    # succeeds after the visible running timer reaches 00:00.
    if preferred_cable:
        cable = str(preferred_cable).strip()
        cable_info = {"source": "locked_job_target", "board": board, "preferred_cable": preferred_cable}
    else:
        cable, cable_info = resolve_programming_cable_for_board(board, b, preferred_cable="")
        cable_info["warning"] = "no locked preferred cable was provided; resolved by live board scan"

    idx = str(b.get("jtag_device_index", "")).strip()
    target = f"{remote_sof}@{idx}" if idx else remote_sof
    timeout = quartus_program_timeout_for_board(board)
    cmd = f'"{exe}" -m JTAG -c "{cable}" -o "p;{target}"'

    target_check = validate_locked_programming_target(board, cable, job_id=job_id)
    if not target_check.get("success"):
        target_check.update({"success": False, "command": cmd, "returncode": -1, "stdout": "", "stderr": target_check.get("error", "programming target safety stop"), "cable_source": cable_info})
        return target_check

    if load_config().get("dry_run", True):
        return ok(
            dry_run=True,
            command=cmd,
            stdout="[DRY-RUN] Programming command not executed.",
            stderr="",
            returncode=0,
            cable_source=cable_info,
            locked_target=target_check,
            sof_source="simple_sof_passthrough",
            jtag_connection_policy="v4.25_warmup_retry_skipped_dry_run",
        )

    reliability = (cfg.get("quartus_programming_reliability", {}) or {})
    max_attempts = int(reliability.get("jtag_connect_attempts", 3) or 3)
    max_attempts = max(1, min(max_attempts, 5))
    warmup_enabled = bool(reliability.get("preflight_jtag_warmup", True))
    retry_delay = float(reliability.get("jtag_connect_retry_delay_seconds", 2.0) or 2.0)
    settle_seconds = float(reliability.get("jtag_settle_seconds_before_program", 1.5) or 1.5)
    warmup_timeout = int(reliability.get("jtag_warmup_timeout_seconds", 35) or 35)

    def retryable_jtag_connect_failure(code: int, out: str, err: str) -> bool:
        text_l = f"{out or ''}\n{err or ''}".lower()
        # Connection/JTAG-server/scan-chain failures are system timing issues and
        # may succeed after jtagd/quartus_pgm -l warms the cable.  Do not treat
        # invalid SOF/user-design errors as retryable.
        hard_user_errors = [
            "sof file", "not compatible", "not supported", "can't recognize silicon id",
            "incorrect device", "device id does not match", "file is not a valid",
        ]
        if any(p in text_l for p in hard_user_errors):
            return False
        patterns = [
            "failed to connect", "cannot connect", "can't connect", "connection refused",
            "connection reset", "connection timed", "temporarily unavailable",
            "jtagd", "jtag server", "no jtag hardware", "unable to access jtag",
            "can't access jtag", "cannot access jtag", "unable to scan",
            "can't scan jtag", "cannot scan jtag", "jtag chain", "different taps selected",
            "synchronization failed", "unable to lock", "chain in use", "server busy",
            "usb-blaster", "error (18939)", "error (18952)", "error (209025)",
        ]
        return any(p in text_l for p in patterns)

    def warmup_jtag(reason: str, attempt: int) -> Dict[str, Any]:
        if not warmup_enabled:
            return ok(skipped=True, reason="warmup disabled")
        update_running_job_phase(job_id, "jtag_warmup", f"warming JTAG connection before programming attempt {attempt}: {cable}")
        # quartus_pgm -l wakes jtagd and refreshes the cable table.  jtagconfig is
        # optional; it is ignored if not installed.  Both use the same Quartus server.
        sleep_cmd = f"sleep {max(0.0, settle_seconds):.2f}"
        warm_cmd = (
            f'("{exe}" -l || true); '
            f'(jtagconfig 2>/dev/null || true); '
            f'{sleep_cmd}; '
            f'("{exe}" -l || true)'
        )
        code, out, err = run_remote(warm_cmd, timeout=warmup_timeout)
        return {
            "success": True,
            "returncode": code,
            "stdout_tail": _tail_text(out, 1200),
            "stderr_tail": _tail_text(err, 1200),
            "reason": reason,
            "attempt": attempt,
            "cable": cable,
        }

    remote_timeout = max(60, int(timeout or 120))
    if "agilex" in str(board or "").lower():
        remote_timeout = max(remote_timeout, 900)

    warmups: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    # v4.32 instant mode: if the background/bash prewarm daemon recently ran,
    # do not spend another 5-12 seconds doing quartus_pgm -l inside this job.
    instant_cfg = (cfg.get("instant_programming", {}) or {})
    prewarm_fresh_seconds = int(instant_cfg.get("prewarm_fresh_seconds", 90) or 90)
    prewarm_age = max(999999, int(time.time() - float(JTAG_PREWARM_HEARTBEAT_TS or 0)))
    prewarm_ok = bool((JTAG_PREWARM_LAST_RESULT or {}).get("success", False)) and prewarm_age <= prewarm_fresh_seconds
    if bool(instant_cfg.get("skip_per_job_warmup_if_prewarmed", True)) and prewarm_ok:
        warmups.append(ok(skipped=True, reason="recent_background_prewarm_is_fresh", prewarm_age_seconds=prewarm_age))
    else:
        warmups.append(warmup_jtag("preflight_before_first_quartus_pgm", 1))

    last = {"success": False, "returncode": -1, "stdout": "", "stderr": "programming did not run"}
    for attempt in range(1, max_attempts + 1):
        update_running_job_phase(job_id, "programming", f"running quartus_pgm attempt {attempt}/{max_attempts} on {cable}")
        log_file = f"/tmp/pi_ai_{job_id}_attempt_{attempt}.log"
        inner = f'{cmd} </dev/null > {shlex.quote(log_file)} 2>&1'
        wrapped = (
            f'timeout --kill-after=10s {remote_timeout}s bash -lc {shlex.quote(inner)}; '
            f'RET=$?; cat {shlex.quote(log_file)} 2>/dev/null; exit $RET'
        )
        code, out, err = run_remote(wrapped, timeout=remote_timeout + 25)
        success_text = f"{out or ''}\n{err or ''}"
        success = code == 0 or "Configuration succeeded" in success_text or "Successfully performed" in success_text
        retryable = retryable_jtag_connect_failure(code, out, err)
        if code == 124 and not success:
            err = (err or "") + f"\n[timeout] quartus_pgm exceeded {remote_timeout}s and was killed"
        this_attempt = {
            "attempt": attempt,
            "returncode": code,
            "success": success,
            "retryable_jtag_connect_failure": retryable,
            "stdout_tail": _tail_text(out, 2500),
            "stderr_tail": _tail_text(err, 2500),
        }
        attempts.append(this_attempt)
        last = {
            "success": success,
            "command": cmd,
            "returncode": code,
            "stdout": out,
            "stderr": err,
            "cable_source": cable_info,
            "locked_target": target_check,
            "remote_timeout_seconds": remote_timeout,
            "sof_source": "simple_sof_passthrough",
            "jtag_connection_policy": "v4.32_instant_skip_warmup_if_prewarmed_then_retry",
            "program_attempts": attempts,
            "jtag_warmups": warmups,
        }
        if success:
            last["success"] = True
            return last
        if not retryable:
            break
        if attempt < max_attempts:
            warmups.append(warmup_jtag("retry_after_transient_jtag_connect_failure", attempt + 1))
            time.sleep(max(0.0, retry_delay))

    if attempts and attempts[-1].get("retryable_jtag_connect_failure"):
        last["retryable_system_error"] = True
        last["system_requeue"] = True
        last["stderr"] = (last.get("stderr") or "") + "\n[v4.32] JTAG connection did not stabilize after internal warmup attempts; job should be replanned instead of blamed on the user's SOF."
    return last


def program_locked_board_provided_sof_only(initial_board: str, remote_sof: str, job_id: str, verilog_code: str = "", filename: str = "design.v", selected_jtag_cable: str = "", test_seconds: int = 0) -> Dict[str, Any]:
    """
    Program only the AI-selected physical board instance using the provided .sof.
    On success, keep that JTAG instance locked for the student's customizable test timer.
    No cross-board fallback is attempted.
    """
    attempts = []
    test_seconds = max(0, int(test_seconds or 0))
    program_start_ts = time.time()
    result = program_server(initial_board, remote_sof, job_id, preferred_cable=selected_jtag_cable)
    program_end_ts = time.time()
    usage_after = record_jtag_usage(
        initial_board,
        selected_jtag_cable,
        job_id,
        bool(result.get("success")),
        program_start_ts,
        program_end_ts,
        sof_name=remote_sof,
    )
    attempts.append({
        "board": initial_board,
        "detected_cable": selected_jtag_cable,
        "result": result,
        "reason": "program_provided_sof_only_no_fallback",
        "program_seconds": int(program_end_ts - program_start_ts),
        "jtag_usage_after": usage_after,
    })

    if result.get("success"):
        hold_result = hold_instance_for_testing(initial_board, selected_jtag_cable, job_id, test_seconds)
        return {
            "success": True,
            "selected_board": initial_board,
            "selected_jtag_cable": selected_jtag_cable,
            "jtag_cable": selected_jtag_cable,
            "program_result": result,
            "fallback_used": False,
            "fallback_disabled": True,
            "fallback_attempts": attempts,
            "jtag_usage_after": usage_after,
            "program_seconds": int(program_end_ts - program_start_ts),
            "test_seconds": test_seconds,
            "test_minutes": int(round(test_seconds / 60)) if test_seconds else 0,
            "test_timer": hold_result,
            "released": hold_result if test_seconds <= 0 else {},
            "held_for_testing": test_seconds > 0,
        }

    rel = release_instance(initial_board, selected_jtag_cable, reason="program_failed")
    system_requeue = bool(result.get("system_requeue") or result.get("retryable_system_error") or result.get("target_validation_failed"))
    return {
        "success": False,
        "selected_board": initial_board,
        "selected_jtag_cable": selected_jtag_cable,
        "jtag_cable": selected_jtag_cable,
        "program_result": result,
        "fallback_used": False,
        "fallback_disabled": True,
        "fallback_reason": summarize_program_failure({"program_result": result}) or ("System target validation failed; job will be replanned." if system_requeue else "Provided SOF programming failed. User must provide a SOF made for the selected physical FPGA."),
        "fallback_attempts": attempts,
        "jtag_usage_after": usage_after,
        "program_seconds": int(program_end_ts - program_start_ts),
        "test_seconds": 0,
        "test_minutes": 0,
        "released": rel,
        "held_for_testing": False,
        "system_requeue": system_requeue,
        "retryable_system_error": system_requeue,
        "target_validation_failed": bool(result.get("target_validation_failed")),
    }


def _log_programming_remote_once(board: str, sof_name: str, client_hostname: str, student_ip: str, success: bool) -> None:
    """Best-effort remote text log. Runs only in a daemon thread."""
    cfg = load_config()
    b = cfg["board_catalog"][board]
    family = b.get("quartus_family", "standard")
    lp = log_path(family)
    if not lp:
        return
    text = f"PiAI | {now_iso()} | board={board} | sof={sof_name} | host={client_hostname} | ip={student_ip} | success={success}"
    # Keep this short and non-critical. Remote logging must never block FIFO.
    run_remote(f'mkdir -p "$(dirname {lp})"; echo {shlex.quote(text)} >> {shlex.quote(lp)}', timeout=5)


def log_programming(board: str, sof_name: str, client_hostname: str, student_ip: str, success: bool) -> None:
    """Nonblocking programming log.

    Older versions wrote the remote log synchronously after quartus_pgm returned.
    If SSH/logging stalled, the job stayed stuck as running with Remain 00:00 and
    the queue worker stopped starting later jobs. This function now returns
    immediately and does optional remote logging in the background.
    """
    try:
        add_history("programming_log_queued", board, {
            "sof": sof_name,
            "host": client_hostname,
            "ip": student_ip,
            "success": bool(success),
        })
    except Exception:
        pass

    def _worker():
        try:
            _log_programming_remote_once(board, sof_name, client_hostname, student_ip, success)
        except Exception as e:
            try:
                add_history("programming_log_failed_noncritical", board, {"sof": sof_name, "error": str(e)})
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True, name=f"program_log_{board}_{sof_name}").start()



def update_running_job_phase(job_id: str, phase: str, message: str = "") -> None:
    """Best-effort visible phase update for long-running deploy stages."""
    try:
        state = load_state()
        job = state.setdefault("jobs", {}).get(str(job_id))
        if not isinstance(job, dict) or str(job.get("status") or "").lower() != "running":
            return
        job["running_phase"] = str(phase or "programming")
        if message:
            job["message"] = str(message)
        job["phase_updated_at"] = now_iso()
        job["phase_updated_ts"] = time.time()
        state["jobs"][str(job_id)] = job
        save_state_preserving_concurrent_jobs(state)
        try:
            update_queue_stream_broadcast_once("running_phase_changed")
        except Exception:
            pass
    except Exception:
        pass

def perform_deploy(verilog_path: Path, sof_path: Path, requested_board: Optional[str], client_hostname: str, student_ip: str, job_id: Optional[str] = None, test_seconds: int = 0, qsf_text: str = "", qsf_path: str = "") -> Dict[str, Any]:
    test_seconds = max(0, int(test_seconds or 0))
    if verilog_path.suffix.lower() not in (".v", ".sv"):
        return fail("Only .v or .sv Verilog/SystemVerilog files are accepted.")
    if sof_path.suffix.lower() != ".sof":
        return fail("Only .sof programming files are accepted.")

    limits = controller_runtime_limits()
    verilog_code = limited_text_file_read(verilog_path, int(limits.get("max_classifier_verilog_bytes", 1024 * 1024)))
    if not qsf_text and qsf_path:
        try:
            qp = Path(qsf_path)
            if qp.exists() and qp.is_file() and qp.suffix.lower() == ".qsf":
                qsf_text = limited_text_file_read(qp, int(limits.get("max_classifier_qsf_bytes", 256 * 1024)))
        except Exception:
            qsf_text = ""
    job_id = job_id or uuid.uuid4().hex[:10]
    # v4.41: queued jobs already have a locked JTAG target. Do not force a live
    # quartus_pgm -l rescan here; use the prewarm/cache path and let the final
    # safety gate validate the locked cable from cache. This removes avoidable
    # seconds before quartus_pgm starts.
    instant_cfg = (load_config().get("instant_programming", {}) or {})
    force_live_jtag = not (requested_board and bool(instant_cfg.get("skip_live_jtag_rescan_for_locked_jobs", True)))
    update_running_job_phase(job_id, "ai_selecting", "AI is reading Verilog and QSF to select the board")
    ai = select_board_ai(verilog_code, verilog_path.name, requested_board=requested_board, force_refresh=force_live_jtag, allow_job_id=job_id, qsf_text=qsf_text)
    if not ai.get("success"):
        return ai
    if not ai.get("safe_to_program"):
        return fail("AI selection confidence is below safe programming threshold.", ai_result=ai)

    board = ai.get("selected_board")
    update_running_job_phase(job_id, "preparing_board", f"AI selected {board}; preparing the assigned JTAG board")
    selected_jtag_cable = ai.get("selected_jtag_cable", "")
    mark_queue_job_jtag(job_id, board, selected_jtag_cable, ai.get("selected_instance_id", ""), ai.get("selected_lock_key", ""), ai)
    lock = lock_instance(board, selected_jtag_cable, owner=f"deploy:{job_id}", expected_seconds=programming_lock_expected_seconds(board, test_seconds), job_id=job_id)
    if not lock.get("success"):
        return lock

    remote_sof = ""
    program_result = None
    try:
        update_running_job_phase(job_id, "preparing_board", "power/reset before programming")
        power_set(board, True)
        reset_board(board)

        if load_config().get("dry_run", True):
            remote_sof = f"[dry-run]/{secure_filename(sof_path.name)}"
        else:
            update_running_job_phase(job_id, "copying_sof", "copying SOF to Quartus server for quartus_pgm")
            remote_sof = upload_sof_to_server(sof_path, board, job_id)

        update_running_job_phase(job_id, "programming", "running quartus_pgm on selected JTAG slot")
        deploy_program = program_locked_board_provided_sof_only(board, remote_sof, job_id, verilog_code, verilog_path.name, selected_jtag_cable, test_seconds=test_seconds)
        success = bool(deploy_program.get("success"))
        final_board = deploy_program.get("selected_board", board)
        log_programming(final_board, sof_path.name, client_hostname, student_ip, success)
        remote_sof_cleanup = cleanup_remote_runtime_sof(remote_sof, job_id)
        return {
            "success": success,
            "job_id": job_id,
            "selected_board": final_board,
            "ai_selected_board": board,
            "selected_jtag_cable": selected_jtag_cable,
            "jtag_cable": selected_jtag_cable,
            "selected_instance_id": ai.get("selected_instance_id"),
            "jtag_instance": ai.get("selected_instance_id"),
            "selected_lock_key": ai.get("selected_lock_key"),
            "jtag_usage_after": deploy_program.get("jtag_usage_after"),
            "program_seconds": deploy_program.get("program_seconds"),
            "test_seconds": deploy_program.get("test_seconds", test_seconds),
            "test_minutes": deploy_program.get("test_minutes", int(round(test_seconds / 60)) if test_seconds else 0),
            "test_timer": deploy_program.get("test_timer", {}),
            "held_for_testing": deploy_program.get("held_for_testing", False),
            "ai_result": ai,
            "remote_sof": remote_sof,
            "remote_sof_cleanup": remote_sof_cleanup,
            "program_result": deploy_program.get("program_result"),
            "fallback_used": deploy_program.get("fallback_used", False),
            "fallback_from": deploy_program.get("fallback_from"),
            "fallback_reason": deploy_program.get("fallback_reason"),
            "fallback_attempts": deploy_program.get("fallback_attempts", []),
            "initial_release": deploy_program.get("initial_release"),
            "released": deploy_program.get("released"),
        }
    except Exception as e:
        rel = release_instance(board, selected_jtag_cable, reason="system_exception_requeue")
        remote_sof_cleanup = cleanup_remote_runtime_sof(remote_sof, job_id) if remote_sof else ok(cleanup=False, reason="no remote_sof")
        msg = str(e)
        transient = any(term in msg.lower() for term in ("timeout", "timed out", "sftp", "ssh", "socket", "transport", "connection", "temporarily"))
        return fail(msg, selected_board=board, selected_jtag_cable=selected_jtag_cable, remote_sof=remote_sof, remote_sof_cleanup=remote_sof_cleanup, program_result=program_result, released=rel, system_requeue=transient, retryable_system_error=transient)





def perform_deploy_code_server_sof(verilog_code: str, filename: str, sof_remote_path: str, requested_board: Optional[str], client_hostname: str, student_ip: str, job_id: Optional[str] = None, test_seconds: int = 0, qsf_text: str = "") -> Dict[str, Any]:
    """Mixed mode: Verilog code was uploaded/provided, but the SOF already exists on the Quartus server."""
    test_seconds = max(0, int(test_seconds or 0))
    if not filename.lower().endswith((".v", ".sv")):
        return fail("filename must end with .v or .sv.")
    if not sof_remote_path.lower().endswith(".sof"):
        return fail("sof_path must point to a .sof file on the server.")
    if not remote_path_allowed(sof_remote_path):
        return fail("sof_path is outside allowed server project folders.", sof_path=sof_remote_path)
    if not remote_file_exists(sof_remote_path):
        return fail("Remote SOF file does not exist.", sof_path=sof_remote_path)
    if not verilog_code.strip():
        return fail("verilog_code is empty.")

    job_id = job_id or uuid.uuid4().hex[:10]
    instant_cfg = (load_config().get("instant_programming", {}) or {})
    force_live_jtag = not (requested_board and bool(instant_cfg.get("skip_live_jtag_rescan_for_locked_jobs", True)))
    update_running_job_phase(job_id, "ai_selecting", "AI is reading Verilog and QSF to select the board")
    ai = select_board_ai(verilog_code, filename, requested_board=requested_board, force_refresh=force_live_jtag, allow_job_id=job_id, qsf_text=qsf_text)
    if not ai.get("success"):
        return ai
    if not ai.get("safe_to_program"):
        return fail("AI selection confidence is below safe programming threshold.", ai_result=ai)

    board = ai.get("selected_board")
    update_running_job_phase(job_id, "preparing_board", f"AI selected {board}; preparing the assigned JTAG board")
    selected_jtag_cable = ai.get("selected_jtag_cable", "")
    mark_queue_job_jtag(job_id, board, selected_jtag_cable, ai.get("selected_instance_id", ""), ai.get("selected_lock_key", ""), ai)
    lock = lock_instance(board, selected_jtag_cable, owner=f"deploy_code_server_sof:{job_id}", expected_seconds=programming_lock_expected_seconds(board, test_seconds), job_id=job_id)
    if not lock.get("success"):
        return lock

    program_result = None
    try:
        power_set(board, True)
        reset_board(board)
        deploy_program = program_locked_board_provided_sof_only(board, sof_remote_path, job_id, verilog_code, filename, selected_jtag_cable, test_seconds=test_seconds)
        success = bool(deploy_program.get("success"))
        final_board = deploy_program.get("selected_board", board)
        log_programming(final_board, Path(sof_remote_path).name, client_hostname, student_ip, success)
        return {
            "success": success,
            "mode": "local_verilog_server_sof",
            "job_id": job_id,
            "selected_board": final_board,
            "ai_selected_board": board,
            "selected_jtag_cable": selected_jtag_cable,
            "jtag_cable": selected_jtag_cable,
            "selected_instance_id": ai.get("selected_instance_id"),
            "jtag_instance": ai.get("selected_instance_id"),
            "selected_lock_key": ai.get("selected_lock_key"),
            "jtag_usage_after": deploy_program.get("jtag_usage_after"),
            "program_seconds": deploy_program.get("program_seconds"),
            "test_seconds": deploy_program.get("test_seconds", test_seconds),
            "test_minutes": deploy_program.get("test_minutes", int(round(test_seconds / 60)) if test_seconds else 0),
            "test_timer": deploy_program.get("test_timer", {}),
            "held_for_testing": deploy_program.get("held_for_testing", False),
            "ai_result": ai,
            "filename": filename,
            "remote_sof": sof_remote_path,
            "program_result": deploy_program.get("program_result"),
            "fallback_used": deploy_program.get("fallback_used", False),
            "fallback_from": deploy_program.get("fallback_from"),
            "fallback_reason": deploy_program.get("fallback_reason"),
            "fallback_attempts": deploy_program.get("fallback_attempts", []),
            "initial_release": deploy_program.get("initial_release"),
            "released": deploy_program.get("released"),
        }
    except Exception as e:
        rel = release_instance(board, selected_jtag_cable, reason="exception")
        return fail(str(e), mode="local_verilog_server_sof", selected_board=board, selected_jtag_cable=selected_jtag_cable, remote_sof=sof_remote_path, program_result=program_result, released=rel)

def perform_deploy_server_paths(verilog_remote_path: str, sof_remote_path: str, requested_board: Optional[str], client_hostname: str, student_ip: str, job_id: Optional[str] = None, test_seconds: int = 0, qsf_text: str = "", qsf_remote_path: str = "") -> Dict[str, Any]:
    """Mode 2: use files that already exist on the Quartus server."""
    test_seconds = max(0, int(test_seconds or 0))
    if not verilog_remote_path.lower().endswith((".v", ".sv")):
        return fail("verilog_path must point to a .v or .sv file on the server.")
    if not sof_remote_path.lower().endswith(".sof"):
        return fail("sof_path must point to a .sof file on the server.")
    if not remote_path_allowed(verilog_remote_path):
        return fail("verilog_path is outside allowed server project folders.", verilog_path=verilog_remote_path)
    if not remote_path_allowed(sof_remote_path):
        return fail("sof_path is outside allowed server project folders.", sof_path=sof_remote_path)
    if not remote_file_exists(verilog_remote_path):
        return fail("Remote Verilog file does not exist.", verilog_path=verilog_remote_path)
    if not remote_file_exists(sof_remote_path):
        return fail("Remote SOF file does not exist.", sof_path=sof_remote_path)

    try:
        verilog_code = read_remote_text(verilog_remote_path)
    except Exception as e:
        return fail(f"Unable to read remote Verilog file: {e}", verilog_path=verilog_remote_path)
    if not qsf_text and qsf_remote_path:
        try:
            if remote_path_allowed(qsf_remote_path) and remote_file_exists(qsf_remote_path):
                qsf_text = read_remote_text(qsf_remote_path)
        except Exception:
            qsf_text = ""

    job_id = job_id or uuid.uuid4().hex[:10]
    instant_cfg = (load_config().get("instant_programming", {}) or {})
    force_live_jtag = not (requested_board and bool(instant_cfg.get("skip_live_jtag_rescan_for_locked_jobs", True)))
    update_running_job_phase(job_id, "ai_selecting", "AI is reading Verilog and QSF to select the board")
    ai = select_board_ai(verilog_code, Path(verilog_remote_path).name, requested_board=requested_board, force_refresh=force_live_jtag, allow_job_id=job_id, qsf_text=qsf_text)
    if not ai.get("success"):
        return ai
    if not ai.get("safe_to_program"):
        return fail("AI selection confidence is below safe programming threshold.", ai_result=ai)

    board = ai.get("selected_board")
    update_running_job_phase(job_id, "preparing_board", f"AI selected {board}; preparing the assigned JTAG board")
    selected_jtag_cable = ai.get("selected_jtag_cable", "")
    mark_queue_job_jtag(job_id, board, selected_jtag_cable, ai.get("selected_instance_id", ""), ai.get("selected_lock_key", ""), ai)
    lock = lock_instance(board, selected_jtag_cable, owner=f"deploy_server_path:{job_id}", expected_seconds=programming_lock_expected_seconds(board, test_seconds), job_id=job_id)
    if not lock.get("success"):
        return lock

    program_result = None
    try:
        power_set(board, True)
        reset_board(board)
        deploy_program = program_locked_board_provided_sof_only(board, sof_remote_path, job_id, verilog_code, Path(verilog_remote_path).name, selected_jtag_cable, test_seconds=test_seconds)
        success = bool(deploy_program.get("success"))
        final_board = deploy_program.get("selected_board", board)
        log_programming(final_board, Path(sof_remote_path).name, client_hostname, student_ip, success)
        return {
            "success": success,
            "mode": "server_paths",
            "job_id": job_id,
            "selected_board": final_board,
            "ai_selected_board": board,
            "selected_jtag_cable": selected_jtag_cable,
            "jtag_cable": selected_jtag_cable,
            "selected_instance_id": ai.get("selected_instance_id"),
            "jtag_instance": ai.get("selected_instance_id"),
            "selected_lock_key": ai.get("selected_lock_key"),
            "jtag_usage_after": deploy_program.get("jtag_usage_after"),
            "program_seconds": deploy_program.get("program_seconds"),
            "test_seconds": deploy_program.get("test_seconds", test_seconds),
            "test_minutes": deploy_program.get("test_minutes", int(round(test_seconds / 60)) if test_seconds else 0),
            "test_timer": deploy_program.get("test_timer", {}),
            "held_for_testing": deploy_program.get("held_for_testing", False),
            "ai_result": ai,
            "verilog_path": verilog_remote_path,
            "remote_sof": sof_remote_path,
            "program_result": deploy_program.get("program_result"),
            "fallback_used": deploy_program.get("fallback_used", False),
            "fallback_from": deploy_program.get("fallback_from"),
            "fallback_reason": deploy_program.get("fallback_reason"),
            "fallback_attempts": deploy_program.get("fallback_attempts", []),
            "initial_release": deploy_program.get("initial_release"),
            "released": deploy_program.get("released"),
        }
    except Exception as e:
        rel = release_instance(board, selected_jtag_cable, reason="exception")
        return fail(str(e), mode="server_paths", selected_board=board, selected_jtag_cable=selected_jtag_cable, remote_sof=sof_remote_path, program_result=program_result, released=rel)

# =========================
# API helpers/routes
# =========================
def ok(**kwargs):
    data = {"success": True}
    data.update(kwargs)
    return data


def fail(message: str, **kwargs):
    data = {"success": False, "error": message}
    data.update(kwargs)
    return data



@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(_exc):
    limits = controller_runtime_limits()
    max_mb = max(1, int(limits.get("max_upload_bytes", 0) / (1024 * 1024)))
    return response(
        fail(
            "Upload is larger than the Raspberry Pi safety limit.",
            max_upload_mb=max_mb,
            hint="Reduce the .sof/.v size or raise upload_limits.max_upload_mb in config_pi_hat.json/private Pi config if you intentionally need larger uploads.",
        ),
        413,
    )


def response(data: Dict[str, Any], status: int = 200):
    resp = jsonify(data)
    try:
        resp.headers["X-UADY-Server-Time-Ns"] = str(time.time_ns())
        resp.headers["X-UADY-Sync-Mode"] = "event-driven-low-latency"
    except Exception:
        pass
    return resp, status


@app.get("/healthz")
def api_healthz():
    return response(ok(status="alive", timestamp=now_iso(), controller="pi_api"))


@app.get("/sync/ping")
def api_sync_ping():
    """Lightweight timestamp endpoint for checking GUI <-> Pi sync latency."""
    with QUEUE_STREAM_BROADCAST_LOCK:
        stream_meta = {k: v for k, v in QUEUE_STREAM_BROADCAST.items() if k != "payload"}
    include_queue = str(request.args.get("queue", "0")).lower() in ("1", "true", "yes")
    return response(ok(
        sync_mode="event_driven_low_latency",
        server_time_ts=time.time(),
        server_time_ns=time.time_ns(),
        controller=load_config().get("controller_name"),
        stream_broadcast=stream_meta,
        queue=queue_snapshot(fast=True) if include_queue else None,
    ))


@app.get("/status")
def api_status():
    cfg = load_config()
    return response(ok(
        controller=cfg.get("controller_name"),
        host=cfg.get("host"),
        port=cfg.get("port"),
        dry_run=cfg.get("dry_run"),
        use_gpio=cfg.get("use_gpio"),
        api_key_required=api_auth_required(),
        default_test_minutes=default_test_minutes(),
        max_test_minutes=max_test_minutes(),
        disabled_jtag_count=len(load_state().get("disabled_jtag", {})),
        server_host=server_info().get("host"),
        hostname=socket.gethostname(),
        time=now_iso(),
        traffic=traffic_snapshot(),
        traffic_limits=traffic_cfg(),
        dynamic_scaling=adaptive_runtime_config(refresh=False),
        queue_worker_heartbeat_age_seconds=max(0, int(time.time() - float(QUEUE_WORKER_HEARTBEAT_TS or 0))) if QUEUE_WORKER_HEARTBEAT_TS else None,
        pending_queued_jobs=pending_queued_job_count(),
    ))


@app.get("/jtag")
def api_jtag():
    force = str(request.args.get("force", "0")).lower() in ("1", "true", "yes")
    return response(discover_jtag(force=force))


@app.post("/jtag/prewarm_now")
def api_jtag_prewarm_now():
    require_pi_key()
    return response(jtag_prewarm_once("manual_api_prewarm_now"))


@app.get("/jtag/prewarm_status")
def api_jtag_prewarm_status():
    now_ts = time.time()
    heartbeat_age = None
    if JTAG_PREWARM_HEARTBEAT_TS:
        heartbeat_age = max(0, int(now_ts - float(JTAG_PREWARM_HEARTBEAT_TS or now_ts)))
    return response(ok(
        started=bool(JTAG_PREWARM_WORKER_STARTED),
        heartbeat_age_seconds=heartbeat_age,
        active_programming_jobs=active_programming_jobs_for_prewarm(),
        last_result=JTAG_PREWARM_LAST_RESULT,
        config=jtag_prewarm_daemon_config(),
    ))


@app.get("/boards")
def api_boards():
    force = request.args.get("force", "0") in ("1", "true", "yes")
    return response(board_status(force_jtag=force))


@app.post("/ai/classify_board")
def api_classify_board():
    data = request.get_json(silent=True) or {}
    verilog_code = data.get("verilog_code", "")
    verilog_path = (data.get("verilog_path") or "").strip()
    filename = data.get("filename", "uploaded.v")
    qsf_text = data.get("qsf_text", "") or ""
    qsf_path = (data.get("qsf_path") or "").strip()
    if qsf_path and not qsf_text.strip():
        try:
            if remote_path_allowed(qsf_path) and remote_file_exists(qsf_path):
                qsf_text = read_remote_text(qsf_path)
        except Exception:
            qsf_text = ""
    if verilog_path and not verilog_code.strip():
        try:
            verilog_code = read_remote_text(verilog_path)
            filename = Path(verilog_path).name
        except Exception as e:
            return response(fail(str(e), verilog_path=verilog_path), 400)
    if not verilog_code.strip():
        return response(fail("verilog_code or verilog_path is required"), 400)
    if qwen_prompt_only_mode() and ollama_qwen_config().get("enabled"):
        local_classifier = {"target_board": "Ambiguous - manual selection required", "provider": "local_classifier_skipped_prompt_only", "confidence_score": 0}
        selected_classifier = choose_prompt_only_qwen_classifier(verilog_code, qsf_text=qsf_text, filename=filename)
    else:
        local_classifier = classify_fpga_board(verilog_code, qsf_text=qsf_text, filename=filename)
        selected_classifier = choose_classifier_with_ollama_qwen(local_classifier, verilog_code, qsf_text=qsf_text, filename=filename)
    return response(ok(classifier=selected_classifier, local_classifier=local_classifier, ai_provider=selected_classifier.get("provider", "local_deterministic"), ollama_qwen_used=bool(selected_classifier.get("ollama_qwen_used"))))



@app.get("/ai/ollama_status")
def api_ollama_status():
    q = ollama_qwen_config()
    if not q.get("enabled"):
        return response(ok(enabled=False, provider=q.get("provider"), message="Ollama Qwen disabled in config_pi_hat.json"))
    url = str(q.get("base_url", "http://127.0.0.1:11434")).rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=min(float(q.get("timeout_seconds", 15) or 15), 5.0)) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        models = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
        return response(ok(enabled=True, reachable=True, model=q.get("model"), model_installed=q.get("model") in models, models=models, base_url=q.get("base_url")))
    except Exception as e:
        return response(ok(enabled=True, reachable=False, model=q.get("model"), base_url=q.get("base_url"), error=str(e)))

@app.post("/ai/select_board")
def api_select_board():
    data = request.get_json(silent=True) or {}
    verilog_code = data.get("verilog_code", "")
    verilog_path = (data.get("verilog_path") or "").strip()
    filename = data.get("filename", "uploaded.v")
    requested_board = data.get("requested_board") or None
    force_refresh = bool(data.get("force_refresh", True))
    qsf_text = data.get("qsf_text", "") or ""
    qsf_path = (data.get("qsf_path") or "").strip()
    if qsf_path and not qsf_text.strip():
        try:
            if remote_path_allowed(qsf_path) and remote_file_exists(qsf_path):
                qsf_text = read_remote_text(qsf_path)
        except Exception:
            qsf_text = ""
    if verilog_path and not verilog_code.strip():
        try:
            verilog_code = read_remote_text(verilog_path)
            filename = Path(verilog_path).name
        except Exception as e:
            return response(fail(str(e), verilog_path=verilog_path), 400)
    if not verilog_code.strip():
        return response(fail("verilog_code or verilog_path is required"), 400)
    return response(select_board_ai(verilog_code, filename, requested_board=requested_board, force_refresh=force_refresh, qsf_text=qsf_text))



# Direct /deploy endpoint removed in v3.67. Use /queue/deploy so all programming goes through the fair queue.


# Manual /board/<board>/<action> API removed in v3.67.

@app.post("/jtag/instance/action")
def api_jtag_instance_action():
    data = request.get_json(silent=True) if request.is_json else {}
    data = data or request.form.to_dict() or {}
    board = (data.get("board") or "").strip()
    cable = (data.get("detected_cable") or data.get("cable") or data.get("jtag_cable") or "").strip()
    action = (data.get("action") or "").strip().lower()
    if not board or not cable:
        return response(fail("board and detected_cable are required."), 400)
    if board not in load_config().get("board_catalog", {}):
        return response(fail(f"Unknown board: {board}", board=board), 404)

    key = instance_lock_key(board, cable)

    if action in ("lock", "manual_lock", "release", "unlock", "manual_release"):
        return response(fail("Manual Lock/Release has been removed. Use Disable/Enable for admin control; programming/test timers still reserve boards automatically."), 410)

    if action in ("disable", "disabled"):
        state = load_state()
        state.setdefault("disabled_jtag", {})[key] = {
            "board": board,
            "detected_cable": cable,
            "lock_key": key,
            "disabled_at": now_iso(),
            "disabled_ts": time.time(),
            "reason": data.get("reason", "manual_terminal_disable"),
        }
        requeued = requeue_jobs_assigned_to_disabled_slots(state, reason="manual_jtag_disable")
        state = annotate_queue_assignments(state)
        save_state(state)
        auto_repair = automatic_queue_repair_once("jtag_disable_auto_repair", force_plan=True)
        state_after_disable = load_state()
        testing_preserved = any(
            isinstance(j, dict)
            and str(j.get("status") or "").lower() == "testing"
            and (j.get("selected_lock_key") == key or j.get("planned_lock_key") == key or instance_lock_key(str(j.get("selected_board") or j.get("ai_selected_board") or j.get("planned_board") or board), str(j.get("jtag_cable") or j.get("selected_jtag_cable") or j.get("planned_jtag_cable") or cable)) == key)
            for j in (state_after_disable.get("jobs", {}) or {}).values()
        )
        add_history("disable_jtag_instance", board, {"detected_cable": cable, "lock_key": key, "requeued_jobs": bool(requeued), "testing_preserved": bool(testing_preserved), "auto_repair": auto_repair, "effect": "disabled_from_ai_and_fifo_selection_testing_sessions_continue"})
        return response(ok(board=board, detected_cable=cable, lock_key=key, enabled=False, manual_disabled=True, effect="Disabled: AI/FIFO will not select this JTAG slot for new queued/running jobs. If a job is already testing here, its reserved timer continues until it finishes or is cancelled.", requeued_jobs=bool(requeued), testing_preserved=bool(testing_preserved), auto_repair=auto_repair, queue=queue_snapshot(fast=True)))

    if action in ("enable", "enabled"):
        state = load_state()
        removed = state.setdefault("disabled_jtag", {}).pop(key, None)
        state = annotate_queue_assignments(state)
        save_state(state)
        auto_repair = automatic_queue_repair_once("jtag_enable_auto_repair", force_plan=True)
        add_history("enable_jtag_instance", board, {"detected_cable": cable, "lock_key": key, "was_disabled": bool(removed), "auto_repair": auto_repair, "effect": "enabled_for_ai_and_fifo_selection"})
        return response(ok(board=board, detected_cable=cable, lock_key=key, enabled=True, manual_disabled=False, effect="Enabled: AI and FIFO queue may select this physical JTAG slot again when it is free.", auto_repair=auto_repair, queue=queue_snapshot(fast=True)))

    return response(fail(f"Unknown JTAG instance action: {action}"), 400)


@app.get("/server/projects")
def api_server_projects():
    family = request.args.get("family", "standard")
    limit = request.args.get("limit", "50")
    return response(find_server_projects(limit=limit, family=family))



# /history API removed in v3.67 because the GUI does not use it.


def diagnostics_snapshot() -> Dict[str, Any]:
    state = load_state()
    jobs = state.get("jobs", {}) or {}
    active = [jid for jid, job in jobs.items() if isinstance(job, dict) and str(job.get("status") or "").lower() in ("running", "testing")]
    receiving = [jid for jid, job in jobs.items() if isinstance(job, dict) and str(job.get("status") or "").lower() in ("receiving", "uploading")]
    queued = [jid for jid, job in jobs.items() if isinstance(job, dict) and str(job.get("status") or "").lower() == "queued"]

    slot_owners: Dict[str, List[str]] = {}
    for jid in active:
        key = job_slot_lock_key(jobs.get(jid, {}))
        if key:
            slot_owners.setdefault(key, []).append(jid)
    duplicate_slot_owners = {k: v for k, v in slot_owners.items() if len(v) > 1}

    now_ts = time.time()
    stuck_running = []
    expired_uploads = []
    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").lower()
        if status == "running":
            started = float(job.get("started_ts", 0) or 0)
            expected = int(job.get("program_estimated_seconds", job.get("estimated_seconds", 0)) or 0)
            if started and expected and now_ts - started > expected + strict_running_watchdog_grace_seconds():
                stuck_running.append(jid)
        if status in ("receiving", "uploading"):
            deadline = float(job.get("receive_deadline_ts", 0) or 0)
            if deadline and now_ts > deadline:
                expired_uploads.append(jid)

    return ok(
        timestamp=now_iso(),
        traffic=traffic_snapshot(),
        queue_worker={
            "started": bool(QUEUE_WORKER_STARTED),
            "heartbeat_age_seconds": max(0, int(now_ts - float(QUEUE_WORKER_HEARTBEAT_TS or now_ts))),
            "runner_threads": _running_thread_count() if "_running_thread_count" in globals() else 0,
        },
        queue_counts={
            "queued": len(queued),
            "receiving_or_uploading": len(receiving),
            "active_running_or_testing": len(active),
            "known_jobs": len(jobs),
        },
        duplicate_slot_owners=duplicate_slot_owners,
        stuck_running_jobs=stuck_running,
        expired_upload_jobs=expired_uploads,
        strict_resource_engine=load_config().get("strict_resource_engine", {}),
        dynamic_scaling=adaptive_runtime_config(refresh=False),
    )


@app.get("/diagnostics/load")
def api_diagnostics_load():
    return response(diagnostics_snapshot())


@app.get("/diagnostics/dynamic_config")
def api_diagnostics_dynamic_config():
    force = str(request.args.get("force", "0")).lower() in ("1", "true", "yes", "force")
    jtag_data = None
    if force:
        try:
            jtag_data = discover_jtag(force=True)
        except Exception as e:
            jtag_data = {"success": False, "cables": [], "errors": [{"error": str(e)}], "timestamp": now_iso()}
    return response(ok(dynamic_scaling=adaptive_runtime_config(refresh=force, jtag_data=jtag_data, source="diagnostics_dynamic_config")))


@app.post("/diagnostics/audit_now")
def api_diagnostics_audit_now():
    state = load_state()
    changed = False
    if recover_stuck_upload_jobs(state):
        changed = True
    if recover_orphan_uploaded_jobs(state):
        changed = True
    if recover_stuck_running_jobs(state):
        changed = True
    if requeue_jobs_assigned_to_disabled_slots(state, reason="repair_now_disabled_slot"):
        changed = True
    if repair_active_slot_conflicts(state):
        changed = True
    if strict_resource_reconcile(state, reason="diagnostics_audit_now"):
        changed = True
    if changed:
        state = annotate_queue_assignments(state)
        save_state(state)
    data = diagnostics_snapshot()
    data["repaired"] = bool(changed)
    return response(data)


@app.get("/queue")
def api_queue():
    # Fast default for live GUI polling. v4.28 also kicks the queue worker if a
    # queued row is ready with Wait=00:00 but no runner is active.
    force = str(request.args.get("force", "0")).lower() in ("1", "true", "yes", "force")
    try:
        ensure_queue_worker(force_restart_if_stalled=True)
    except Exception:
        pass
    repair = automatic_queue_repair_once("api_queue_auto_repair", force_plan=bool(force))
    dispatch_result = {"success": True, "started_count": 0, "started_job_ids": []}
    try:
        if pending_queued_job_count() > 0 and _running_thread_count() == 0:
            ensure_queue_worker(force_restart_if_stalled=True)
            dispatch_result = dispatch_ready_queued_jobs_once("api_queue_visible_dispatch")
    except Exception as dispatch_error:
        dispatch_result = {"success": False, "error": str(dispatch_error)}
    data = queue_snapshot(fast=not force)
    data["api_queue_auto_repair"] = repair
    data["api_queue_dispatch"] = dispatch_result
    data["jtag_usage"] = load_state().get("jtag_usage", {})
    dyn = adaptive_runtime_config(refresh=False)
    data["dynamic_scaling"] = {
        "enabled": bool(dyn.get("enabled", False)),
        "limits": dyn.get("limits", {}),
        "slot_summary": dyn.get("slot_summary", {}),
        "warnings": dyn.get("warnings", []),
    }
    return response(data)


@app.post("/queue/stage_cleanup_now")
def api_queue_stage_cleanup_now():
    """Run temporary queued-job stage cleanup on demand. Normal operation also runs hourly."""
    data = cleanup_old_temporary_stage_cache("manual_stage_cleanup_now")
    return response(data)


@app.post("/diagnostics/cleanup_temp")
def api_cleanup_temp_files():
    require_pi_key()
    stage = cleanup_old_temporary_stage_cache("manual_diagnostics_cleanup_temp")
    state_tmp = cleanup_orphan_board_state_temp_files(0, "manual_diagnostics_cleanup_temp")
    return response(ok(stage_cleanup=stage, board_state_temp_cleanup=state_tmp))


@app.post("/queue/kick_now")
def api_queue_kick_now():
    require_pi_key()
    ensure_queue_worker(force_restart_if_stalled=True)
    wake_queue_worker("manual_queue_kick_now")
    repair = automatic_queue_repair_once("manual_queue_kick_now", force_plan=True)
    return response(ok(kicked=True, repair=repair, queue=queue_snapshot(fast=True)))


def queue_stream_config() -> Dict[str, Any]:
    cfg = load_config()
    s = (cfg.get("realtime_stream", {}) or {})
    def fval(name: str, default: float) -> float:
        try:
            return float(s.get(name, default) or default)
        except Exception:
            return float(default)
    return {
        "enabled": bool(s.get("enabled", True)),
        "queue_interval_seconds": max(0.02, min(fval("queue_interval_seconds", 0.05), 10.0)),
        "broadcast_cache_seconds": max(0.02, min(fval("broadcast_cache_seconds", 0.05), 10.0)),
        "client_heartbeat_seconds": max(1.0, min(fval("client_heartbeat_seconds", 5.0), 60.0)),
        "send_only_when_changed": bool(s.get("send_only_when_changed", False)),
    }


def update_queue_stream_broadcast_once(reason: str = "queue_stream_broadcast") -> Dict[str, Any]:
    try:
        # v5.07: live queue stream is often the only thing polling after upload.
        # If a valid queued job is waiting and no runner exists, start the normal
        # FIFO dispatch path here too.  This is automatic runtime behavior, not a
        # user-facing repair tool.
        dispatch_result = {"success": True, "started_count": 0, "started_job_ids": []}
        try:
            if pending_queued_job_count() > 0 and _running_thread_count() == 0:
                ensure_queue_worker(force_restart_if_stalled=True)
                dispatch_result = dispatch_ready_queued_jobs_once("queue_stream_auto_dispatch")
        except Exception as dispatch_error:
            dispatch_result = {"success": False, "error": str(dispatch_error), "exception_type": type(dispatch_error).__name__}
        data = queue_snapshot(fast=True)
        data["queue_stream_auto_dispatch"] = dispatch_result
        emit_ts = time.time()
        emit_ns = time.time_ns()
        with QUEUE_STREAM_BROADCAST_LOCK:
            wake_seq = int(QUEUE_STREAM_BROADCAST.get("state_change_sequence", QUEUE_STATE_CHANGE_SEQUENCE) or 0)
            last_wake_reason = str(QUEUE_STREAM_BROADCAST.get("last_wake_reason") or "")
            last_wake_ns = int(QUEUE_STREAM_BROADCAST.get("last_wake_ns", 0) or 0)
        data["stream"] = True
        data["stream_ts"] = emit_ts
        data["stream_ns"] = emit_ns
        data["traffic"] = traffic_snapshot()
        data["broadcast_reason"] = reason
        data["sync"] = {
            "mode": "event_driven_low_latency",
            "server_emit_ts": emit_ts,
            "server_emit_ns": emit_ns,
            "state_change_sequence": wake_seq,
            "last_wake_reason": last_wake_reason,
            "last_wake_ns": last_wake_ns,
        }
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()
        with QUEUE_STREAM_BROADCAST_LOCK:
            old_hash = str(QUEUE_STREAM_BROADCAST.get("hash") or "")
            seq = int(QUEUE_STREAM_BROADCAST.get("sequence", 0) or 0)
            if digest != old_hash:
                seq += 1
            QUEUE_STREAM_BROADCAST.update({
                "payload": payload,
                "updated_ts": time.time(),
                "updated_at": now_iso(),
                "hash": digest,
                "sequence": seq,
                "error": "",
            })
        return ok(updated=True, hash=digest, sequence=seq)
    except Exception as e:
        err = json.dumps({"success": False, "error": str(e), "stream": True, "timestamp": now_iso()}, ensure_ascii=False, separators=(",", ":"))
        with QUEUE_STREAM_BROADCAST_LOCK:
            QUEUE_STREAM_BROADCAST.update({"payload": err, "updated_ts": time.time(), "updated_at": now_iso(), "error": str(e)})
        return fail("queue stream broadcast update failed", error=str(e))


def queue_stream_broadcast_loop() -> None:
    while True:
        try:
            cfg = queue_stream_config()
            update_queue_stream_broadcast_once("event_driven_broadcast_loop")
            wait_s = float(cfg.get("broadcast_cache_seconds", 0.05) or 0.05)
            QUEUE_STREAM_WAKE_EVENT.wait(wait_s)
            QUEUE_STREAM_WAKE_EVENT.clear()
        except Exception:
            time.sleep(0.5)


def ensure_queue_stream_broadcaster() -> None:
    global QUEUE_STREAM_BROADCAST_THREAD, QUEUE_STREAM_BROADCAST_STARTED
    if QUEUE_STREAM_BROADCAST_THREAD and QUEUE_STREAM_BROADCAST_THREAD.is_alive():
        QUEUE_STREAM_BROADCAST_STARTED = True
        return
    t = threading.Thread(target=queue_stream_broadcast_loop, daemon=True, name="queue_stream_broadcast_cache")
    QUEUE_STREAM_BROADCAST_THREAD = t
    QUEUE_STREAM_BROADCAST_STARTED = True
    t.start()


def get_queue_stream_broadcast_payload(force_if_empty: bool = True) -> Tuple[str, int, str]:
    with QUEUE_STREAM_BROADCAST_LOCK:
        payload = str(QUEUE_STREAM_BROADCAST.get("payload") or "")
        seq = int(QUEUE_STREAM_BROADCAST.get("sequence", 0) or 0)
        digest = str(QUEUE_STREAM_BROADCAST.get("hash") or "")
    if force_if_empty and not payload:
        update_queue_stream_broadcast_once("first_client_force_fill")
        with QUEUE_STREAM_BROADCAST_LOCK:
            payload = str(QUEUE_STREAM_BROADCAST.get("payload") or "")
            seq = int(QUEUE_STREAM_BROADCAST.get("sequence", 0) or 0)
            digest = str(QUEUE_STREAM_BROADCAST.get("hash") or "")
    return payload, seq, digest




@app.get("/diagnostics/classroom_load")
def api_diagnostics_classroom_load():
    """Show runtime load limits and fair-share status for classroom use."""
    return response(ok(
        traffic=traffic_snapshot(),
        traffic_control=traffic_cfg(),
        dynamic_runtime=adaptive_runtime_config(refresh=False),
        fair_share=fair_share_config(),
        stream_broadcast={k: v for k, v in QUEUE_STREAM_BROADCAST.items() if k != "payload"},
        queue=queue_snapshot(fast=True),
    ))


@app.get("/stream/queue")
def api_stream_queue():
    """
    Classroom-scale realtime queue stream.

    v4.30: one broadcaster computes queue_snapshot(fast=True), and all connected
    student GUIs reuse that payload. 50 students no longer create 50 state-file
    reads every tick.
    """
    ok_stream, snap = acquire_stream_client_slot()
    if not ok_stream:
        resp, status = response(fail(
            "Server busy: too many live queue streams. Retry shortly.",
            retry_after_seconds=5,
            traffic=snap,
        ), 429)
        try:
            resp.headers["Retry-After"] = "5"
        except Exception:
            pass
        return resp, status

    cfg = queue_stream_config()
    interval = float(cfg.get("queue_interval_seconds", 1.0) or 1.0)
    heartbeat = float(cfg.get("client_heartbeat_seconds", 15.0) or 15.0)
    send_only_changed = bool(cfg.get("send_only_when_changed", False))
    ensure_queue_stream_broadcaster()

    @stream_with_context
    def generate():
        last_seq = -1
        last_heartbeat = 0.0
        try:
            while True:
                try:
                    payload, seq, _digest = get_queue_stream_broadcast_payload(force_if_empty=True)
                    now_ts = time.time()
                    if payload and ((not send_only_changed) or seq != last_seq):
                        yield "event: queue\n"
                        yield "data: " + payload + "\n\n"
                        last_seq = seq
                        last_heartbeat = now_ts
                    elif now_ts - last_heartbeat >= heartbeat:
                        # Lightweight heartbeat keeps proxy/requests from timing out without
                        # recomputing queue state for every student.
                        yield ": keepalive " + str(int(now_ts)) + "\n\n"
                        last_heartbeat = now_ts
                except GeneratorExit:
                    break
                except Exception as e:
                    err = json.dumps({"success": False, "error": str(e), "stream": True, "timestamp": now_iso()}, ensure_ascii=False)
                    yield "event: error\n"
                    yield "data: " + err + "\n\n"
                time.sleep(interval)
        finally:
            release_stream_client_slot()

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "X-UADY-Sync-Mode": "event-driven-low-latency",
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


def cancel_queue_job_internal(job_id: str, data: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Cancel one queue/testing job without making the Flask UI wait on multiple requests."""
    state = load_state()
    jobs = state.setdefault("jobs", {})
    if job_id not in jobs:
        return fail("Unknown queue job", job_id=job_id), 404

    job = jobs[job_id]
    if not bool(data.get("explicit_cancel")):
        return fail("Cancel denied. Cancellation requires an explicit Cancel button or batch command.", job_id=job_id), 400

    provided_token = (data.get("cancel_token") or "").strip()
    requester_student = (data.get("student") or data.get("user") or "").strip()
    requester_host = (data.get("client_hostname") or "").strip()
    requester_ip = (data.get("student_ip") or request.remote_addr or "").strip()

    job_token = (job.get("cancel_token") or "").strip()
    owner_match = queue_cancel_owner_matches(job, data)
    token_match = bool(job_token and provided_token and provided_token == job_token)
    if job_token and not token_match and not owner_match:
        return fail(
            "Cancel denied. Only the GUI/user that created this job can cancel it.",
            job_id=job_id,
            creator=job.get("student") or job.get("client_hostname") or "unknown",
            requester=requester_student or requester_host or requester_ip or "unknown",
            cancel_owner_check="token_or_same_student_host_ip_required"
        ), 403
    if not job_token and not owner_match:
        return fail("Cancel denied. This job was created by another user.", job_id=job_id), 403
    if owner_match and not token_match:
        job["cancel_owner_fallback_used"] = True
        job["cancel_owner_fallback_at"] = now_iso()

    status = str(job.get("status") or "").lower()
    if status in ("completed", "cancelled", "failed"):
        return fail(f"Cancel disabled. Job is already {status}.", job_id=job_id, status=status), 400

    # v4.23: some jobs were visually/logically in testing but still had a stale
    # status=running row. Treat those as testing so the creator can cancel the
    # reserved timer and hand the slot to the next queued job.
    if job_is_testing_like(job, state):
        status = "testing"
        job["status"] = "testing"
        jobs[job_id] = job

    if status in ("receiving", "uploading"):
        job["status"] = "cancelled"
        job = mark_job_history_final(job, "job_cancelled")
        job["message"] = f"cancelled during upload by creator: {requester_student or requester_host or requester_ip or 'unknown'}"
        job["finished_at"] = now_iso()
        job["finished_ts"] = time.time()
        try:
            job["server_history"] = write_job_history_to_server(job, event="job_cancelled")
        except Exception as e:
            job["server_history"] = fail(f"server history logging failed: {e}")
        job["finished_job_temp_cleanup"] = cleanup_finished_job_temp_files(job, "cleanup_after_cancelled_upload")
        jobs[job_id] = job
        state["queue"] = [q for q in state.get("queue", []) if q != job_id]
        _record_recent_job(state, job_id)
        save_state(state)
        return ok(job_id=job_id, status="cancelled", job=public_queue_job(job), queue_plan=state.get("queue_plan", {})), 200

    if status == "running" or state.get("current_job") == job_id:
        # v4.23: allow the creator to cancel a running row. We send a best-effort
        # remote kill to the Quartus server, release/quarantine the logical slot,
        # and ignore any late runner result by advancing the generation marker.
        board = job.get("selected_board") or job.get("ai_selected_board") or job.get("planned_board") or ""
        cable = job.get("jtag_cable") or job.get("selected_jtag_cable") or job.get("planned_jtag_cable") or ""
        kill_result = kill_remote_quartus_for_job(job_id)
        if board and cable:
            try:
                quarantine_old_runner_slot(state, board, cable, job_id, reason="running_cancelled_by_creator")
            except Exception:
                clear_board_slot_state(state, board, cable, reason="running_cancelled_by_creator", job_id=job_id)
        job["status"] = "cancelled"
        job = mark_job_history_final(job, "running_cancelled_by_creator")
        job["cancelled_at"] = now_iso()
        job["finished_at"] = job["cancelled_at"]
        job["finished_ts"] = time.time()
        job["cancel_requested"] = True
        job["abandoned_run_generation"] = int(job.get("run_generation", 0) or 0)
        job["message"] = f"cancelled by creator during running; remote Quartus stop sent; slot will not accept late result"
        job["remote_quartus_cancel"] = kill_result
        try:
            job["server_history"] = write_job_history_to_server(job, event="running_cancelled_by_creator")
        except Exception as e:
            job["server_history"] = fail(f"server history logging failed: {e}")
        job["finished_job_temp_cleanup"] = cleanup_finished_job_temp_files(job, "cleanup_after_running_cancelled")
        jobs[job_id] = job
        if state.get("current_job") == job_id:
            state["current_job"] = None
        state["queue"] = [q for q in state.get("queue", []) if q != job_id]
        _record_recent_job(state, job_id)
        state = annotate_queue_assignments(state)
        save_state(state)
        with QUEUE_JOB_THREADS_LOCK:
            QUEUE_JOB_THREADS.pop(str(job_id), None)
        try:
            ensure_queue_worker()
            auto_repair = automatic_queue_repair_once("running_cancelled_auto_start_next", force_plan=True)
        except Exception as e:
            auto_repair = {"success": False, "error": str(e)}
        return ok(job_id=job_id, status="cancelled", job=public_queue_job(job), remote_quartus_cancel=kill_result, auto_repair=auto_repair, queue=queue_snapshot(fast=True)), 200

    if status == "testing":
        board = job.get("selected_board") or job.get("ai_selected_board") or ""
        cable = job.get("jtag_cable") or job.get("selected_jtag_cable") or ""

        rel = release_instance(board, cable, reason="testing_cancelled_by_creator") if board and cable else {}
        state = load_state()
        jobs = state.setdefault("jobs", {})
        job = jobs.get(job_id, job)

        job["status"] = "cancelled"
        job = mark_job_history_final(job, "testing_cancelled_by_creator")
        job["cancelled_at"] = now_iso()
        job["finished_at"] = job["cancelled_at"]
        job["finished_ts"] = time.time()
        job["test_finished_at"] = job["finished_at"]
        job["test_finished_ts"] = job["finished_ts"]
        job["message"] = f"cancelled by creator during testing; slot cleared and released {job.get('jtag_instance') or 'JTAG'} | {cable}"
        job["slot_cleared_at"] = job["finished_at"]
        job["cleared_reason"] = "testing_cancelled_by_creator"
        job["release_result"] = rel

        if board and cable:
            lock_key = instance_lock_key(board, cable)
            state.setdefault("locks", {})[lock_key] = {
                "busy": False,
                "released_at": time.time(),
                "released_at_iso": now_iso(),
                "reason": "testing_cancelled_by_creator",
                "board": board,
                "detected_cable": cable,
                "lock_key": lock_key,
                "cleared": True,
                "clear_mode": "cancel_logical_clear",
            }

        try:
            job["server_history"] = write_job_history_to_server(job, event="testing_cancelled_by_creator")
        except Exception as e:
            job["server_history"] = fail(f"server history logging failed: {e}")
        job["finished_job_temp_cleanup"] = cleanup_finished_job_temp_files(job, "cleanup_after_testing_cancelled")

        jobs[job_id] = job
        if state.get("current_job") == job_id:
            state["current_job"] = None
        state["queue"] = [q for q in state.get("queue", []) if q != job_id]
        _record_recent_job(state, job_id)
        state = annotate_queue_assignments(state)
        save_state(state)
        try:
            ensure_queue_worker()
            auto_repair = automatic_queue_repair_once("testing_cancelled_auto_start_next", force_plan=True)
        except Exception as e:
            auto_repair = {"success": False, "error": str(e)}
        return ok(job_id=job_id, status="cancelled", released=rel, job=public_queue_job(job), queue_plan=state.get("queue_plan", {}), auto_repair=auto_repair, queue=queue_snapshot(fast=True)), 200

    if status not in ("queued", "pending", "receiving", "uploading"):
        return fail("Only queued, uploading, receiving, or testing jobs can be cancelled.", job_id=job_id, status=job.get("status")), 400

    # v4.07: allow users to cancel an upload/archive job and remove temporary Pi spool.
    try:
        sd = Path(str(job.get("spool_dir") or ""))
        if sd.exists() and sd.is_dir():
            shutil.rmtree(sd)
    except Exception:
        pass

    job["status"] = "cancelled"
    job = mark_job_history_final(job, "job_cancelled")
    job["message"] = f"cancelled by creator: {requester_student or requester_host or requester_ip or 'unknown'}"
    job["finished_at"] = now_iso()
    job["finished_ts"] = time.time()
    try:
        job["server_history"] = write_job_history_to_server(job, event="job_cancelled")
    except Exception as e:
        job["server_history"] = fail(f"server history logging failed: {e}")
    job["finished_job_temp_cleanup"] = cleanup_finished_job_temp_files(job, "cleanup_after_cancelled")
    jobs[job_id] = job
    state["queue"] = [q for q in state.get("queue", []) if q != job_id]
    _record_recent_job(state, job_id)
    state = annotate_queue_assignments(state)
    save_state(state)
    add_history("queue_cancel", job.get("requested_board") or "", {"job_id": job_id, "requester": requester_student or requester_host or requester_ip})
    return ok(job_id=job_id, status="cancelled", job=public_queue_job(job), queue_plan=state.get("queue_plan", {})), 200


def queue_cancel_owner_matches(job: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """Return True when the requester clearly matches the job creator.

    v4.25: the GUI token can be lost when the user runs a newly extracted GUI
    folder, restarts the GUI, or the job came from a previous version.  The backend
    still keeps student/client_hostname/student_ip, so a same-owner match can
    safely allow cancellation instead of saying "you did not create this job".
    """
    requester_student = str(data.get("student") or data.get("user") or "").strip().lower()
    requester_host = str(data.get("client_hostname") or "").strip().lower()
    requester_ip = str(data.get("student_ip") or getattr(request, "remote_addr", "") or "").strip().lower()
    requester_token_hash = hash_client_token(data.get("client_token") or data.get("gui_client_token"))
    creator_token_hash = str(job.get("owner_token_hash") or "").strip()
    if requester_token_hash and creator_token_hash and secrets.compare_digest(requester_token_hash, creator_token_hash):
        return True
    creator_student = str(job.get("student") or "").strip().lower()
    creator_host = str(job.get("client_hostname") or "").strip().lower()
    creator_ip = str(job.get("student_ip") or "").strip().lower()
    if requester_student and creator_student and requester_student == creator_student:
        return True
    if requester_host and creator_host and requester_host == creator_host:
        return True
    if requester_ip and creator_ip and requester_ip == creator_ip:
        return True
    return False


@app.post("/queue/<job_id>/cancel")
def api_queue_cancel(job_id: str):
    data = request.get_json(silent=True) if request.is_json else {}
    data = data or request.form.to_dict() or {}
    result, status_code = cancel_queue_job_internal(job_id, data)
    if isinstance(result, dict) and result.get("success"):
        wake_queue_worker("queue_cancel")
    return response(result, status_code)


@app.post("/queue/cancel_batch")
def api_queue_cancel_batch():
    data = request.get_json(silent=True) if request.is_json else {}
    data = data or {}
    if not bool(data.get("explicit_cancel")):
        return response(fail("Cancel denied. Batch cancellation requires explicit_cancel=True."), 400)

    raw_jobs = data.get("jobs", []) or []
    if not isinstance(raw_jobs, list) or not raw_jobs:
        return response(fail("Batch cancel requires a non-empty jobs list."), 400)

    results = []
    cancelled = 0
    failed_count = 0
    for item in raw_jobs:
        if not isinstance(item, dict):
            failed_count += 1
            results.append(fail("Invalid batch item. Expected object."))
            continue
        jid = str(item.get("job_id") or "").strip()
        if not jid:
            failed_count += 1
            results.append(fail("Missing job_id in batch item."))
            continue

        merged = dict(data)
        merged.update(item)
        merged["explicit_cancel"] = True
        result, status_code = cancel_queue_job_internal(jid, merged)
        result["http_status"] = status_code
        result["job_id"] = jid
        if result.get("success"):
            cancelled += 1
        else:
            failed_count += 1
        results.append(result)

    # Return fresh queue snapshot so GUI can update after one network request.
    snapshot = {}
    try:
        snapshot = queue_snapshot()
    except Exception as e:
        snapshot = fail(f"Unable to refresh queue snapshot after batch cancel: {e}")

    if cancelled:
        wake_queue_worker("queue_cancel_batch")
    return response(ok(cancelled=cancelled, failed=failed_count, results=results, queue=snapshot))


def make_queue_job_metadata(data: Dict[str, Any], job_id: str, default_kind: str = "upload") -> Dict[str, Any]:
    """Build shared queue job metadata from JSON/form input."""
    requested_board = data.get("requested_board") or None
    client_hostname = data.get("client_hostname") or "unknown"
    student_ip = data.get("student_ip") or request.remote_addr or "unknown"
    priority_role = data.get("priority_label") or data.get("priority_role") or data.get("priority", "Student")
    priority = priority_value_from_role(priority_role)
    priority_label = priority_label_from_value(priority_role)
    student = data.get("student") or data.get("user") or client_hostname or student_ip
    major = data.get("major") or data.get("student_major") or data.get("carrera") or ""
    test_minutes = sanitize_test_minutes(data.get("test_minutes", default_test_minutes()))
    test_seconds = test_minutes * 60
    filename = data.get("filename") or data.get("verilog_filename") or "uploaded.v"

    return {
        "job_id": job_id,
        "status": data.get("status") or "queued",
        "kind": data.get("kind") or default_kind,
        "filename": filename,
        "requested_board": requested_board,
        "client_hostname": client_hostname,
        "student_ip": student_ip,
        "created_at": data.get("created_at") or now_iso(),
        "created_ts": float(data.get("created_ts", 0) or time.time()),
        "message": data.get("message") or "queued",
        "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds),
        "test_minutes": test_minutes,
        "test_seconds": test_seconds,
        "priority": priority,
        "priority_label": priority_label,
        "priority_role": priority_label,
        "student": student,
        "major": major,
        "submission_signature": data.get("submission_signature") or data.get("file_pair_signature") or "",
        "verilog_file_signature": data.get("verilog_file_signature") or "",
        "sof_file_signature": data.get("sof_file_signature") or "",
        "qsf_file_signature": data.get("qsf_file_signature") or "",
        "verilog_client_path": data.get("verilog_client_path") or "",
        "sof_client_path": data.get("sof_client_path") or "",
        "qsf_client_path": data.get("qsf_client_path") or "",
        "verilog_client_mtime_ns": data.get("verilog_client_mtime_ns") or "",
        "sof_client_mtime_ns": data.get("sof_client_mtime_ns") or "",
        "qsf_client_mtime_ns": data.get("qsf_client_mtime_ns") or "",
        "verilog_client_size": data.get("verilog_client_size") or "",
        "sof_client_size": data.get("sof_client_size") or "",
        "qsf_client_size": data.get("qsf_client_size") or "",
        "qsf_path": data.get("qsf_path") or "",
        "qsf_filename": data.get("qsf_filename") or "",
        "qsf_text": data.get("qsf_text") or "",
        "source_mode": data.get("source_mode") or data.get("submit_mode") or data.get("origin") or "",
        "submit_mode": data.get("submit_mode") or data.get("source_mode") or data.get("origin") or "",
        "owner_token_hash": hash_client_token(data.get("client_token") or data.get("gui_client_token")),
        "cancel_token": data.get("cancel_token") or uuid.uuid4().hex,
    }


def maybe_recover_stuck_upload_jobs_for_prequeue(state: Dict[str, Any], min_gap_seconds: float = 1.0) -> bool:
    """Rate-limited recovery sweep used by prequeue admission.

    Global recovery can be O(number_of_jobs). Running it for every simultaneous
    classroom upload increases p95 latency. The automatic queue maintenance worker still
    runs continuously; this path only gives prequeue an occasional immediate
    sweep.
    """
    global LAST_PREQUEUE_RECOVERY_TS
    now_ts = time.time()
    with PREQUEUE_RECOVERY_LOCK:
        if now_ts - float(LAST_PREQUEUE_RECOVERY_TS or 0.0) < max(0.1, float(min_gap_seconds or 1.0)):
            return False
        LAST_PREQUEUE_RECOVERY_TS = now_ts
    try:
        return bool(recover_stuck_upload_jobs(state))
    except Exception:
        return False


def prequeue_upload_job(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Register a job immediately before uploading large files.

    v3.93: this operation is now atomic. Under a 100-job stress test, separate
    load_state()/save_state() calls could overwrite each other and make some
    returned job IDs disappear before cleanup.
    """
    job_id = data.get("job_id") or uuid.uuid4().hex[:10]

    def mutate(state: Dict[str, Any]):
        cleanup_daily_state_rollover_in_state(state)
        jobs = state.setdefault("jobs", {})
        if job_id in jobs:
            return fail("Job ID already exists", job_id=job_id)

        job = make_queue_job_metadata(data, job_id, default_kind=data.get("kind") or "upload")
        attach_submission_signature(job, data)
        try:
            cleaned = cleanup_stale_upload_blockers_for_owner(state, job, reason="prequeue_before_duplicate_check")
            if cleaned:
                state.setdefault("history", []).append({"time": now_iso(), "event": "stale_upload_blockers_cleaned", "board": "", "details": {"new_job_id": job_id, "cleaned": cleaned}})
        except Exception:
            pass
        # v4.45: do not scan/repair every receiving/uploading job for every
        # simultaneous prequeue request. A rate-limited sweep plus the background
        # queue maintenance worker keeps recovery safe without adding classroom-burst latency.
        maybe_recover_stuck_upload_jobs_for_prequeue(state, min_gap_seconds=1.0)
        duplicate = find_active_duplicate_submission(state, job)
        if duplicate:
            return duplicate_submission_response(duplicate[0], duplicate[1])
        fair_share_block = fair_share_admission_check(state, job)
        if fair_share_block:
            return fair_share_block
        job["status"] = "receiving"
        job["message"] = "receiving files from GUI; waiting for upload to finish"
        job["upload_started_at"] = now_iso()
        job["upload_started_ts"] = time.time()
        job["receive_deadline_ts"] = time.time() + strict_upload_timeout_seconds()
        job["upload_deadline_at"] = iso_from_ts(job["receive_deadline_ts"])
        job["planned_instance_id"] = "uploading"
        job["verilog_filename"] = data.get("verilog_filename") or data.get("filename") or "uploaded.v"
        job["sof_filename"] = data.get("sof_filename") or "uploaded.sof"
        jobs[job_id] = job
        if job_id not in state.setdefault("queue", []):
            state["queue"].append(job_id)

        # Record history inside this same transaction. v3.93 did a second
        # add_history() write after creating every prequeue job; under stress
        # that doubled state-file lock contention and increased p95 latency.
        history = state.setdefault("history", [])
        history.append({"time": now_iso(), "event": "queue_prequeue", "board": "", "details": {"job_id": job_id, "student": data.get("student") or data.get("user") or data.get("client_hostname"), "major": data.get("major") or data.get("student_major") or data.get("carrera") or "", "kind": data.get("kind") or "upload", "source_mode": data.get("source_mode") or data.get("submit_mode") or data.get("origin") or ""}})
        if len(history) > 200:
            del history[:-200]

        return ok(
            job_id=job_id,
            cancel_token=job["cancel_token"],
            status="receiving",
            job=public_queue_job(job),
            queue_length=len(sorted_waiting_display_job_ids(state)),
        )

    result = update_state_atomic(mutate)
    if isinstance(result, dict) and result.get("success"):
        # Create/update the permanent lightweight server record immediately when
        # the GUI creates the queue row. The filename is stable per Job ID, so
        # later upload/testing/cancel/completion events update this same file
        # instead of creating duplicates.
        try:
            hist_cfg = (load_config().get("server_history", {}) or {})
            if bool(hist_cfg.get("record_on_queue_accept", True)):
                jid = str(result.get("job_id") or job_id)
                latest_job = (load_state().get("jobs", {}) or {}).get(jid, {})
                write_job_history_to_server_async(jid, latest_job, "job_prequeued")
        except Exception as e:
            print(f"[HISTORY WARN] prequeue history schedule failed for job={job_id}: {e}", flush=True)
        wake_queue_worker("prequeue_upload_job")
    return result


@app.post("/queue/prequeue_upload")
def api_queue_prequeue_upload():
    data = request.get_json(silent=True) if request.is_json else {}
    data = data or request.form.to_dict() or {}
    return response(prequeue_upload_job(data))


@app.post("/queue/<job_id>/upload_failed")
def api_queue_upload_failed(job_id: str):
    data = request.get_json(silent=True) if request.is_json else {}
    data = data or request.form.to_dict() or {}
    idempotent = bool(data.get("idempotent")) or "stress test cleanup" in str(data.get("reason") or "").lower()

    def mutate(state: Dict[str, Any]):
        job = state.setdefault("jobs", {}).get(job_id)
        if not job:
            if idempotent:
                return ok(job_id=job_id, status="missing", idempotent_cleanup=True, message="job already removed or overwritten before cleanup")
            return fail("Unknown queue job", job_id=job_id)

        current_status = str(job.get("status") or "").lower()
        if current_status not in ("receiving", "uploading"):
            if idempotent:
                return ok(job_id=job_id, status=job.get("status"), idempotent_cleanup=True, message="job was already outside upload state")
            return fail("Job is not waiting for upload", job_id=job_id, status=job.get("status"))

        job["status"] = "failed"
        job = mark_job_history_final(job, "job_failed")
        job["message"] = "upload failed: " + str(data.get("reason") or data.get("error") or "unknown error")
        job["finished_at"] = now_iso()
        job["finished_ts"] = time.time()
        state["jobs"][job_id] = job
        state["queue"] = [jid for jid in state.get("queue", []) if jid != job_id]
        _record_recent_job(state, job_id)
        return ok(job_id=job_id, status="failed", job=public_queue_job(job))

    result = update_state_atomic(mutate)
    if result.get("success"):
        wake_queue_worker("queue_upload_failed")
        return response(result)
    if result.get("error") == "Unknown queue job":
        return response(result, 404)
    return response(result, 400)


@app.post("/queue/upload_failed_batch")
def api_queue_upload_failed_batch():
    """Fast idempotent batch cleanup for stress tests and bulk upload failures.

    One atomic write handles many receiving/uploading jobs. This avoids 100
    separate cleanup requests contending for board_state.json.
    """
    data = request.get_json(silent=True) if request.is_json else {}
    data = data or {}
    raw_ids = data.get("job_ids", []) or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return response(fail("upload_failed_batch requires job_ids list"), 400)
    reason = str(data.get("reason") or "batch upload failed")
    ids = []
    seen = set()
    for jid in raw_ids:
        jid = str(jid or "").strip()
        if jid and jid not in seen:
            ids.append(jid)
            seen.add(jid)

    def mutate(state: Dict[str, Any]):
        jobs = state.setdefault("jobs", {})
        now_txt = now_iso()
        now_ts = time.time()
        failed_ids = []
        skipped = []
        for jid in ids:
            job = jobs.get(jid)
            if not job:
                skipped.append({"job_id": jid, "status": "missing"})
                continue
            status = str(job.get("status") or "").lower()
            if status not in ("receiving", "uploading"):
                skipped.append({"job_id": jid, "status": status})
                continue
            job["status"] = "failed"
            job = mark_job_history_final(job, "job_failed")
            job["message"] = "upload failed: " + reason
            job["finished_at"] = now_txt
            job["finished_ts"] = now_ts
            jobs[jid] = job
            _record_recent_job(state, jid)
            failed_ids.append(jid)
        if failed_ids:
            failed_set = set(failed_ids)
            state["queue"] = [jid for jid in state.get("queue", []) if jid not in failed_set]
        return ok(requested=len(ids), failed_count=len(failed_ids), failed_ids=failed_ids, skipped=skipped[:20])

    result = update_state_atomic(mutate)
    if isinstance(result, dict) and result.get("success"):
        wake_queue_worker("queue_upload_failed_batch")
    return response(result)


@app.post("/queue/<job_id>/upload_files")
def api_queue_upload_files(job_id: str):
    """Attach files to a prequeued job by archiving them directly on the Quartus server.

    v4.11 policy:
    - Do not archive full .v/.sof files before queueing.
    - Save uploads only in a temporary Pi spool.
    - Queue immediately after the upload body is received.
    - When the job runs, copy the .sof to the Quartus server for quartus_pgm.
    - Permanent history is only a small text job record.
    """
    # Snapshot the job, but do not reject queued-before-attach races.
    state = load_state()
    jobs = state.setdefault("jobs", {})
    job = dict(jobs.get(job_id) or {})
    if not job:
        return response(fail("Unknown queue job", job_id=job_id), 404)

    status_l = str(job.get("status") or "").lower()
    already_ok, already_reason = upload_already_accepted_for_programming(job)
    if already_ok:
        return response(ok(
            job_id=job_id,
            status=job.get("status", "queued"),
            idempotent_upload=True,
            upload_already_accepted=True,
            message=already_reason,
            job=public_queue_job(job),
        ))
    if status_l == "queued" and job.get("archived_sof_path") and job.get("archived_verilog_path"):
        return response(ok(
            job_id=job_id,
            status="queued",
            idempotent_upload=True,
            message="upload_files already archived on Quartus server; job is already queued",
            job=public_queue_job(job),
        ))

    allowed_statuses = {"receiving", "uploading", "queued"}
    if status_l not in allowed_statuses:
        return response(fail(
            "Job is not in a state that can accept uploaded files",
            job_id=job_id,
            status=job.get("status"),
            accepted_statuses=sorted(allowed_statuses),
            request_files=list(request.files.keys()),
            request_form_keys=list(request.form.keys()),
        ), 400)

    form_verilog_path = (request.form.get("verilog_path") or job.get("verilog_path") or "").strip()
    form_sof_path = (request.form.get("sof_path") or job.get("sof_path") or "").strip()
    has_verilog_file = "verilog_file" in request.files
    has_sof_file = "sof_file" in request.files

    if not has_verilog_file and not form_verilog_path:
        return response(fail(
            "Upload attach requires verilog_file or verilog_path.",
            job_id=job_id,
            status=job.get("status"),
            request_files=list(request.files.keys()),
            request_form_keys=list(request.form.keys()),
            hint="GUI must send multipart field named verilog_file for local Verilog uploads.",
        ), 400)
    if not has_sof_file and not form_sof_path:
        return response(fail(
            "Upload attach requires sof_file or sof_path.",
            job_id=job_id,
            status=job.get("status"),
            request_files=list(request.files.keys()),
            request_form_keys=list(request.form.keys()),
            hint="GUI must send multipart field named sof_file for local SOF uploads.",
        ), 400)

    # Mark uploading using one atomic transaction. No stale-state writes.
    def mark_uploading(state: Dict[str, Any]):
        j = state.setdefault("jobs", {}).get(job_id)
        if not j:
            return fail("Unknown queue job", job_id=job_id)
        st = str(j.get("status") or "").lower()
        if st not in allowed_statuses:
            return fail("Job changed state before upload could start", job_id=job_id, status=j.get("status"))
        j["status"] = "uploading"
        j["message"] = "receiving .v/.sof into temporary runtime spool"
        j["upload_files_request_started_at"] = now_iso()
        j["upload_files_request_started_ts"] = time.time()
        j["upload_files_in_progress"] = True
        j["receive_deadline_ts"] = time.time() + strict_upload_timeout_seconds()
        j["upload_deadline_at"] = iso_from_ts(j["receive_deadline_ts"])
        j["pi_file_storage"] = False
        j["archive_target"] = history_base_dir()
        state.setdefault("jobs", {})[job_id] = j
        if job_id not in state.setdefault("queue", []):
            state["queue"].append(job_id)
        return ok(job=public_queue_job(j))

    mark = update_state_atomic(mark_uploading)
    if not mark.get("success"):
        code = 404 if mark.get("error") == "Unknown queue job" else 400
        return response(mark, code)
    job = dict(mark.get("job") or job)

    # v4.11: receive the HTTP upload into a temporary Pi spool and queue immediately.
    # Do not archive full .v/.sof files to history. The only permanent record is
    # a small text/JSON job record written on completion/failure.
    if has_verilog_file and has_sof_file:
        spool = save_upload_files_to_temporary_spool(job_id, job)
        if not spool.get("success"):
            def mark_stage_failed(state: Dict[str, Any]):
                j = state.setdefault("jobs", {}).get(job_id, {})
                if j:
                    j["upload_files_in_progress"] = False
                    j["upload_stage"] = "stage_failed_retry_allowed"
                    j["last_upload_error"] = spool.get("error", "temporary spool failed")
                    j["message"] = "temporary upload stage failed; retry allowed"
                    state.setdefault("jobs", {})[job_id] = j
                return spool
            update_state_atomic(mark_stage_failed)
            return response(spool, 400)

        def mark_staged(state: Dict[str, Any]):
            j = state.setdefault("jobs", {}).get(job_id, {})
            if not j:
                return fail("Unknown queue job after temporary upload stage", job_id=job_id)
            j.update({
                "status": "uploading",
                "message": "upload received; archiving to Quartus server in background",
                "upload_stage": "received_temp_spool",
                "upload_files_in_progress": False,
                "spool_dir": spool.get("spool_dir", ""),
                "spool_verilog_path": spool.get("spool_verilog_path", ""),
                "spool_sof_path": spool.get("spool_sof_path", ""),
                "spool_qsf_path": spool.get("spool_qsf_path", ""),
                "verilog_filename": spool.get("verilog_filename", j.get("verilog_filename", "")),
                "sof_filename": spool.get("sof_filename", j.get("sof_filename", "")),
                "qsf_filename": spool.get("qsf_filename", j.get("qsf_filename", "")),
                "verilog_size_bytes": spool.get("verilog_size_bytes", 0),
                "sof_size_bytes": spool.get("sof_size_bytes", 0),
                "qsf_size_bytes": spool.get("qsf_size_bytes", 0),
                "verilog_code": spool.get("verilog_code", j.get("verilog_code", "")),
                "qsf_text": spool.get("qsf_text", j.get("qsf_text", "")),
                "pi_file_storage": False,
                "temporary_pi_spool": True,
                "no_pi_student_file_storage": True,
                "receive_deadline_ts": time.time() + strict_upload_timeout_seconds(),
            })
            j["upload_deadline_at"] = iso_from_ts(j["receive_deadline_ts"])
            j["planned_instance_id"] = "uploading"
            j["remaining_seconds"] = int(strict_upload_timeout_seconds())
            j["wait_seconds"] = 0
            state.setdefault("jobs", {})[job_id] = j
            if job_id not in state.setdefault("queue", []):
                state["queue"].append(job_id)
            history = state.setdefault("history", [])
            history.append({"time": now_iso(), "event": "queue_upload_temp_spooled", "board": j.get("requested_board") or j.get("target_board_hint") or "", "details": {"job_id": job_id, "sof_size_bytes": j.get("sof_size_bytes", 0), "spool_dir": j.get("spool_dir", "")}})
            if len(history) > 200:
                del history[:-200]
            return ok(job=public_queue_job(j))

        staged = update_state_atomic(mark_staged)
        if not staged.get("success"):
            return response(staged, 404)
        final = lightweight_upload_finalize_to_queue(job_id, spool)
        if final.get("success"):
            # v5.07: this is the normal queue path, not a repair button.
            # Once the upload has a valid staged .v/.qsf/.sof package, immediately
            # kick the FIFO dispatcher so a free JTAG slot starts programming now.
            # This prevents the GUI from showing queued/uploading forever with
            # active_runner_threads=0 while boards are available.
            repair_result = {"success": True, "started_count": 0, "started_job_ids": []}
            dispatch_result = {"success": True, "started_count": 0, "started_job_ids": []}
            try:
                repair_result = automatic_queue_repair_once("upload_files_finalized_auto_repair", force_plan=True)
            except Exception as e:
                repair_result = {"success": False, "error": str(e), "exception_type": type(e).__name__}
            try:
                dispatch_result = dispatch_ready_queued_jobs_once("upload_files_finalized_immediate_dispatch")
            except Exception as e:
                dispatch_result = {"success": False, "error": str(e), "exception_type": type(e).__name__}
            wake_queue_worker("upload_files_finalized")
            latest_state = load_state()
            latest_job = latest_state.get("jobs", {}).get(job_id, {})
            return response(ok(
                job_id=job_id,
                cancel_token=final.get("cancel_token", ""),
                status=latest_job.get("status", final.get("status", "queued")),
                lightweight_record_only=True,
                message="upload received; queued and dispatcher kicked automatically",
                job=public_queue_job(latest_job) if latest_job else final.get("job"),
                queue_length=len(sorted_queued_job_ids(latest_state)),
                queue_plan=latest_state.get("queue_plan", final.get("queue_plan", {})),
                auto_repair=repair_result,
                auto_dispatch=dispatch_result,
            ))
        return response(final, 400)

    # Server-path mode has no GUI file body. Keep the direct archive path for compatibility.
    try:
        archive = archive_submission_to_quartus_server(
            job_id,
            job,
            has_verilog_file=has_verilog_file,
            has_sof_file=has_sof_file,
            form_verilog_path=form_verilog_path,
            form_sof_path=form_sof_path,
        )
        if not archive.get("success"):
            def mark_archive_failed(state: Dict[str, Any]):
                j = state.setdefault("jobs", {}).get(job_id, {})
                if j:
                    j["upload_files_in_progress"] = False
                    j["last_upload_error"] = archive.get("error", "archive failed")
                    j["message"] = "archive to Quartus server failed; retry allowed"
                    state.setdefault("jobs", {})[job_id] = j
                return archive
            update_state_atomic(mark_archive_failed)
            return response(archive, 400)
    except Exception as e:
        def mark_upload_save_failed(state: Dict[str, Any]):
            j = state.setdefault("jobs", {}).get(job_id, {})
            if j:
                j["upload_files_in_progress"] = False
                j["last_upload_error"] = str(e)
                j["message"] = "archive/upload failed; retry allowed"
                state.setdefault("jobs", {})[job_id] = j
            return fail("upload_files failed while archiving to Quartus server", job_id=job_id, error=str(e))
        update_state_atomic(mark_upload_save_failed)
        return response(fail("upload_files failed while archiving to Quartus server", job_id=job_id, error=str(e)), 500)

    final = finalize_archived_upload_success(job_id, archive)
    if final.get("success"):
        wake_queue_worker("upload_files_archived_finalized")
        return response(final)
    code = 404 if final.get("error") == "Unknown queue job during archive finalize" else 400
    return response(final, code)



@app.get("/queue/<job_id>/verify_sof")
def api_queue_job_verify_sof(job_id: str):
    """v3.99 compatibility endpoint: report SOF path info only; no verification/blocking."""
    state = load_state()
    job = state.setdefault("jobs", {}).get(job_id)
    if not job:
        return response(fail("Unknown queue job", job_id=job_id), 404)
    local_path = str(job.get("sof_local_path") or "")
    remote_path = str(job.get("archived_sof_path") or job.get("sof_path") or job.get("remote_sof") or "")
    info = {"verification_disabled": True, "policy": "SOF pass-through from Quartus server history archive; quartus_pgm decides success/failure"}
    if local_path:
        p = Path(local_path)
        info.update({"location": "raspberry_pi", "path": local_path, "exists": p.exists(), "size_bytes": int(p.stat().st_size) if p.exists() else 0})
    elif remote_path:
        info.update({"location": "quartus_server", "path": remote_path})
    else:
        info.update({"location": "none", "path": ""})
    return response(ok(job_id=job_id, sof_info=info, job=public_queue_job(job)))

@app.get("/queue/<job_id>")
def api_queue_job_detail(job_id: str):
    state = load_state()
    job = state.setdefault("jobs", {}).get(job_id)
    if not job:
        return response(fail("Unknown queue job", job_id=job_id), 404)
    return response(ok(job_id=job_id, job=public_queue_job(job), in_queue=job_id in state.get("queue", [])))


@app.post("/queue/<job_id>/archive_retry_now")
def api_queue_job_archive_retry_now(job_id: str):
    """Manually restart async server archive for a spooled upload job."""
    require_api_key()
    state = load_state()
    job = state.setdefault("jobs", {}).get(job_id)
    if not job:
        return response(fail("Unknown queue job", job_id=job_id), 404)
    spool = {
        "spool_dir": job.get("spool_dir", ""),
        "spool_verilog_path": job.get("spool_verilog_path", ""),
        "spool_sof_path": job.get("spool_sof_path", ""),
        "spool_qsf_path": job.get("spool_qsf_path", ""),
        "verilog_filename": job.get("verilog_filename", ""),
        "sof_filename": job.get("sof_filename", ""),
        "qsf_filename": job.get("qsf_filename", ""),
        "verilog_size_bytes": job.get("verilog_size_bytes", 0),
        "sof_size_bytes": job.get("sof_size_bytes", 0),
        "qsf_size_bytes": job.get("qsf_size_bytes", 0),
        "verilog_code": job.get("verilog_code", ""),
        "qsf_text": job.get("qsf_text", ""),
    }
    v_ok = Path(str(spool.get("spool_verilog_path") or "")).is_file()
    s_ok = Path(str(spool.get("spool_sof_path") or "")).is_file()
    if not (v_ok and s_ok):
        return response(fail("Temporary spool files are missing; re-upload this job", job_id=job_id, spool=spool), 400)
    started = start_archive_spooled_upload_thread(job_id, job, spool)
    latest = load_state().setdefault("jobs", {}).get(job_id, job)
    return response(ok(job_id=job_id, archive_thread_started=bool(started), archive_thread_already_running=not bool(started), job=public_queue_job(latest)))


@app.post("/queue/deploy")
def api_queue_deploy():
    cfg = load_config()
    uploads_dir = BASE_DIR / cfg.get("uploads_dir", "uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:10]
    requested_board = None
    client_hostname = "unknown"
    student_ip = request.remote_addr or "unknown"

    if request.is_json:
        data = request.get_json(silent=True) or {}
        requested_board = data.get("requested_board") or None
        client_hostname = data.get("client_hostname") or client_hostname
        student_ip = data.get("student_ip") or student_ip
        priority_role = data.get("priority_label") or data.get("priority_role") or data.get("priority", "Student")
        priority = priority_value_from_role(priority_role)
        priority_label = priority_label_from_value(priority_role)
        student = data.get("student") or data.get("user") or client_hostname or student_ip
        major = data.get("major") or data.get("student_major") or data.get("carrera") or ""
        test_minutes = sanitize_test_minutes(data.get("test_minutes", default_test_minutes()))
        test_seconds = test_minutes * 60
        verilog_path = (data.get("verilog_path") or "").strip()
        verilog_code = data.get("verilog_code", "")
        filename = data.get("filename") or (Path(verilog_path).name if verilog_path else "uploaded.v")
        sof_path = (data.get("sof_path") or "").strip()
        qsf_path = (data.get("qsf_path") or "").strip()
        qsf_text = data.get("qsf_text", "") or ""
        source_mode = data.get("source_mode") or data.get("submit_mode") or data.get("origin") or ""
        if verilog_path and sof_path:
            job = {"job_id": job_id, "status": "queued", "kind": "server_paths", "verilog_path": verilog_path, "sof_path": sof_path, "qsf_path": qsf_path, "qsf_text": qsf_text, "qsf_filename": data.get("qsf_filename") or Path(qsf_path).name, "filename": filename, "requested_board": requested_board, "client_hostname": client_hostname, "student_ip": student_ip, "created_at": now_iso(), "created_ts": time.time(), "message": "queued", "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds), "test_minutes": test_minutes, "test_seconds": test_seconds, "priority": priority, "priority_label": priority_label, "priority_role": priority_label, "student": student, "major": major, "source_mode": source_mode, "submit_mode": source_mode}
            attach_submission_signature(job, data)
            return response(enqueue_job(job))
        if verilog_code.strip() and sof_path:
            job = {"job_id": job_id, "status": "queued", "kind": "code_server_sof", "verilog_code": verilog_code, "sof_path": sof_path, "qsf_path": qsf_path, "qsf_text": qsf_text, "qsf_filename": data.get("qsf_filename") or Path(qsf_path).name, "filename": filename, "requested_board": requested_board, "client_hostname": client_hostname, "student_ip": student_ip, "created_at": now_iso(), "created_ts": time.time(), "message": "queued", "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds), "test_minutes": test_minutes, "test_seconds": test_seconds, "priority": priority, "priority_label": priority_label, "priority_role": priority_label, "student": student, "major": major, "source_mode": source_mode, "submit_mode": source_mode}
            attach_submission_signature(job, data)
            return response(enqueue_job(job))
        return response(fail("JSON queue deploy requires either verilog_path+sof_path or verilog_code+sof_path."), 400)

    job_dir = uploads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    form_verilog_path = (request.form.get("verilog_path") or "").strip()
    form_sof_path = (request.form.get("sof_path") or "").strip()
    requested_board = (request.form.get("requested_board") or "").strip() or None
    client_hostname = request.form.get("client_hostname") or client_hostname
    student_ip = request.form.get("student_ip") or student_ip
    priority_role = request.form.get("priority_label") or request.form.get("priority_role") or request.form.get("priority", "Student")
    priority = priority_value_from_role(priority_role)
    priority_label = priority_label_from_value(priority_role)
    student = request.form.get("student") or request.form.get("user") or client_hostname or student_ip
    major = request.form.get("major") or request.form.get("student_major") or request.form.get("carrera") or ""
    source_mode = request.form.get("source_mode") or request.form.get("submit_mode") or request.form.get("origin") or ""
    test_minutes = sanitize_test_minutes(request.form.get("test_minutes", default_test_minutes()))
    test_seconds = test_minutes * 60

    if "verilog_file" not in request.files and not form_verilog_path:
        return response(fail("Queue deploy requires verilog_file or verilog_path."), 400)
    if "sof_file" not in request.files and not form_sof_path:
        return response(fail("Queue deploy requires sof_file or sof_path."), 400)

    verilog_local_copy_path = ""
    if "verilog_file" in request.files:
        vf = request.files["verilog_file"]
        v_path = job_dir / secure_filename(vf.filename or "design.v")
        v_save = save_filestorage_atomic(vf, v_path)
        if not v_save.get("success"):
            return response(v_save, 500)
    else:
        if not form_verilog_path.lower().endswith((".v", ".sv")):
            return response(fail("verilog_path must point to a .v or .sv file on the server."), 400)
        if not remote_path_allowed(form_verilog_path):
            return response(fail("verilog_path is outside allowed server project folders.", verilog_path=form_verilog_path), 400)
        if not remote_file_exists(form_verilog_path):
            return response(fail("Remote Verilog file does not exist.", verilog_path=form_verilog_path), 400)
        code = read_remote_text(form_verilog_path)
        v_path = job_dir / secure_filename(Path(form_verilog_path).name or "server_design.v")
        v_path.write_text(code, encoding="utf-8", errors="ignore")
        verilog_local_copy_path = str(v_path)

    if form_sof_path:
        if "verilog_file" in request.files:
            verilog_code = v_path.read_text(encoding="utf-8", errors="ignore")
            job = {"job_id": job_id, "status": "queued", "kind": "code_server_sof", "verilog_code": verilog_code, "sof_path": form_sof_path, "filename": v_path.name, "requested_board": requested_board, "client_hostname": client_hostname, "student_ip": student_ip, "created_at": now_iso(), "created_ts": time.time(), "message": "queued", "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds), "test_minutes": test_minutes, "test_seconds": test_seconds, "priority": priority, "priority_label": priority_label, "priority_role": priority_label, "student": student, "major": major, "source_mode": source_mode, "submit_mode": source_mode}
        else:
            job = {"job_id": job_id, "status": "queued", "kind": "server_paths", "verilog_path": form_verilog_path, "sof_path": form_sof_path, "filename": Path(form_verilog_path).name, "requested_board": requested_board, "client_hostname": client_hostname, "student_ip": student_ip, "created_at": now_iso(), "created_ts": time.time(), "message": "queued", "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds), "test_minutes": test_minutes, "test_seconds": test_seconds, "priority": priority, "priority_label": priority_label, "priority_role": priority_label, "student": student, "major": major, "source_mode": source_mode, "submit_mode": source_mode}
        attach_submission_signature(job, request.form.to_dict())
        return response(enqueue_job(job))

    sf = request.files["sof_file"]
    s_path = job_dir / secure_filename(sf.filename or "design.sof")
    s_save = save_filestorage_atomic(sf, s_path)
    if not s_save.get("success"):
        return response(s_save, 500)
    if not str(s_path).lower().endswith(".sof"):
        return response(fail("Uploaded programming file must end with .sof", job_id=job_id, path=str(s_path)), 400)
    if form_verilog_path and verilog_local_copy_path:
        kind = "server_verilog_local_sof"
    else:
        kind = "upload"
    job = {"job_id": job_id, "status": "queued", "kind": kind, "verilog_local_path": str(v_path), "sof_local_path": str(s_path), "verilog_local_copy_path": verilog_local_copy_path, "verilog_path": form_verilog_path, "filename": v_path.name, "sof_source": "raspberry_pi_local_upload_passthrough", "sof_size_bytes": int(Path(s_path).stat().st_size) if Path(s_path).exists() else 0, "requested_board": requested_board, "client_hostname": client_hostname, "student_ip": student_ip, "created_at": now_iso(), "created_ts": time.time(), "message": "queued", "estimated_seconds": estimate_deploy_seconds(requested_board or "", test_seconds), "test_minutes": test_minutes, "test_seconds": test_seconds, "priority": priority, "priority_label": priority_label, "priority_role": priority_label, "student": student, "major": major, "source_mode": source_mode, "submit_mode": source_mode}
    attach_submission_signature(job, request.form.to_dict())
    return response(enqueue_job(job))


@app.get("/security/terminal_key_status")
def security_terminal_key_status():
    # The actual key is never returned by the API. Print it from the Raspberry Pi
    # terminal with UADY_PI.py --keys when an admin needs to pair/open a GUI.
    current_terminal_access_key()
    return response(ok(
        terminal_key_required=True,
        managed_by="raspberry_pi_private_storage",
        key_visible_in_api=False,
        can_change_from_gui=False,
    ))


@app.post("/security/verify_terminal_key")
def security_verify_terminal_key():
    data = request.get_json(silent=True) or {}
    if verify_terminal_access_key(data.get("terminal_key", "")):
        return response(ok(unlocked=True, managed_by="raspberry_pi_private_storage"))
    return response(fail("Invalid terminal key", unlocked=False), 401)


@app.get("/")
def api_root():
    return response(ok(message="UADY Pi AI/HAT Dynamic JTAG Controller v5.4 Final Polished", endpoints=["/status", "/jtag", "/boards", "/ai/select_board", "/ai/ollama_status", "/queue", "/queue/prequeue_upload", "/queue/<job_id>/upload_files", "/stream/queue", "/queue/deploy", "/queue/<job_id>/cancel", "/queue/cancel_batch", "/server/projects", "/jtag/instance/action", "/jtag/prewarm_status", "/jtag/prewarm_now", "/security/terminal_key_status", "/security/verify_terminal_key"]))




def run_jtag_prewarm_startup_cli() -> int:
    """Run JTAG prewarm directly from RUN_PI_CONTROLLER.sh, before Flask starts.

    This is intentionally blocking: when the bash script is executed on the Pi,
    it wakes the Quartus/JTAG server first.  The HTTP server starts afterward,
    so the first student job should not be the thing that wakes jtagd.
    """
    cfg = load_config()
    pcfg = jtag_prewarm_daemon_config()
    print("=" * 70)
    print(cfg.get("controller_name", "UADY Pi AI/HAT Controller"))
    print("BASH STARTUP JTAG PREWARM: running before Flask/API server starts")
    print(f"enabled={pcfg.get('enabled')} iterations={pcfg.get('startup_iterations')} delay={pcfg.get('startup_delay_seconds')}s timeout={pcfg.get('timeout_seconds')}s")
    print("=" * 70)
    if not pcfg.get("enabled", True):
        print("JTAG prewarm disabled in config_pi_hat.json")
        return 0
    success_any = False
    iterations = int(pcfg.get("startup_iterations", 3) or 3)
    delay = float(pcfg.get("startup_delay_seconds", 2.0) or 2.0)
    for i in range(1, iterations + 1):
        print(f"[JTAG PREWARM] startup pass {i}/{iterations}", flush=True)
        try:
            res = jtag_prewarm_once(f"bash_run_pi_controller_startup_{i}")
            print(json.dumps({
                "success": res.get("success"),
                "skipped": res.get("skipped"),
                "reason": res.get("reason"),
                "families": res.get("families"),
                "cables": res.get("cables"),
                "active_programming_jobs": res.get("active_programming_jobs"),
                "errors": res.get("errors"),
            }, indent=2), flush=True)
            if res.get("success"):
                success_any = True
        except Exception as e:
            print(f"[JTAG PREWARM] pass {i} error: {e}", flush=True)
        if i < iterations:
            time.sleep(max(0.0, delay))
    print(f"[JTAG PREWARM] bash startup complete; success_any={success_any}", flush=True)
    # Do not block controller startup if no cable is visible yet. The daemon and
    # per-job v4.25 retry remain as backup, but this makes best effort at bash start.
    return 0

def run_history_write_test_cli() -> int:
    """CLI: test that /home/lab4p0/History_of_jobs can receive one stable record."""
    print("=" * 70)
    print("UADY server history write test")
    print("=" * 70)
    base = history_base_dir() or "/home/lab4p0/History_of_jobs"
    print(f"Configured history folder: {base}")
    test_job = {
        "job_id": "history_test_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "status": "history_test",
        "student": "history_test",
        "major": "history_test",
        "client_hostname": socket.gethostname(),
        "student_ip": "127.0.0.1",
        "priority_label": "System Test",
        "source_mode": "history_write_test",
        "kind": "history_test",
        "requested_board": "TEST",
        "created_at": now_iso(),
        "message": "Manual history write test from Raspberry Pi",
    }
    res = write_job_history_immediate(test_job["job_id"], test_job, "history_write_test")
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    if res.get("success") and res.get("history_logged"):
        print("[OK] History write test passed.")
        return 0
    print("[FAIL] History write test failed.")
    print("Fix on the Quartus server if needed:")
    print("  sudo mkdir -p /home/lab4p0/History_of_jobs")
    print("  sudo chown -R lab4p0:lab4p0 /home/lab4p0/History_of_jobs")
    print("  sudo chmod 775 /home/lab4p0/History_of_jobs")
    return 2


def main():
    cfg = load_config()
    uploads_dir = BASE_DIR / cfg.get("uploads_dir", "uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print(cfg.get("controller_name", "UADY Pi AI/HAT Controller"))
    print("JTAG stays on the Quartus server. Pi controls AI/HAT and commands programming.")
    print(f"Dry run: {cfg.get('dry_run')} | use_gpio: {cfg.get('use_gpio')}")
    print(f"Listening on {cfg.get('host', '0.0.0.0')}:{cfg.get('port', 5050)}")
    # Create/load the API and terminal keys before Flask starts. Only the storage
    # path is printed; use UADY_PI.py --keys on the
    # Raspberry Pi terminal when an admin needs to pair/open a GUI.
    current_pi_api_key()
    current_terminal_access_key()
    print(f"Pi API key: enabled; stored outside config at {pi_secret_path()}")
    print(f"GUI terminal key: enabled; shared by all GUIs; stored outside source at {pi_secret_path()}")
    dyn = initialize_dynamic_runtime_config(force_jtag=True)
    if dyn.get("enabled"):
        limits = dyn.get("limits", {})
        slots = dyn.get("slot_summary", {})
        print(f"Dynamic scaling: slots={limits.get('detected_known_slot_count', 0)}/{limits.get('detected_slot_count', 0)} families={slots.get('detected_board_families', [])}")
        print(f"Adaptive limits: total={limits.get('max_total_http_requests')} upload={limits.get('max_upload_requests')} ai={limits.get('max_ai_requests')} streams={limits.get('max_stream_clients')} queue_soft={limits.get('queue_soft_capacity_jobs')}")
        for warning in dyn.get("warnings", []) or []:
            print(f"Dynamic scaling warning: {warning}")
    print("=" * 70)
    # v4.17: repair old/stale queue state automatically on startup; testing survives disable, queued/running relocate.
    startup_repair = automatic_queue_repair_once("startup_auto_repair", force_plan=True)
    print(f"Automatic startup repair: changed={startup_repair.get('changed')} success={startup_repair.get('success')}")
    # v4.20: clean stale temporary queue stage cache and orphan board_state temp files automatically; active queued/running jobs are protected.
    stage_cleanup = cleanup_old_temporary_stage_cache("startup_stage_cleanup")
    print(f"Temporary stage cleanup: deleted={stage_cleanup.get('deleted_count')} root={stage_cleanup.get('root')} ttl={stage_cleanup.get('ttl_seconds')}s")
    state_tmp_cleanup = cleanup_orphan_board_state_temp_files(0, "startup_state_tmp_cleanup")
    print(f"Upload-spool state-temp cleanup: deleted={state_tmp_cleanup.get('deleted_count')}")
    ai_preload = preload_ollama_qwen_model()
    print(f"Ollama AI preload: success={ai_preload.get('success')} model={ai_preload.get('model')} wall_ms={ai_preload.get('wall_ms', 0)} error={ai_preload.get('error', '')}")
    extractor_path = ensure_signal_extractor_binary()
    print(f"Signal extractor: ready={bool(extractor_path)} path={str(extractor_path or '')} reason={_SIGNAL_EXTRACTOR_BUILD_STATUS.get('reason', '')}")
    ensure_jtag_prewarm_worker()
    print(f"JTAG prewarm daemon: started={JTAG_PREWARM_WORKER_STARTED} interval={jtag_prewarm_daemon_config().get('interval_seconds')}s (bash startup prewarm already ran from RUN_PI_CONTROLLER.sh)")
    ensure_queue_worker(force_restart_if_stalled=True)
    ensure_auto_repair_worker()
    ensure_temp_stage_cleanup_worker()
    ensure_state_tmp_cleanup_worker()
    ensure_queue_stream_broadcaster()
    print(f"Queue stream broadcaster: started={QUEUE_STREAM_BROADCAST_STARTED}")
    app.run(host=cfg.get("host", "0.0.0.0"), port=int(cfg.get("port", 5050)), debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    import sys
    if "--jtag-prewarm-startup" in sys.argv or "--jtag-prewarm-once" in sys.argv:
        raise SystemExit(run_jtag_prewarm_startup_cli())
    if "--test-history-write" in sys.argv:
        raise SystemExit(run_history_write_test_cli())
    main()
