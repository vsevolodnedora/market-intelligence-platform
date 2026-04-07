"""Durable event outbox for the EDGAR ingestor.

This module is the **publication boundary** between the ingestion daemon and
downstream consumers.  It owns:

  1. Event envelope construction — converting parsed filing results into
     normalised domain events.
  2. Transactional outbox writes — inserting outbox rows inside the same
     SQLite transaction that persists filing state, so that the DB is the
     single durable source of truth.
  3. Artifact commit helpers — atomic file writes (temp → fsync → rename)
     so the filesystem side is as deterministic as possible.
  4. Claim-and-publish loop — a separate async task that reads pending
     outbox rows, publishes them (currently to a pluggable callback; NATS
     integration is a future step), and marks them published.

Design rationale:

    Filesystem + SQLite + NATS cannot be made truly atomic in one step.
    The correct pattern is:
      1. Write artifact to temp file
      2. Atomic rename to final file
      3. Open DB transaction
      4. Save filing / artifact / derived rows
      5. Insert outbox events
      6. Commit DB transaction
      7. Separate publisher sends NATS and marks outbox rows published

    The SQLite DB plus outbox is the single durable source of truth.  NATS
    (or any external transport) becomes transport, not truth.

This module does NOT contain SEC parsing, watchlist matching, or strategy
evaluation.

Dependencies: Python >= 3.11 (no additional packages beyond edgar_core).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from edgar_core import (
    EightKEvent,
    FilingArtifact,
    FilingDiscovery,
    FilingParty,
    Form4Filing,
    SQLiteStorage,
    SubmissionHeader,
    dump_json,
    get_logger,
    guess_content_type_from_filename,
    normalized_header_metadata,
    sha256_hex,
    utcnow,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Event subjects — the domain event taxonomy
# ---------------------------------------------------------------------------

class EventSubjects:
    FILING_RETRIEVED = "edgar.filing.retrieved"
    FILING_PARTIAL = "edgar.filing.retrieved_partial"
    FILING_FAILED = "edgar.filing.failed"
    FORM4_PARSED = "edgar.form4.parsed"
    EIGHT_K_ITEM = "edgar.8k.item_detected"
    EIGHT_K_FACTS = "edgar.8k.facts_extracted"
    FEED_GAP = "edgar.feed.gap_detected"
    # New form-specific subjects
    THIRTEEN_F_PARSED = "edgar.13f.parsed"
    THIRTEEN_DG_PARSED = "edgar.13dg.parsed"
    XBRL_PARSED = "edgar.xbrl.parsed"
    FUND_FILING_PARSED = "edgar.fund.parsed"


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class EventEnvelope:
    """Normalised domain event ready for outbox insertion.

    **Delivery semantics (at-least-once):**
    The default JSONL publisher writes to disk *before* marking the outbox
    row as published.  If the process crashes between those two steps, the
    event will be re-leased and appended again on restart.  Downstream
    consumers **must** treat ``event_id`` as a hard idempotency key and
    deduplicate accordingly.

    **Correction / revision semantics:**
    When a filing is re-parsed and the payload changes, a new immutable
    revision row is inserted into the outbox with the same ``event_id``
    but a different ``payload_hash``.  Prior revisions are preserved for
    full historical reconstruction and audit.  Consumers should use the
    highest ``commit_seq`` for a given ``event_id`` as the authoritative
    version.
    """
    event_id: str
    subject: str
    accession_number: str
    payload: dict[str, Any]
    created_at: str  # ISO-8601 — envelope construction time (pre-commit)
    payload_hash: str = ""  # SHA-256 of canonical payload JSON
    lease_token: str | None = None  # Set during lease_pending()
    commit_seq: int | None = None   # DB-assigned monotonic sequence
    committed_at: str | None = None # ISO-8601 — actual DB commit time

    @staticmethod
    def new(
        subject: str,
        accession_number: str,
        payload: dict[str, Any],
        *,
        business_key: str | None = None,
    ) -> EventEnvelope:
        """Create a new event envelope.

        When *business_key* is ``None`` (e.g. feed-gap events with no natural
        key), a random UUID is used as before.
        """
        if business_key is not None:
            raw = f"{subject}:{business_key}"
            event_id = hashlib.sha256(raw.encode()).hexdigest()[:32]
        else:
            event_id = uuid.uuid4().hex
        payload_json = dump_json(payload)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()[:16]
        return EventEnvelope(
            event_id=event_id,
            subject=subject,
            accession_number=accession_number,
            payload=payload,
            created_at=utcnow().isoformat(),
            payload_hash=payload_hash,
        )


# ---------------------------------------------------------------------------
# Outbox store — SQLite-backed durable event queue
# ---------------------------------------------------------------------------

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_events (
    commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    published_at TEXT,
    leased_at TEXT,
    lease_token TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status
    ON outbox_events(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_accession
    ON outbox_events(accession_number);
CREATE INDEX IF NOT EXISTS idx_outbox_event_id
    ON outbox_events(event_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_payload
    ON outbox_events(event_id, payload_hash);
"""

# Maximum publish attempts before marking as permanently failed
MAX_PUBLISH_ATTEMPTS = 10


