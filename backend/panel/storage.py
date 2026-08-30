"""SQLite persistence for meeting state and durable background jobs.

The store deliberately uses only the standard library.  Every public method is
safe to call from FastAPI's event loop or worker tasks; a process-local lock
serializes access to the shared SQLite connection while WAL mode keeps the file
recoverable across process restarts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .crypto import EncryptionManager


LIFECYCLES = {
    "prepared",
    "live",
    "paused",
    "finalizing",
    "review",
    "completed",
    "failed",
}
_ACTIVE_LIFECYCLES = ("live", "paused", "finalizing")

_TRANSITIONS = {
    "prepared": {"live", "failed"},
    "live": {"paused", "finalizing", "failed"},
    "paused": {"live", "finalizing", "failed"},
    "finalizing": {"review", "failed"},
    "review": {"completed", "finalizing", "failed"},
    "completed": {"finalizing"},
    "failed": {"finalizing"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _future(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class SQLiteStore:
    """Meeting-scoped persistence with append-only revisioned events."""

    def __init__(self, path: str | Path, cipher: EncryptionManager | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        default_key = self.path.with_suffix(".key.dpapi" if os.name == "nt" else ".key")
        self.cipher = cipher or EncryptionManager.load(default_key)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._init_schema()
            self._seal_legacy_content()
            self.requeue_expired_jobs()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                lifecycle TEXT NOT NULL CHECK (
                    lifecycle IN ('prepared','live','paused','finalizing','review','completed','failed')
                ),
                topic TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                terms_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 0,
                transcript_revision INTEGER NOT NULL DEFAULT 0,
                state_revision INTEGER NOT NULL DEFAULT 0,
                retained INTEGER NOT NULL DEFAULT 1,
                consent_external_processing INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                finalization_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                UNIQUE(meeting_id, seq),
                UNIQUE(meeting_id, revision)
            );

            CREATE TABLE IF NOT EXISTS utterances (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                external_item_id TEXT NOT NULL,
                previous_item_id TEXT,
                seq INTEGER NOT NULL,
                text TEXT NOT NULL,
                speaker TEXT NOT NULL DEFAULT 'unknown',
                started_at TEXT,
                ended_at TEXT,
                source TEXT NOT NULL DEFAULT 'realtime',
                created_at TEXT NOT NULL,
                UNIQUE(meeting_id, external_item_id),
                UNIQUE(meeting_id, seq)
            );

            CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                utterance_id TEXT NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
                normalized_hash TEXT NOT NULL,
                claim_text TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'public',
                status TEXT NOT NULL DEFAULT 'queued',
                verdict TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                base_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(meeting_id, normalized_hash)
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                base_revision INTEGER NOT NULL,
                state_revision INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(meeting_id, state_revision)
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                base_revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                lease_id TEXT,
                lease_until TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(meeting_id, kind, dedupe_key)
            );

            CREATE INDEX IF NOT EXISTS jobs_ready_idx
                ON jobs(status, kind, available_at, created_at);

            CREATE TABLE IF NOT EXISTS minutes (
                meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
                transcript_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                structured_json TEXT NOT NULL,
                markdown TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS audio_metadata (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                chunk_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(meeting_id, chunk_id),
                UNIQUE(meeting_id, sequence)
            );
            """
        )
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(meetings)").fetchall()
        }
        if "consent_external_processing" not in columns:
            self._conn.execute(
                "ALTER TABLE meetings ADD COLUMN consent_external_processing INTEGER NOT NULL DEFAULT 0"
            )

    def create_meeting(
        self,
        *,
        topic: str = "",
        goal: str = "",
        terms: Iterable[str] = (),
        consent_external_processing: bool = False,
        meeting_id: str | None = None,
    ) -> dict[str, Any]:
        meeting_id = meeting_id or str(uuid.uuid4())
        now = utc_now()
        clean_terms = [str(term).strip() for term in terms if str(term).strip()]
        with self._transaction():
            active = self._conn.execute(
                "SELECT id,lifecycle FROM meetings WHERE lifecycle IN (?,?,?) LIMIT 1",
                _ACTIVE_LIFECYCLES,
            ).fetchone()
            if active is not None:
                raise ValueError(
                    f"another meeting is already active: {active['id']} ({active['lifecycle']})"
                )
            self._conn.execute(
                """
                INSERT INTO meetings(
                    id,lifecycle,topic,goal,terms_json,consent_external_processing,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    meeting_id,
                    "prepared",
                    self._seal_text(topic.strip(), "meeting.topic"),
                    self._seal_text(goal.strip(), "meeting.goal"),
                    self._seal_json(clean_terms, "meeting.terms"),
                    int(bool(consent_external_processing)),
                    now,
                    now,
                ),
            )
            self._append_event_locked(
                meeting_id,
                "meeting.created",
                {
                    "lifecycle": "prepared",
                    "topic": topic.strip(),
                    "goal": goal.strip(),
                    "consent_external_processing": bool(consent_external_processing),
                },
            )
        return self.get_meeting(meeting_id)

    def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return self._meeting_dict(row)

    def list_meetings(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [self._meeting_dict(row) for row in rows]

    def transition(self, meeting_id: str, target: str, *, error: str = "") -> tuple[dict, dict | None]:
        if target not in LIFECYCLES:
            raise ValueError(f"invalid lifecycle: {target}")
        with self._transaction():
            row = self._require_meeting_locked(meeting_id)
            current = str(row["lifecycle"])
            if current == target:
                return self._meeting_dict(row), None
            if target not in _TRANSITIONS[current]:
                raise ValueError(f"invalid lifecycle transition: {current} -> {target}")
            if target == "live":
                active = self._conn.execute(
                    """
                    SELECT id,lifecycle FROM meetings
                    WHERE id<>? AND lifecycle IN (?,?,?) LIMIT 1
                    """,
                    (meeting_id, *_ACTIVE_LIFECYCLES),
                ).fetchone()
                if active is not None:
                    raise ValueError(
                        f"another meeting is already active: {active['id']} ({active['lifecycle']})"
                    )
            now = utc_now()
            started_at = row["started_at"]
            ended_at = row["ended_at"]
            if target == "live" and not started_at:
                started_at = now
            if target in {"finalizing", "failed"} and not ended_at:
                ended_at = now
            self._conn.execute(
                """
                UPDATE meetings
                SET lifecycle=?,updated_at=?,started_at=?,ended_at=?,finalization_error=?
                WHERE id=?
                """,
                (
                    target,
                    now,
                    started_at,
                    ended_at,
                    self._seal_text(error[:1000], "meeting.finalization_error"),
                    meeting_id,
                ),
            )
            event = self._append_event_locked(
                meeting_id,
                "meeting.lifecycle_changed",
                {"from": current, "to": target, "error": error[:500]},
            )
        return self.get_meeting(meeting_id), event

    def append_event(self, meeting_id: str, event_type: str, payload: dict[str, Any]) -> dict:
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            return self._append_event_locked(meeting_id, event_type, payload)

    def events_after(self, meeting_id: str, revision: int = 0, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM events
                WHERE meeting_id=? AND revision>?
                ORDER BY revision ASC LIMIT ?
                """,
                (meeting_id, max(0, int(revision)), max(1, min(int(limit), 2000))),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    def add_utterance(
        self,
        meeting_id: str,
        *,
        external_item_id: str,
        text: str,
        previous_item_id: str | None = None,
        speaker: str = "unknown",
        started_at: str | None = None,
        ended_at: str | None = None,
        source: str = "realtime",
    ) -> tuple[dict, dict | None, bool]:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("utterance text is empty")
        if not external_item_id.strip():
            raise ValueError("external_item_id is empty")
        with self._transaction():
            meeting = self._require_meeting_locked(meeting_id)
            existing = self._conn.execute(
                "SELECT * FROM utterances WHERE meeting_id=? AND external_item_id=?",
                (meeting_id, external_item_id),
            ).fetchone()
            if existing is not None:
                return self._utterance_dict(existing), None, False
            if meeting["lifecycle"] not in {"live", "paused", "finalizing"}:
                raise ValueError(f"meeting is not accepting transcripts: {meeting['lifecycle']}")
            seq = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM utterances WHERE meeting_id=?",
                    (meeting_id,),
                ).fetchone()[0]
            )
            utterance_id = str(uuid.uuid4())
            now = utc_now()
            self._conn.execute(
                """
                INSERT INTO utterances(
                    id,meeting_id,external_item_id,previous_item_id,seq,text,speaker,
                    started_at,ended_at,source,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    utterance_id,
                    meeting_id,
                    external_item_id,
                    previous_item_id,
                    seq,
                    self._seal_text(clean_text, "utterance.text"),
                    self._seal_text(speaker.strip() or "unknown", "utterance.speaker"),
                    started_at,
                    ended_at,
                    source,
                    now,
                ),
            )
            self._conn.execute(
                """
                UPDATE meetings
                SET transcript_revision=transcript_revision+1,updated_at=? WHERE id=?
                """,
                (now, meeting_id),
            )
            row = self._conn.execute(
                "SELECT * FROM utterances WHERE id=?", (utterance_id,)
            ).fetchone()
            payload = self._utterance_dict(row)
            event = self._append_event_locked(meeting_id, "transcript.final", payload)
        return payload, event, True

    def list_utterances(self, meeting_id: str, *, limit: int = 5000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM utterances WHERE meeting_id=?
                ORDER BY seq ASC LIMIT ?
                """,
                (meeting_id, max(1, min(int(limit), 20000))),
            ).fetchall()
        return [self._utterance_dict(row) for row in rows]

    def upsert_claim(
        self,
        meeting_id: str,
        *,
        utterance_id: str,
        normalized_hash: str,
        claim_text: str,
        scope: str,
        status: str,
        base_revision: int,
    ) -> tuple[dict, dict | None, bool]:
        now = utc_now()
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            existing = self._conn.execute(
                "SELECT * FROM claims WHERE meeting_id=? AND normalized_hash=?",
                (meeting_id, normalized_hash),
            ).fetchone()
            if existing is not None:
                return self._claim_dict(existing), None, False
            claim_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO claims(
                    id,meeting_id,utterance_id,normalized_hash,claim_text,scope,status,
                    base_revision,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    claim_id,
                    meeting_id,
                    utterance_id,
                    normalized_hash,
                    self._seal_text(claim_text.strip(), "claim.text"),
                    scope,
                    status,
                    int(base_revision),
                    now,
                    now,
                ),
            )
            row = self._conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            claim = self._claim_dict(row)
            event = self._append_event_locked(meeting_id, "fact.created", claim)
        return claim, event, True

    def update_claim(
        self,
        claim_id: str,
        *,
        status: str,
        verdict: str = "",
        sources: list[dict] | None = None,
    ) -> tuple[dict, dict]:
        with self._transaction():
            existing = self._conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if existing is None:
                raise KeyError(claim_id)
            now = utc_now()
            self._conn.execute(
                """
                UPDATE claims SET status=?,verdict=?,sources_json=?,updated_at=? WHERE id=?
                """,
                (
                    status,
                    self._seal_text(verdict.strip(), "claim.verdict"),
                    self._seal_json(sources or [], "claim.sources"),
                    now,
                    claim_id,
                ),
            )
            row = self._conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            claim = self._claim_dict(row)
            event = self._append_event_locked(str(row["meeting_id"]), "fact.updated", claim)
        return claim, event

    def list_claims(self, meeting_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM claims WHERE meeting_id=? ORDER BY created_at ASC", (meeting_id,)
            ).fetchall()
        return [self._claim_dict(row) for row in rows]

    def save_snapshot_if_fresh(
        self,
        meeting_id: str,
        *,
        base_revision: int,
        data: dict[str, Any],
    ) -> tuple[dict | None, dict | None, bool]:
        """Save unless a snapshot based on newer input is already present."""
        with self._transaction():
            meeting = self._require_meeting_locked(meeting_id)
            latest = self._conn.execute(
                """
                SELECT * FROM snapshots WHERE meeting_id=?
                ORDER BY base_revision DESC,state_revision DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            if latest is not None and int(latest["base_revision"]) > int(base_revision):
                return self._snapshot_dict(latest), None, False
            state_revision = int(meeting["state_revision"]) + 1
            snapshot_id = str(uuid.uuid4())
            now = utc_now()
            self._conn.execute(
                """
                INSERT INTO snapshots(
                    id,meeting_id,base_revision,state_revision,data_json,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    meeting_id,
                    int(base_revision),
                    state_revision,
                    self._seal_json(data, "snapshot.data"),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE meetings SET state_revision=?,updated_at=? WHERE id=?",
                (state_revision, now, meeting_id),
            )
            row = self._conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            snapshot = self._snapshot_dict(row)
            event = self._append_event_locked(meeting_id, "meeting.state_updated", snapshot)
        return snapshot, event, True

    def latest_snapshot(self, meeting_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM snapshots WHERE meeting_id=?
                ORDER BY state_revision DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
        return self._snapshot_dict(row) if row is not None else None

    def enqueue_job(
        self,
        meeting_id: str,
        *,
        kind: str,
        dedupe_key: str,
        base_revision: int,
        payload: dict[str, Any],
    ) -> tuple[dict, bool]:
        now = utc_now()
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            existing = self._conn.execute(
                "SELECT * FROM jobs WHERE meeting_id=? AND kind=? AND dedupe_key=?",
                (meeting_id, kind, dedupe_key),
            ).fetchone()
            if existing is not None:
                return self._job_dict(existing), False
            job_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO jobs(
                    id,meeting_id,kind,dedupe_key,base_revision,payload_json,status,
                    available_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    meeting_id,
                    kind,
                    dedupe_key,
                    int(base_revision),
                    self._seal_json(payload, "job.payload"),
                    "queued",
                    now,
                    now,
                    now,
                ),
            )
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row), True

    def enqueue_latest_progress(
        self,
        meeting_id: str,
        *,
        base_revision: int,
        payload: dict[str, Any],
    ) -> tuple[dict, bool]:
        """Keep at most one queued progress job while one may be running."""
        now = utc_now()
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            queued = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE meeting_id=? AND kind='progress' AND status='queued'
                ORDER BY created_at DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            if queued is not None:
                dedupe_key = f"revision:{int(base_revision)}"
                self._conn.execute(
                    """
                    UPDATE jobs SET dedupe_key=?,base_revision=?,payload_json=?,available_at=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        dedupe_key,
                        int(base_revision),
                        self._seal_json(payload, "job.payload"),
                        now,
                        now,
                        queued["id"],
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (queued["id"],)
                ).fetchone()
                return self._job_dict(row), False

            job_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO jobs(
                    id,meeting_id,kind,dedupe_key,base_revision,payload_json,status,
                    available_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    meeting_id,
                    "progress",
                    f"revision:{int(base_revision)}",
                    int(base_revision),
                    self._seal_json(payload, "job.payload"),
                    "queued",
                    now,
                    now,
                    now,
                ),
            )
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row), True

    def claim_job(self, kinds: Iterable[str], *, lease_seconds: float = 30.0) -> dict | None:
        kind_list = [str(kind) for kind in kinds]
        if not kind_list:
            return None
        with self._transaction():
            self._requeue_expired_jobs_locked()
            placeholders = ",".join("?" for _ in kind_list)
            now = utc_now()
            row = self._conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status='queued' AND available_at<=? AND kind IN ({placeholders})
                ORDER BY CASE kind WHEN 'finalize' THEN 0 WHEN 'fact' THEN 1 ELSE 2 END,
                         created_at ASC
                LIMIT 1
                """,
                (now, *kind_list),
            ).fetchone()
            if row is None:
                return None
            lease_id = str(uuid.uuid4())
            self._conn.execute(
                """
                UPDATE jobs
                SET status='running',attempts=attempts+1,lease_id=?,lease_until=?,updated_at=?
                WHERE id=? AND status='queued'
                """,
                (lease_id, _future(lease_seconds), now, row["id"]),
            )
            claimed = self._conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return self._job_dict(claimed)

    def complete_job(self, job_id: str, lease_id: str) -> bool:
        with self._transaction():
            cursor = self._conn.execute(
                """
                UPDATE jobs SET status='succeeded',lease_id=NULL,lease_until=NULL,updated_at=?
                WHERE id=? AND status='running' AND lease_id=?
                """,
                (utc_now(), job_id, lease_id),
            )
        return cursor.rowcount == 1

    def fail_job(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retry_delay_s: float | None,
    ) -> bool:
        status = "queued" if retry_delay_s is not None else "failed"
        available_at = _future(retry_delay_s or 0.0)
        with self._transaction():
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status=?,available_at=?,lease_id=NULL,lease_until=NULL,last_error=?,updated_at=?
                WHERE id=? AND status='running' AND lease_id=?
                """,
                (
                    status,
                    available_at,
                    self._seal_text(error[:1000], "job.error"),
                    utc_now(),
                    job_id,
                    lease_id,
                ),
            )
        return cursor.rowcount == 1

    def requeue_expired_jobs(self) -> int:
        with self._transaction():
            return self._requeue_expired_jobs_locked()

    def _requeue_expired_jobs_locked(self) -> int:
        cursor = self._conn.execute(
            """
            UPDATE jobs
            SET status='queued',lease_id=NULL,lease_until=NULL,updated_at=?
            WHERE status='running' AND lease_until IS NOT NULL AND lease_until<=?
            """,
            (utc_now(), utc_now()),
        )
        return cursor.rowcount

    def count_pending_jobs(self, meeting_id: str, kind: str | None = None) -> int:
        with self._lock:
            if kind is None:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE meeting_id=? AND status IN ('queued','running')
                    """,
                    (meeting_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE meeting_id=? AND kind=? AND status IN ('queued','running')
                    """,
                    (meeting_id, kind),
                ).fetchone()
            return int(row[0])

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_dict(row)

    def upsert_minutes(
        self,
        meeting_id: str,
        *,
        transcript_revision: int,
        structured: dict[str, Any],
        markdown: str,
        status: str = "draft",
    ) -> tuple[dict, dict]:
        now = utc_now()
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            self._conn.execute(
                """
                INSERT INTO minutes(
                    meeting_id,transcript_revision,status,structured_json,markdown,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                    transcript_revision=excluded.transcript_revision,
                    status=excluded.status,
                    structured_json=excluded.structured_json,
                    markdown=excluded.markdown,
                    updated_at=excluded.updated_at,
                    approved_at=NULL
                """,
                (
                    meeting_id,
                    int(transcript_revision),
                    status,
                    self._seal_json(structured, "minutes.structured"),
                    self._seal_text(markdown, "minutes.markdown"),
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            minutes = self._minutes_dict(row)
            event = self._append_event_locked(meeting_id, "minutes.ready", minutes)
        return minutes, event

    def mark_minutes_stale(self, meeting_id: str, *, reason: str) -> tuple[dict, dict | None]:
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            if row["status"] == "stale":
                return self._minutes_dict(row), None
            now = utc_now()
            self._conn.execute(
                """
                UPDATE minutes SET status='stale',approved_at=NULL,updated_at=?
                WHERE meeting_id=?
                """,
                (now, meeting_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            minutes = self._minutes_dict(updated)
            event = self._append_event_locked(
                meeting_id,
                "minutes.stale",
                {"minutes": minutes, "reason": reason[:100]},
            )
        return minutes, event

    def get_minutes(self, meeting_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
        return self._minutes_dict(row) if row is not None else None

    def approve_minutes(self, meeting_id: str) -> tuple[dict, dict]:
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            now = utc_now()
            self._conn.execute(
                "UPDATE minutes SET status='approved',approved_at=?,updated_at=? WHERE meeting_id=?",
                (now, now, meeting_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            minutes = self._minutes_dict(updated)
            event = self._append_event_locked(meeting_id, "minutes.approved", minutes)
        return minutes, event

    def add_audio_metadata(
        self,
        meeting_id: str,
        *,
        chunk_id: str,
        sequence: int,
        content_type: str,
        size_bytes: int,
        sha256: str,
        path: str,
        started_at: str | None,
        ended_at: str | None,
    ) -> tuple[dict, dict | None, bool]:
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            existing = self._conn.execute(
                "SELECT * FROM audio_metadata WHERE meeting_id=? AND chunk_id=?",
                (meeting_id, chunk_id),
            ).fetchone()
            if existing is not None:
                return dict(existing), None, False
            audio_id = str(uuid.uuid4())
            now = utc_now()
            self._conn.execute(
                """
                INSERT INTO audio_metadata(
                    id,meeting_id,chunk_id,sequence,content_type,size_bytes,sha256,path,
                    started_at,ended_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    audio_id,
                    meeting_id,
                    chunk_id,
                    int(sequence),
                    content_type,
                    int(size_bytes),
                    sha256,
                    path,
                    started_at,
                    ended_at,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM audio_metadata WHERE id=?", (audio_id,)
            ).fetchone()
            payload = dict(row)
            event = self._append_event_locked(
                meeting_id,
                "audio.persisted",
                {key: payload[key] for key in ("chunk_id", "sequence", "size_bytes", "sha256")},
            )
        return payload, event, True

    def audio_paths(self, meeting_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM audio_metadata WHERE meeting_id=? ORDER BY sequence", (meeting_id,)
            ).fetchall()
        return [str(row["path"]) for row in rows]

    def list_audio_metadata(self, meeting_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audio_metadata WHERE meeting_id=? ORDER BY sequence", (meeting_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_meeting(self, meeting_id: str) -> list[str]:
        with self._transaction():
            self._require_meeting_locked(meeting_id)
            paths = [
                str(row["path"])
                for row in self._conn.execute(
                    "SELECT path FROM audio_metadata WHERE meeting_id=?", (meeting_id,)
                ).fetchall()
            ]
            self._conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        return paths

    def _append_event_locked(self, meeting_id: str, event_type: str, payload: dict) -> dict:
        row = self._require_meeting_locked(meeting_id)
        revision = int(row["revision"]) + 1
        seq = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE meeting_id=?", (meeting_id,)
            ).fetchone()[0]
        )
        event = {
            "type": event_type,
            "meeting_id": meeting_id,
            "event_id": str(uuid.uuid4()),
            "seq": seq,
            "revision": revision,
            "occurred_at": utc_now(),
            "payload": payload,
        }
        self._conn.execute(
            """
            INSERT INTO events(event_id,meeting_id,seq,revision,type,payload_json,occurred_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                event["event_id"],
                meeting_id,
                seq,
                revision,
                event_type,
                self._seal_json(payload, "event.payload"),
                event["occurred_at"],
            ),
        )
        self._conn.execute(
            "UPDATE meetings SET revision=?,updated_at=? WHERE id=?",
            (revision, event["occurred_at"], meeting_id),
        )
        return event

    def _require_meeting_locked(self, meeting_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return row

    def _transaction(self):
        return _Transaction(self._conn, self._lock)

    def _meeting_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["topic"] = self._open_text(result["topic"], "meeting.topic")
        result["goal"] = self._open_text(result["goal"], "meeting.goal")
        result["terms"] = self._open_json(result.pop("terms_json", ""), "meeting.terms", [])
        result["finalization_error"] = self._open_text(
            result.get("finalization_error", ""), "meeting.finalization_error"
        )
        result["retained"] = bool(result.get("retained", 1))
        result["consent_external_processing"] = bool(
            result.get("consent_external_processing", 0)
        )
        return result

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "type": row["type"],
            "meeting_id": row["meeting_id"],
            "event_id": row["event_id"],
            "seq": row["seq"],
            "revision": row["revision"],
            "occurred_at": row["occurred_at"],
            "payload": self._open_json(row["payload_json"], "event.payload", {}),
        }

    def _utterance_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["text"] = self._open_text(result["text"], "utterance.text")
        result["speaker"] = self._open_text(result["speaker"], "utterance.speaker")
        return result

    def _claim_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["claim_text"] = self._open_text(result["claim_text"], "claim.text")
        result["verdict"] = self._open_text(result["verdict"], "claim.verdict")
        result["sources"] = self._open_json(result.pop("sources_json", ""), "claim.sources", [])
        return result

    def _snapshot_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = self._open_json(result.pop("data_json", ""), "snapshot.data", {})
        return result

    def _job_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = self._open_json(result.pop("payload_json", ""), "job.payload", {})
        result["last_error"] = self._open_text(result.get("last_error", ""), "job.error")
        return result

    def _minutes_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["structured"] = self._open_json(
            result.pop("structured_json", ""), "minutes.structured", {}
        )
        result["markdown"] = self._open_text(result["markdown"], "minutes.markdown")
        return result

    def _seal_text(self, value: str, context: str) -> str:
        return self.cipher.encrypt_text(value, context=context)

    def _open_text(self, value: str | None, context: str) -> str:
        return self.cipher.decrypt_text(str(value or ""), context=context)

    def _seal_json(self, value: Any, context: str) -> str:
        return self._seal_text(_json(value), context)

    def _open_json(self, value: str | None, context: str, default: Any) -> Any:
        return _loads(self._open_text(value, context), default)

    def _seal_legacy_content(self) -> None:
        """Encrypt plaintext rows created by an earlier local build in place."""
        fields = {
            "meetings": {
                "topic": "meeting.topic",
                "goal": "meeting.goal",
                "terms_json": "meeting.terms",
                "finalization_error": "meeting.finalization_error",
            },
            "events": {"payload_json": "event.payload"},
            "utterances": {"text": "utterance.text", "speaker": "utterance.speaker"},
            "claims": {
                "claim_text": "claim.text",
                "verdict": "claim.verdict",
                "sources_json": "claim.sources",
            },
            "snapshots": {"data_json": "snapshot.data"},
            "jobs": {"payload_json": "job.payload", "last_error": "job.error"},
            "minutes": {
                "structured_json": "minutes.structured",
                "markdown": "minutes.markdown",
            },
        }
        with self._transaction():
            for table, columns in fields.items():
                selected = ",".join(["rowid", *columns])
                for row in self._conn.execute(f"SELECT {selected} FROM {table}").fetchall():
                    updates: dict[str, str] = {}
                    for column, context in columns.items():
                        value = str(row[column] or "")
                        if not self.cipher.is_encrypted_text(value):
                            updates[column] = self._seal_text(value, context)
                    if updates:
                        setters = ",".join(f"{column}=?" for column in updates)
                        self._conn.execute(
                            f"UPDATE {table} SET {setters} WHERE rowid=?",
                            (*updates.values(), row["rowid"]),
                        )


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self._connection = connection
        self._lock = lock

    def __enter__(self):
        self._lock.acquire()
        self._connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self._connection.execute("COMMIT")
            else:
                self._connection.execute("ROLLBACK")
        finally:
            self._lock.release()
        return False
