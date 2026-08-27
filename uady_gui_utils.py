# -*- coding: utf-8 -*-
"""
uady_gui_utils.py

Shared helper functions/classes for the UADY FPGA GUI.

Keep repeated runtime code here so future GUI projects can import it instead of
copying the same functions again.
"""
from __future__ import annotations

import os
import re
import json
import threading
import datetime
from typing import Any, Iterable, List, Mapping, Optional, Sequence


class ApiSession:
    """requests.Session wrapper for HTTP keep-alive."""

    def __init__(self, requests_module: Any, base_url_getter, headers_getter, validator=None):
        self.requests = requests_module
        self.base_url_getter = base_url_getter
        self.headers_getter = headers_getter
        self.validator = validator
        # requests.Session is kept per worker thread.  A single shared Session can
        # race when the GUI stream, JTAG poll, queue poll, and upload threads use
        # it at the same time.  Per-thread sessions keep HTTP keep-alive benefits
        # without sharing mutable connection-pool state across Tk background tasks.
        self._thread_local = threading.local()
        self._sessions = set()
        self._sessions_lock = threading.RLock()

    def _session_obj(self):
        if self.requests is None:
            raise RuntimeError("Falta instalar requests. Ejecuta: pip install requests")
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self.requests.Session()
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.add(session)
        return session

    def _drop_thread_session(self):
        session = getattr(self._thread_local, "session", None)
        if session is None:
            return
        try:
            session.close()
        finally:
            with self._sessions_lock:
                self._sessions.discard(session)
            self._thread_local.session = None

    def close(self):
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        self._thread_local.session = None

    def _url(self, path: str) -> str:
        if self.validator:
            self.validator()
        return str(self.base_url_getter()).rstrip("/") + path

    def get_json(self, path: str, timeout: int = 15):
        try:
            r = self._session_obj().get(self._url(path), headers=self.headers_getter(), timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            # Drop only the current worker thread's broken keep-alive socket after
            # timeout/reset/refused errors; do not interrupt a live SSE stream or
            # another upload running in a different background thread.
            self._drop_thread_session()
            raise

    def post_json(self, path: str, payload: Mapping[str, Any], timeout: int = 20):
        try:
            r = self._session_obj().post(self._url(path), json=payload, headers=self.headers_getter(), timeout=timeout)
            try:
                r.raise_for_status()
            except Exception as exc:
                detail = ""
                try:
                    detail = r.text[:2000]
                except Exception:
                    detail = ""
                self._drop_thread_session()
                raise RuntimeError(f"HTTP request failed: {exc}; response={detail}") from exc
            return r.json()
        except Exception:
            self._drop_thread_session()
            raise

    def post_files(self, path: str, fields: Mapping[str, Any], files: Mapping[str, str], timeout: int = 300):
        opened = []
        try:
            prepared = {}
            for name, fp in files.items():
                f = open(fp, "rb")
                opened.append(f)
                prepared[name] = (os.path.basename(fp), f)
            r = self._session_obj().post(
                self._url(path),
                data=dict(fields),
                files=prepared,
                headers=self.headers_getter(),
                timeout=timeout,
            )
            try:
                r.raise_for_status()
            except Exception as exc:
                detail = ""
                try:
                    detail = r.text[:4000]
                except Exception:
                    detail = ""
                self._drop_thread_session()
                raise RuntimeError(f"HTTP file upload failed: {exc}; response={detail}") from exc
            return r.json()
        except Exception:
            self._drop_thread_session()
            raise
        finally:
            for f in opened:
                try:
                    f.close()
                except Exception:
                    pass

    def stream_sse_json(self, path: str, timeout=(3, 15)):
        """
        Stream Server-Sent Events and yield JSON payloads from data: lines.

        This gives WebSocket-like push updates without adding a new dependency.
        If the stream breaks, close the keep-alive session so the GUI reconnects cleanly.
        """
        if self.requests is None:
            raise RuntimeError("Falta instalar requests. Ejecuta: pip install requests")
        try:
            with self._session_obj().get(
                self._url(path),
                headers=self.headers_getter(),
                timeout=timeout,
                stream=True,
            ) as r:
                r.raise_for_status()
                buf = []
                for raw in r.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    line = raw.strip()
                    if not line:
                        if buf:
                            data = "\n".join(buf)
                            buf = []
                            try:
                                yield json.loads(data)
                            except Exception:
                                continue
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        buf.append(line[5:].strip())
        except Exception:
            self._drop_thread_session()
            raise


def fmt_seconds(value: Any) -> str:
    try:
        value = int(value or 0)
    except Exception:
        value = 0
    m, s = divmod(max(0, value), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def now_compact_string() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def sanitize_name(value: Any, fallback: str = "unknown") -> str:
    text = str(value or fallback).strip()
    if "@" in text:
        text = text.split("@", 1)[0]
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return text or fallback


def dedupe_rows_by_key(rows: Iterable[Mapping[str, Any]], key: str = "job_id") -> List[Mapping[str, Any]]:
    out = []
    seen = set()
    for row in rows:
        rid = row.get(key, "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(row)
    return out


def treeview_sync_by_key(tree, rows: Sequence[Sequence[Any]], key_index: int = 0, restore_key: Optional[str] = None):
    """
    Sync a ttk.Treeview efficiently.

    If the row key order did not change, update cells in place.
    If keys changed, rebuild once.
    """
    existing_ids = list(tree.get_children())
    existing_keys = []
    for iid in existing_ids:
        vals = tree.item(iid, "values")
        existing_keys.append(vals[key_index] if len(vals) > key_index else "")

    new_keys = [row[key_index] if len(row) > key_index else "" for row in rows]
    item_by_key = {}

    if existing_keys == new_keys:
        for iid, row in zip(existing_ids, rows):
            # Tk stores Treeview values as Tcl strings. Normalize before comparing
            # so unchanged rows are not redrawn every live refresh just because the
            # producer used an int/bool while Tk returned a string.
            old_values = tree.item(iid, "values")
            old_norm = tuple(str(v) for v in old_values)
            new_norm = tuple(str(v) for v in row)
            if old_norm != new_norm:
                tree.item(iid, values=row)
            if len(row) > key_index:
                item_by_key[row[key_index]] = iid
    else:
        for iid in existing_ids:
            tree.delete(iid)
        for row in rows:
            iid = tree.insert("", "end", values=row)
            if len(row) > key_index:
                item_by_key[row[key_index]] = iid

    if restore_key and restore_key in item_by_key:
        try:
            iid = item_by_key[restore_key]
            tree.selection_set(iid)
            tree.focus(iid)
            tree.see(iid)
        except Exception:
            pass

    return item_by_key