class OutboxStore:
    """Read / write outbox rows using an externally-provided connection.

    All write methods accept an explicit ``sqlite3.Connection`` so that the
    caller can bundle outbox inserts with other DB writes in the **same**
    transaction.  Read methods (``lease_pending``, ``mark_published``, etc.)
    open their own connections via the storage helper.
    """

    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        publish_retry_base_seconds: float = 2.0,
    ) -> None:
        self.storage = storage
        self._publish_retry_base = publish_retry_base_seconds

    # --- Schema ---

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create the outbox table if it does not exist.

        This is the **canonical** and only definition of the outbox DDL.
        Called once during daemon startup, after ``SQLiteStorage.initialize()``
        has created the core ingestion schema.
        """
        conn.executescript(_OUTBOX_SCHEMA)
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(outbox_events)").fetchall()
        }
        if "lease_token" not in cols:
            conn.execute("ALTER TABLE outbox_events ADD COLUMN lease_token TEXT")
            logger.info("outbox migration: added lease_token column")
        if "commit_seq" not in cols:
            # Existing table with event_id TEXT PRIMARY KEY — need to migrate.
            # SQLite cannot add AUTOINCREMENT to an existing table, so for
            # existing DBs we add a plain INTEGER column.  New rows will get
            # monotonic values via explicit MAX(commit_seq)+1 computation in
            # insert_event(); old rows get NULL which sorts before any integer
            # (desirable: old events publish first via COALESCE in lease_pending).
            conn.execute("ALTER TABLE outbox_events ADD COLUMN commit_seq INTEGER")
            logger.info("outbox migration: added commit_seq column")
        if "payload_hash" not in cols:
            conn.execute(
                "ALTER TABLE outbox_events ADD COLUMN payload_hash TEXT NOT NULL DEFAULT ''"
            )
            logger.info("outbox migration: added payload_hash column for correction semantics")
        # Ensure monotonic commit_seq index exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_commit_seq "
            "ON outbox_events(status, commit_seq)"
        )
        if "committed_at" not in cols:
            conn.execute("ALTER TABLE outbox_events ADD COLUMN committed_at TEXT")
            logger.info("outbox migration: added committed_at column for transaction-time accuracy")

        # --- Migration: remove legacy UNIQUE constraint on event_id ---
        # Old schemas had ``event_id TEXT NOT NULL UNIQUE`` which prevents
        # storing multiple immutable revision rows per event_id.  SQLite
        # cannot drop a column-level UNIQUE constraint, so we detect the
        # autoindex and recreate the table without it.
        has_unique_event_id = any(
            row[1] == "sqlite_autoindex_outbox_events_1"
            for row in conn.execute("PRAGMA index_list(outbox_events)").fetchall()
        )
        if has_unique_event_id:
            logger.info(
                "outbox migration: removing UNIQUE constraint on event_id "
                "to support immutable revision rows"
            )
            conn.executescript("""
                CREATE TABLE outbox_events_new (
                    commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    publish_attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    committed_at TEXT,
                    published_at TEXT,
                    leased_at TEXT,
                    lease_token TEXT
                );
                INSERT INTO outbox_events_new
                    (commit_seq, event_id, accession_number, subject, payload_json,
                     payload_hash, status, publish_attempts, next_attempt_at,
                     last_error, created_at, committed_at, published_at,
                     leased_at, lease_token)
                SELECT commit_seq, event_id, accession_number, subject, payload_json,
                       payload_hash, status, publish_attempts, next_attempt_at,
                       last_error, created_at, committed_at, published_at,
                       leased_at, lease_token
                FROM outbox_events;
                DROP TABLE outbox_events;
                ALTER TABLE outbox_events_new RENAME TO outbox_events;
                CREATE INDEX IF NOT EXISTS idx_outbox_status
                    ON outbox_events(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_outbox_accession
                    ON outbox_events(accession_number);
                CREATE INDEX IF NOT EXISTS idx_outbox_event_id
                    ON outbox_events(event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_payload
                    ON outbox_events(event_id, payload_hash);
                CREATE INDEX IF NOT EXISTS idx_outbox_commit_seq
                    ON outbox_events(status, commit_seq);
            """)
            logger.info("outbox migration: table recreated without UNIQUE on event_id")

    # --- Transactional writes (caller owns the connection & transaction) ---

    def insert_event(self, conn: sqlite3.Connection, envelope: EventEnvelope) -> None:
        """Insert a single outbox event — caller must commit.

        **Immutable revision semantics:** Each call inserts a new row.  If an
        event with the same ``event_id`` *and* ``payload_hash`` already exists,
        the insert is a no-op (idempotent).  If the same ``event_id`` is
        emitted with a *different* ``payload_hash``, a brand-new revision row
        is inserted and queued as ``pending``.  Prior revisions are **never**
        modified or deleted — they remain fully queryable for audit / replay.

        Downstream consumers should use the highest ``commit_seq`` for a given
        ``event_id`` as the authoritative version.

        After insert, the DB-assigned ``commit_seq`` is written back onto the
        envelope for downstream use.
        """
        payload_json = dump_json(envelope.payload)
        payload_hash = envelope.payload_hash or hashlib.sha256(payload_json.encode()).hexdigest()[:16]

        # Stamp committed_at inside the transaction for accurate commit-time
        # semantics.  This replaces the pre-commit created_at for partitioning
        # and ordering purposes.
        committed_now = utcnow().isoformat()

        # Check if an identical revision (same event_id + payload_hash) exists.
        # The UNIQUE index on (event_id, payload_hash) enforces this at the DB
        # level, but we check explicitly to distinguish "idempotent no-op" from
        # "new revision" for logging and commit_seq back-fill.
        existing = conn.execute(
            "SELECT commit_seq FROM outbox_events "
            "WHERE event_id = ? AND payload_hash = ?",
            (envelope.event_id, payload_hash),
        ).fetchone()

        if existing is not None:
            # Identical payload already stored — idempotent no-op.
            envelope.commit_seq = existing[0] if existing[0] is not None else existing["commit_seq"]
            return

        # Either a brand-new event_id or a correction with a different
        # payload_hash.  Insert a new immutable revision row.
        is_correction = conn.execute(
            "SELECT 1 FROM outbox_events WHERE event_id = ? LIMIT 1",
            (envelope.event_id,),
        ).fetchone() is not None

        conn.execute(
            """INSERT INTO outbox_events
               (event_id, accession_number, subject, payload_json, payload_hash,
                status, publish_attempts, created_at, committed_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                envelope.event_id,
                envelope.accession_number,
                envelope.subject,
                payload_json,
                payload_hash,
                envelope.created_at,
                committed_now,
            ),
        )
        envelope.committed_at = committed_now

        # For migrated schemas where commit_seq is a plain column (not PK),
        # it will be NULL after the insert above.  Backfill it from the max.
        row = conn.execute(
            "SELECT commit_seq FROM outbox_events "
            "WHERE event_id = ? AND payload_hash = ?",
            (envelope.event_id, payload_hash),
        ).fetchone()
        if row and row[0] is None:
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(commit_seq), 0) + 1 FROM outbox_events"
            ).fetchone()[0]
            conn.execute(
                "UPDATE outbox_events SET commit_seq = ? "
                "WHERE event_id = ? AND payload_hash = ?",
                (next_seq, envelope.event_id, payload_hash),
            )
            envelope.commit_seq = next_seq
        elif row:
            envelope.commit_seq = row[0]

        if is_correction:
            logger.info(
                "correction detected for event_id=%s subject=%s: "
                "new revision inserted (new_hash=%s) with commit_seq=%s; "
                "prior revisions preserved",
                envelope.event_id, envelope.subject, payload_hash,
                envelope.commit_seq,
            )

    def insert_events(self, conn: sqlite3.Connection, envelopes: list[EventEnvelope]) -> None:
        """Insert multiple outbox events — caller must commit."""
        for env in envelopes:
            self.insert_event(conn, env)

    # --- Publisher-side reads (own connection) ---

    def lease_pending(self, limit: int = 200, lease_seconds: int = 60) -> list[EventEnvelope]:
        """Claim up to *limit* pending events for publication.
        Before selecting candidates, any rows whose lease has expired
        (leased longer than ``lease_seconds`` ago) are reclaimed back to
        ``pending`` so they become eligible again.

        Events are leased in ``commit_seq`` order — a DB-assigned monotonic
        sequence that reflects true commit order, not wall-clock timestamps.

        Callers should invoke this method in a **drain loop** — calling
        repeatedly until the returned list is empty — so that backlogs
        larger than *limit* are fully drained before the caller sleeps.
        """
        now = utcnow()
        now_iso = now.isoformat()
        results: list[EventEnvelope] = []
        with self.storage._conn() as conn:
            # Step 0: Reclaim expired leases so they become re-eligible
            lease_cutoff = (now - timedelta(seconds=lease_seconds)).isoformat()
            conn.execute(
                """UPDATE outbox_events
                   SET status = 'pending', leased_at = NULL, lease_token = NULL
                   WHERE status = 'leased' AND leased_at < ?""",
                (lease_cutoff,),
            )

            # Step 1: Identify candidates — ordered by commit_seq for strict
            # monotonic delivery, falling back to created_at for legacy rows
            # where commit_seq may be NULL.
            candidates = conn.execute(
                """SELECT commit_seq, event_id, accession_number, subject,
                          payload_json, payload_hash, created_at, committed_at
                   FROM outbox_events
                   WHERE status = 'pending'
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY COALESCE(commit_seq, -1) ASC, created_at ASC
                   LIMIT ?""",
                (now_iso, limit),
            ).fetchall()

            # Step 2: Atomically claim each candidate with a unique lease_token
            for row in candidates:
                token = uuid.uuid4().hex
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = 'leased', leased_at = ?, lease_token = ?
                       WHERE commit_seq = ? AND status = 'pending'""",
                    (now_iso, token, row["commit_seq"]),
                )
                if cursor.rowcount == 1:
                    results.append(EventEnvelope(
                        event_id=row["event_id"],
                        subject=row["subject"],
                        accession_number=row["accession_number"],
                        payload=json.loads(row["payload_json"]),
                        created_at=row["created_at"],
                        payload_hash=row["payload_hash"] or "",
                        lease_token=token,
                        commit_seq=row["commit_seq"],
                        committed_at=row["committed_at"],
                    ))
            conn.commit()
        return results

    def mark_published(
        self,
        event_id: str,
        lease_token: str | None = None,
        *,
        commit_seq: int | None = None,
    ) -> bool:
        """Mark an event revision as published.

        When ``commit_seq`` is provided it is used as the primary row key,
        which is required now that multiple revision rows can share the same
        ``event_id``.  The ``event_id`` parameter is kept for backward
        compatibility but is only used when ``commit_seq`` is ``None``.

        Returns True if the row was updated, False if the lease was stale.
        """
        now_iso = utcnow().isoformat()
        with self.storage._conn() as conn:
            if commit_seq is not None and lease_token is not None:
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = 'published', published_at = ?, last_error = NULL,
                           lease_token = NULL
                       WHERE commit_seq = ? AND status = 'leased' AND lease_token = ?""",
                    (now_iso, commit_seq, lease_token),
                )
            elif commit_seq is not None:
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = 'published', published_at = ?, last_error = NULL,
                           lease_token = NULL
                       WHERE commit_seq = ?""",
                    (now_iso, commit_seq),
                )
            elif lease_token is not None:
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = 'published', published_at = ?, last_error = NULL,
                           lease_token = NULL
                       WHERE event_id = ? AND status = 'leased' AND lease_token = ?""",
                    (now_iso, event_id, lease_token),
                )
            else:
                # Backward-compatible path (no lease verification).
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = 'published', published_at = ?, last_error = NULL,
                           lease_token = NULL
                       WHERE event_id = ?""",
                    (now_iso, event_id),
                )
            conn.commit()
            if lease_token is not None and cursor.rowcount == 0:
                logger.warning(
                    "mark_published: stale lease for event_id=%s commit_seq=%s "
                    "(token mismatch or expired)",
                    event_id, commit_seq,
                )
                return False
            return True

    def mark_failed(
        self,
        event_id: str,
        error: str,
        lease_token: str | None = None,
        *,
        commit_seq: int | None = None,
    ) -> bool:
        """Record a publish failure with exponential backoff for next attempt.

        When ``commit_seq`` is provided it is used as the primary row key,
        which is required now that multiple revision rows can share the same
        ``event_id``.

        When a ``lease_token`` is provided, the update is guarded by
        ``status = 'leased' AND lease_token = ?`` in a **single** UPDATE
        statement — matching the ``mark_published()`` pattern.  This
        prevents the stale-lease TOCTOU race.
        """
        now = utcnow()
        with self.storage._conn() as conn:
            # Look up current attempts — prefer commit_seq when available
            if commit_seq is not None:
                row = conn.execute(
                    "SELECT publish_attempts FROM outbox_events WHERE commit_seq = ?",
                    (commit_seq,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT publish_attempts FROM outbox_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            attempts = (row["publish_attempts"] if row else 0) + 1
            if attempts >= MAX_PUBLISH_ATTEMPTS:
                new_status = "failed"
                next_at = None
            else:
                new_status = "pending"
                backoff = min(3600, self._publish_retry_base * (2 ** attempts))
                next_at = (now + timedelta(seconds=backoff)).isoformat()

            if commit_seq is not None and lease_token is not None:
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = ?, publish_attempts = ?, last_error = ?,
                           next_attempt_at = ?, leased_at = NULL, lease_token = NULL
                       WHERE commit_seq = ? AND status = 'leased' AND lease_token = ?""",
                    (new_status, attempts, error[:1000], next_at, commit_seq, lease_token),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    logger.warning(
                        "mark_failed: stale lease for event_id=%s commit_seq=%s "
                        "(token mismatch or expired)",
                        event_id, commit_seq,
                    )
                    return False
            elif commit_seq is not None:
                conn.execute(
                    """UPDATE outbox_events
                       SET status = ?, publish_attempts = ?, last_error = ?,
                           next_attempt_at = ?, leased_at = NULL, lease_token = NULL
                       WHERE commit_seq = ?""",
                    (new_status, attempts, error[:1000], next_at, commit_seq),
                )
                conn.commit()
            elif lease_token is not None:
                # Single conditional UPDATE — no separate pre-check.
                cursor = conn.execute(
                    """UPDATE outbox_events
                       SET status = ?, publish_attempts = ?, last_error = ?,
                           next_attempt_at = ?, leased_at = NULL, lease_token = NULL
                       WHERE event_id = ? AND status = 'leased' AND lease_token = ?""",
                    (new_status, attempts, error[:1000], next_at, event_id, lease_token),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    logger.warning(
                        "mark_failed: stale lease for event_id=%s (token mismatch or expired)",
                        event_id,
                    )
                    return False
            else:
                # Backward-compatible path (no lease verification).
                conn.execute(
                    """UPDATE outbox_events
                       SET status = ?, publish_attempts = ?, last_error = ?,
                           next_attempt_at = ?, leased_at = NULL, lease_token = NULL
                       WHERE event_id = ?""",
                    (new_status, attempts, error[:1000], next_at, event_id),
                )
                conn.commit()
            return True

    def reset_stale_leases(self, stale_seconds: int = 120) -> int:
        """Reset events that were leased but never published (crash recovery)."""
        cutoff = (utcnow() - timedelta(seconds=stale_seconds)).isoformat()
        with self.storage._conn() as conn:
            cursor = conn.execute(
                """UPDATE outbox_events
                   SET status = 'pending', leased_at = NULL, lease_token = NULL
                   WHERE status = 'leased' AND leased_at < ?""",
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount

    def pending_count(self) -> int:
        with self.storage._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE status IN ('pending', 'leased')",
            ).fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# Shared directory-fsync helper
# ---------------------------------------------------------------------------

def _fsync_directory(dir_path: Path) -> None:
    """Fsync a directory so that new or renamed entries are durable.

    This is the single implementation used by both ``ArtifactWriter`` (after
    atomic renames) and the JSONL publishers (after creating a new daily
    file).  Centralising the pattern avoids subtle divergence between the
    two durability paths.
    """
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# Tracks file paths whose parent directory has already been fsynced in the
# current process lifetime.  On first write to each daily JSONL file, the
# publisher calls ``_fsync_directory``; subsequent appends to the same file
# skip the redundant syscall.  This eliminates the TOCTOU race inherent in
# ``not target.exists()`` — even if a previous process created the file but
# crashed before its dir-fsync, the current process will re-issue the
# dir-fsync on its first access.
_dir_synced_paths: set[str] = set()


# ---------------------------------------------------------------------------
# Artifact writer — atomic filesystem writes
# ---------------------------------------------------------------------------

class ArtifactWriter:
    """Write files atomically: temp → fsync → rename → dir-fsync.

    This ensures that the final path either contains the complete data or
    does not exist — there are no partially-written artifacts.  The
    directory fsync after rename ensures the rename itself is durable
    even on filesystems where metadata writes are lazy.
    """

    @staticmethod
    def write_atomic(target_path: Path, data: bytes) -> str:
        """Write *data* to *target_path* atomically.  Returns SHA-256 hex digest."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target_path.parent),
            prefix=".tmp_",
            suffix=target_path.suffix,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = -1  # mark as closed
            os.rename(tmp_path, str(target_path))
            # Fsync the parent directory so the rename is durable.
            _fsync_directory(target_path.parent)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return sha256_hex(data)

    @staticmethod
    async def write_atomic_async(target_path: Path, data: bytes) -> str:
        """Async variant — offloads blocking fsync/rename to a thread pool.

        This prevents event-loop stalls from disk flushes and directory
        syncs, which is critical for low-latency trading pipelines.
        """
        import asyncio
        return await asyncio.to_thread(ArtifactWriter.write_atomic, target_path, data)


# ---------------------------------------------------------------------------
# Event construction helpers — single source of truth in event_builders.py
#
# These thin wrappers use lazy imports to avoid circular dependencies
# (event_builders imports EventEnvelope from this module).  All event
# construction logic lives exclusively in event_builders.py.
# ---------------------------------------------------------------------------

def build_filing_retrieved_event(
    accession_number: str,
    archive_cik: str,
    form_type: str,
    company_name: str,
    **kwargs: Any,
) -> EventEnvelope:
    from event_builders import build_filing_retrieved_event as _impl
    return _impl(accession_number, archive_cik, form_type, company_name, **kwargs)


def build_form4_event(
    accession_number: str,
    form4: Form4Filing,
    out_form4_transactions_cap: int = 20,
    out_form4_owners_cap: int = 10,
) -> EventEnvelope:
    from event_builders import build_form4_event as _impl
    return _impl(accession_number, form4, out_form4_transactions_cap, out_form4_owners_cap)


def build_8k_events(
    accession_number: str,
    events: list[EightKEvent],
) -> list[EventEnvelope]:
    from event_builders import build_8k_events as _impl
    return _impl(accession_number, events)


def build_filing_failed_event(
    accession_number: str,
    archive_cik: str,
    form_type: str,
    error: str,
    attempt_no: int = 1,
) -> EventEnvelope:
    from event_builders import build_filing_failed_event as _impl
    return _impl(accession_number, archive_cik, form_type, error, attempt_no)


def build_feed_gap_event(
    watermark_ts: str,
    pages_checked: int,
) -> EventEnvelope:
    from event_builders import build_feed_gap_event as _impl
    return _impl(watermark_ts, pages_checked)


# ---------------------------------------------------------------------------
# Default file-based event publisher (JSON-lines)
# ---------------------------------------------------------------------------

import asyncio as _asyncio


def _jsonl_write_sync(
    target: Path,
    line: str,
) -> None:
    """Blocking write of a single JSON line — runs in a thread."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    # Fsync the parent directory on the first write to each daily file
    # within the current process.  This uses a per-process tracking set
    # rather than ``not target.exists()`` to avoid a TOCTOU race: if a
    # prior process created the file but crashed before its dir-fsync,
    # the directory entry could be non-durable yet ``target.exists()``
    # would return True, skipping the critical fsync.  The tracking set
    # guarantees at least one dir-fsync per file per process lifetime.
    target_key = str(target)
    if target_key not in _dir_synced_paths:
        _fsync_directory(target.parent)
        _dir_synced_paths.add(target_key)


def _event_date_str(envelope: EventEnvelope) -> str:
    """Extract the date portion for file partitioning.

    Prefers ``committed_at`` (actual DB transaction time) over ``created_at``
    (pre-commit construction time) to avoid day-boundary skew when the
    system is busy or a transaction crosses midnight UTC.
    Falls back to ``created_at``, then to UTC now.
    """
    ts = envelope.committed_at or envelope.created_at
    try:
        return ts[:10]  # "YYYY-MM-DD" prefix of ISO-8601
    except (TypeError, IndexError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def jsonl_publish_callback(
    envelope: EventEnvelope,
    publish_dir: Path,
) -> None:
    """Append a single event as a JSON line to a date-partitioned file.

    This is the default publisher for the CLI launcher.  Events are written
    to ``{publish_dir}/events-YYYY-MM-DD.jsonl``, one JSON object per line.
    Downstream consumers (e.g. an execution layer process on the same VM)
    can tail or inotify-watch the directory for new lines.

    **At-least-once delivery:** The JSONL append + fsync happens *before*
    the outbox row is marked published.  A crash between those two steps
    will cause the event to be re-appended on restart.  Consumers MUST
    deduplicate by ``event_id``.

    Day-partitioning is based on the event's ``committed_at`` timestamp
    (transaction commit time), not the pre-commit ``created_at``, so
    delayed publications land in the semantically correct partition.

    All blocking I/O (open, write, flush, fsync) is offloaded to a thread
    via ``asyncio.to_thread`` so the event loop is never stalled.
    """
    day_str = _event_date_str(envelope)
    target = publish_dir / f"events-{day_str}.jsonl"
    line = json.dumps({
        "event_id": envelope.event_id,
        "subject": envelope.subject,
        "accession_number": envelope.accession_number,
        "payload": envelope.payload,
        "payload_hash": envelope.payload_hash,
        "created_at": envelope.created_at,
        "committed_at": envelope.committed_at,
        "commit_seq": envelope.commit_seq,
    }, separators=(",", ":"), sort_keys=True)
    await _asyncio.to_thread(_jsonl_write_sync, target, line)


async def jsonl_publish_batch_callback(
    envelopes: list[EventEnvelope],
    publish_dir: Path,
) -> None:
    """Batch-write multiple events with a single fsync per day-file.

    More efficient than per-event fsync under burst load while still
    guaranteeing durability for the entire batch.  Events are grouped
    by their ``committed_at`` date so each lands in the correct partition.
    """
    if not envelopes:
        return
    # Group events by their semantic day (committed_at, not publish time)
    by_day: dict[str, list[str]] = {}
    for envelope in envelopes:
        day_str = _event_date_str(envelope)
        line = json.dumps({
            "event_id": envelope.event_id,
            "subject": envelope.subject,
            "accession_number": envelope.accession_number,
            "payload": envelope.payload,
            "payload_hash": envelope.payload_hash,
            "created_at": envelope.created_at,
            "committed_at": envelope.committed_at,
            "commit_seq": envelope.commit_seq,
        }, separators=(",", ":"), sort_keys=True)
        by_day.setdefault(day_str, []).append(line)

    def _write_batch() -> None:
        publish_dir.mkdir(parents=True, exist_ok=True)
        for day_str, lines in by_day.items():
            target = publish_dir / f"events-{day_str}.jsonl"
            with open(target, "a", encoding="utf-8") as f:
                for ln in lines:
                    f.write(ln + "\n")
                f.flush()
                os.fsync(f.fileno())
            # Fsync parent directory on first access per process (see
            # _jsonl_write_sync for the TOCTOU rationale).
            target_key = str(target)
            if target_key not in _dir_synced_paths:
                _fsync_directory(target.parent)
                _dir_synced_paths.add(target_key)

    await _asyncio.to_thread(_write_batch)


def make_jsonl_publisher(publish_dir: Path) -> Callable[[EventEnvelope], Awaitable[None]]:
    """Return an async callback that writes events to JSON-lines files (thread-offloaded)."""
    async def _callback(envelope: EventEnvelope) -> None:
        await jsonl_publish_callback(envelope, publish_dir)
    return _callback


def make_jsonl_batch_publisher(publish_dir: Path) -> Callable[[list[EventEnvelope]], Awaitable[None]]:
    """Return an async callback that batch-writes events with a single fsync."""
    async def _callback(envelopes: list[EventEnvelope]) -> None:
        await jsonl_publish_batch_callback(envelopes, publish_dir)
    return _callback


# ---------------------------------------------------------------------------
# Filing commit service — the single orchestration point
# ---------------------------------------------------------------------------

class FilingCommitService:
    """Orchestrates the atomic commit of filing data + outbox events.

    Instead of ``retrieve_full()`` fanning side effects across many separate
    DB calls and commits, this service bundles everything into:

      1. Atomic artifact writes to the filesystem.
      2. A single DB transaction for filing metadata, parties, artifacts,
         structured extracts (Form 4 / 8-K), retrieval status, AND outbox
         events.

    The daemon's retrieval path calls ``commit_retrieved_filing()`` once and
    gets a single durable commit.
    """

    def __init__(self, storage: SQLiteStorage, outbox: OutboxStore) -> None:
        self.storage = storage
        self.outbox = outbox

    def commit_retrieved_filing(
        self,
        *,
        bundle: "RetrievedFilingBundle | None" = None,
        form_registry: "FormRegistry | None" = None,
        # --- Legacy keyword args (backward compatibility) ---
        accession_number: str | None = None,
        archive_cik: str | None = None,
        form_type: str | None = None,
        company_name: str | None = None,
        header: SubmissionHeader | None = None,
        canonical_cik: str | None = None,
        canonical_name: str | None = None,
        canonical_name_normalized: str | None = None,
        txt_path: str | None = None,
        txt_sha256: str | None = None,
        raw_index_path: str | None = None,
        index_sha256: str | None = None,
        primary_doc_path: str | None = None,
        primary_sha256: str | None = None,
        primary_document_url: str | None = None,
        artifact: FilingArtifact | None = None,
        form4: Form4Filing | None = None,
        eight_k_events: list[EightKEvent] | None = None,
        retry_base_seconds: float = 2.0,
        out_form4_transactions_cap: int = 20,
        out_form4_owners_cap: int = 10,
    ) -> list[EventEnvelope]:
        """Commit all filing data + outbox events in a single transaction.

        Accepts either a ``RetrievedFilingBundle`` (new path) or the legacy
        keyword arguments for backward compatibility.  Returns the list of
        envelopes written.
        """
        from domain import RetrievedFilingBundle
        from form_registry import FormRegistry

        # --- Normalise inputs: bundle or legacy kwargs ---
        if bundle is not None:
            accession_number = bundle.accession_number
            archive_cik = bundle.archive_cik
            form_type = bundle.form_type
            company_name = bundle.company_name
            header = bundle.header
            canonical_cik = bundle.canonical_cik
            canonical_name = bundle.canonical_name
            canonical_name_normalized = bundle.canonical_name_normalized
            txt_path = bundle.txt_path
            txt_sha256 = bundle.txt_sha256
            raw_index_path = bundle.index_path
            index_sha256 = bundle.index_sha256
            primary_doc_path = bundle.primary_doc_path
            primary_sha256 = bundle.primary_sha256
            primary_document_url = bundle.primary_document_url
            artifact = bundle.artifact
            # Extract legacy form results for backward compat
            form4 = bundle.form_results.get("Form4Handler")
            _eight_k_raw = bundle.form_results.get("EightKHandler")
            # EightKHandler now returns a dict; unpack for legacy path
            if isinstance(_eight_k_raw, dict):
                eight_k_events = _eight_k_raw.get("events")
            elif isinstance(_eight_k_raw, list):
                eight_k_events = _eight_k_raw
            else:
                eight_k_events = None

        # Must have required fields by now
        assert accession_number is not None
        assert header is not None

        final_status = "retrieved" if primary_doc_path else "retrieved_partial"
        now = utcnow()
        now_iso = now.isoformat()

        # Build all outbox events
        envelopes: list[EventEnvelope] = []
        envelopes.append(build_filing_retrieved_event(
            accession_number=accession_number,
            archive_cik=archive_cik or "",
            form_type=form_type or "",
            company_name=company_name or "",
            issuer_cik=canonical_cik,
            issuer_name=canonical_name,
            status=final_status,
            txt_sha256=txt_sha256,
            primary_sha256=primary_sha256,
            primary_document_url=primary_document_url,
            acceptance_datetime=header.acceptance_datetime,
            filing_date=header.filed_as_of_date,
            header_form_type=header.form_type,
        ))

        # Build form-specific events via registry if available
        if bundle is not None and form_registry is not None:
            form_upper = (header.form_type or "").upper().strip()
            for handler in form_registry.handlers:
                handler_name = type(handler).__name__
                parsed = bundle.form_results.get(handler_name)
                if parsed is not None:
                    evts = handler.build_events(
                        accession_number,
                        parsed,
                        out_form4_transactions_cap=out_form4_transactions_cap,
                        out_form4_owners_cap=out_form4_owners_cap,
                    )
                    envelopes.extend(evts)
        else:
            # Legacy: hardcoded event building
            if form4 and (form4.transactions or form4.holdings):
                envelopes.append(build_form4_event(accession_number, form4, out_form4_transactions_cap, out_form4_owners_cap))
            if eight_k_events:
                envelopes.extend(build_8k_events(accession_number, eight_k_events))

        # Single transaction for everything
        with self.storage._conn() as conn:
            # Header metadata
            header_meta = normalized_header_metadata(header)
            conn.execute(
                "UPDATE filings SET header_metadata_json=?, updated_at=? WHERE accession_number=?",
                (dump_json(header_meta), now_iso, accession_number),
            )

            # Canonical issuer promotion
            if canonical_cik or canonical_name:
                conn.execute(
                    """UPDATE filings SET
                        issuer_cik=?, issuer_name=?, issuer_name_normalized=?,
                        updated_at=?
                    WHERE accession_number=?""",
                    (canonical_cik, canonical_name, canonical_name_normalized,
                     now_iso, accession_number),
                )

            # Filing parties
            if header.parties:
                for p in header.parties:
                    if p.cik is None:
                        conn.execute(
                            "DELETE FROM filing_parties "
                            "WHERE accession_number=? AND role=? AND cik IS NULL",
                            (accession_number, p.role),
                        )
                    conn.execute(
                        """INSERT OR REPLACE INTO filing_parties
                        (accession_number, role, cik, name, name_normalized, created_at)
                        VALUES (?,?,?,?,?,?)""",
                        (accession_number, p.role, p.cik, p.name, p.name_normalized, now_iso),
                    )

            # Artifact record
            if artifact:
                conn.execute(
                    """INSERT OR REPLACE INTO filing_documents
                    (accession_number, artifact_type, source_url, local_path,
                     sha256, content_type, metadata_json, created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (artifact.accession_number, artifact.artifact_type, artifact.source_url,
                     str(artifact.local_path), artifact.sha256, artifact.content_type,
                     dump_json(artifact.metadata), now_iso),
                )

            # Form-specific persistence via registry
            if bundle is not None and form_registry is not None:
                for handler in form_registry.handlers:
                    handler_name = type(handler).__name__
                    parsed = bundle.form_results.get(handler_name)
                    if parsed is not None:
                        handler.persist(conn, accession_number, parsed, now_iso)
            else:
                # Legacy: hardcoded form persistence
                conn.execute(
                    "DELETE FROM form4_transactions WHERE accession_number=?",
                    (accession_number,),
                )
                if form4 and form4.transactions:
                    for txn in form4.transactions:
                        conn.execute(
                            """INSERT INTO form4_transactions (
                                accession_number, issuer_cik, issuer_name, issuer_ticker,
                                reporting_owner_cik, reporting_owner_name,
                                is_director, is_officer, officer_title, is_ten_pct_owner,
                                security_title, transaction_date, transaction_code,
                                shares, price_per_share, acquired_disposed,
                                shares_owned_after, direct_indirect, is_derivative, created_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                accession_number, txn.issuer_cik, txn.issuer_name,
                                txn.issuer_ticker, txn.reporting_owner_cik,
                                txn.reporting_owner_name, int(txn.is_director),
                                int(txn.is_officer), txn.officer_title,
                                int(txn.is_ten_pct_owner), txn.security_title,
                                txn.transaction_date, txn.transaction_code,
                                txn.shares, txn.price_per_share, txn.acquired_disposed,
                                txn.shares_owned_after, txn.direct_indirect,
                                int(txn.is_derivative), now_iso,
                            ),
                        )
                conn.execute(
                    "DELETE FROM form4_holdings WHERE accession_number=?",
                    (accession_number,),
                )
                if form4 and form4.holdings:
                    for h in form4.holdings:
                        conn.execute(
                            """INSERT INTO form4_holdings (
                                accession_number, issuer_cik, issuer_name, issuer_ticker,
                                reporting_owner_cik, reporting_owner_name,
                                is_director, is_officer, officer_title, is_ten_pct_owner,
                                security_title, shares_owned, direct_indirect,
                                nature_of_ownership, is_derivative, created_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                accession_number, h.issuer_cik, h.issuer_name,
                                h.issuer_ticker, h.reporting_owner_cik,
                                h.reporting_owner_name, int(h.is_director),
                                int(h.is_officer), h.officer_title,
                                int(h.is_ten_pct_owner), h.security_title,
                                h.shares_owned, h.direct_indirect,
                                h.nature_of_ownership, int(h.is_derivative), now_iso,
                            ),
                        )
                conn.execute(
                    "DELETE FROM eight_k_events WHERE accession_number=?",
                    (accession_number,),
                )
                if eight_k_events:
                    for ev in eight_k_events:
                        conn.execute(
                            """INSERT OR REPLACE INTO eight_k_events (
                                accession_number, item_number, item_description,
                                filing_date, company_name, cik, created_at
                            ) VALUES (?,?,?,?,?,?,?)""",
                            (ev.accession_number, ev.item_number, ev.item_description,
                             ev.filing_date, ev.company_name, ev.cik, now_iso),
                        )

            # Retrieval status update
            row = conn.execute(
                "SELECT attempt_count FROM filings WHERE accession_number=?",
                (accession_number,),
            ).fetchone()
            current_attempts = (row["attempt_count"] if row else 0) + 1

            if final_status == "retrieved_partial":
                backoff_seconds = min(3600, retry_base_seconds * (2 ** current_attempts))
                next_retry_iso: str | None = (now + timedelta(seconds=backoff_seconds)).isoformat()
                logger.info(
                    "commit_retrieved_filing partial retry backoff for %s: attempt #%d, "
                    "next_retry_at=%s (%.0fs, base=%.1fs)",
                    accession_number, current_attempts, next_retry_iso,
                    backoff_seconds, retry_base_seconds,
                )
            else:
                next_retry_iso = None

            conn.execute(
                """UPDATE filings SET
                    retrieval_status=?, updated_at=?, last_attempt_at=?,
                    attempt_count=?, next_retry_at=?,
                    raw_txt_path=?, raw_index_path=?, primary_doc_path=?,
                    txt_sha256=?, index_sha256=?, primary_sha256=?,
                    primary_document_url=COALESCE(?, primary_document_url)
                WHERE accession_number=?""",
                (
                    final_status, now_iso, now_iso, current_attempts,
                    next_retry_iso,
                    txt_path, raw_index_path, primary_doc_path,
                    txt_sha256, index_sha256, primary_sha256,
                    primary_document_url,
                    accession_number,
                ),
            )

            # Outbox events (in the same transaction!)
            self.outbox.insert_events(conn, envelopes)

            conn.commit()

        logger.info(
            "commit_retrieved_filing: acc=%s status=%s events=%d",
            accession_number, final_status, len(envelopes),
        )
        return envelopes

    def commit_failed_filing(
        self,
        accession_number: str,
        archive_cik: str,
        form_type: str,
        error: str,
        retry_base_seconds: float = 2.0,
    ) -> list[EventEnvelope]:
        """Record a retrieval failure + emit a failure event, transactionally."""
        now = utcnow()
        now_iso = now.isoformat()

        with self.storage._conn() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM filings WHERE accession_number=?",
                (accession_number,),
            ).fetchone()
            current_attempts = (row["attempt_count"] if row else 0) + 1
            backoff_seconds = min(3600, retry_base_seconds * (2 ** current_attempts))
            next_retry = (now + timedelta(seconds=backoff_seconds)).isoformat()

            envelope = build_filing_failed_event(
                accession_number, archive_cik, form_type, error,
                attempt_no=current_attempts,
            )

            conn.execute(
                """UPDATE filings SET
                    retrieval_status='retrieval_failed', updated_at=?,
                    last_attempt_at=?, attempt_count=?, next_retry_at=?
                WHERE accession_number=?""",
                (now_iso, now_iso, current_attempts, next_retry, accession_number),
            )

            self.outbox.insert_event(conn, envelope)
            conn.commit()

        logger.info(
            "commit_failed_filing: acc=%s attempt=%d next_retry=%.0fs",
            accession_number, current_attempts, backoff_seconds,
        )
        return [envelope]