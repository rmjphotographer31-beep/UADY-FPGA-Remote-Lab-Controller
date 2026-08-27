#!/usr/bin/env python3
"""SQLite persistence layer for queue and terminal job states."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


TERMINAL = {"cancelled", "completed", "failed"}


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._db.commit()

    def upsert(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        status = str(job.get("status") or "queued")
        revision = int(job.get("revision", 0) or 0)
        payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._db.execute(
                """
                INSERT INTO jobs(job_id,status,revision,payload_json)
                VALUES(?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    revision=MAX(jobs.revision, excluded.revision),
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (job_id, status, revision, payload),
            )
            self._db.commit()

    def transition(
        self,
        job_id: str,
        expected_revision: int,
        target_status: str,
        updates: dict[str, Any],
    ) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT payload_json,status,revision FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] in TERMINAL:
                return False
            if int(row["revision"]) != int(expected_revision):
                return False

            payload = json.loads(row["payload_json"])
            payload.update(updates)
            payload["status"] = target_status
            payload["revision"] = int(expected_revision) + 1

            cursor = self._db.execute(
                """
                UPDATE jobs
                SET status=?, revision=revision+1, payload_json=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE job_id=? AND revision=?
                  AND status NOT IN ('cancelled','completed','failed')
                """,
                (
                    target_status,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    job_id,
                    expected_revision,
                ),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM jobs ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
