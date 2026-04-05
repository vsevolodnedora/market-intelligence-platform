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

Design rationale (from the extension plan):

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
    FEED_GAP = "edgar.feed.gap_detected"


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class EventEnvelope:
    """Normalised domain event ready for outbox insertion."""
    event_id: str
    subject: str
    accession_number: str
    payload: dict[str, Any]
    created_at: str  # ISO-8601
    lease_token: str | None = None  # Set during lease_pending()
    commit_seq: int | None = None   # DB-assigned monotonic sequence

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
        return EventEnvelope(
            event_id=event_id,
            subject=subject,
            accession_number=accession_number,
            payload=payload,
            created_at=utcnow().isoformat(),
        )


# ---------------------------------------------------------------------------
# Outbox store — SQLite-backed durable event queue
# ---------------------------------------------------------------------------

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_events (
    commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    accession_number TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    leased_at TEXT,
    lease_token TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status
    ON outbox_events(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_accession
    ON outbox_events(accession_number);
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
        # Ensure monotonic commit_seq index exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_commit_seq "
            "ON outbox_events(status, commit_seq)"
        )

    # --- Transactional writes (caller owns the connection & transaction) ---

    def insert_event(self, conn: sqlite3.Connection, envelope: EventEnvelope) -> None:
        """Insert a single outbox event — caller must commit.

        Uses INSERT OR IGNORE on the UNIQUE(event_id) constraint so that
        duplicate events are silently skipped.  After insert, the DB-assigned
        ``commit_seq`` is written back onto the envelope for downstream use.

        On fresh schemas ``commit_seq`` is the INTEGER PRIMARY KEY
        AUTOINCREMENT and is assigned automatically.  On migrated schemas
        (where event_id was the original PK) ``commit_seq`` is a plain
        INTEGER column — in that case we explicitly compute the next value.
        """
        # Try the insert — commit_seq auto-increments on fresh schemas.
        conn.execute(
            """INSERT OR IGNORE INTO outbox_events
               (event_id, accession_number, subject, payload_json,
                status, publish_attempts, created_at)
               VALUES (?, ?, ?, ?, 'pending', 0, ?)""",
            (
                envelope.event_id,
                envelope.accession_number,
                envelope.subject,
                dump_json(envelope.payload),
                envelope.created_at,
            ),
        )
        # For migrated schemas where commit_seq is a plain column (not PK),
        # it will be NULL after the insert above.  Backfill it from the max.
        row = conn.execute(
            "SELECT commit_seq FROM outbox_events WHERE event_id = ?",
            (envelope.event_id,),
        ).fetchone()
        if row and row[0] is None:
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(commit_seq), 0) + 1 FROM outbox_events"
            ).fetchone()[0]
            conn.execute(
                "UPDATE outbox_events SET commit_seq = ? WHERE event_id = ?",
                (next_seq, envelope.event_id),
            )
            envelope.commit_seq = next_seq
        elif row:
            envelope.commit_seq = row[0]

    def insert_events(self, conn: sqlite3.Connection, envelopes: list[EventEnvelope]) -> None:
        """Insert multiple outbox events — caller must commit."""
        for env in envelopes:
            self.insert_event(conn, env)

    # --- Publisher-side reads (own connection) ---

    def lease_pending(self, limit: int = 50, lease_seconds: int = 60) -> list[EventEnvelope]:
        """Claim up to *limit* pending events for publication.
        Before selecting candidates, any rows whose lease has expired
        (leased longer than ``lease_seconds`` ago) are reclaimed back to
        ``pending`` so they become eligible again.

        Events are leased in ``commit_seq`` order — a DB-assigned monotonic
        sequence that reflects true commit order, not wall-clock timestamps.
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
                          payload_json, created_at
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
                       WHERE event_id = ? AND status = 'pending'""",
                    (now_iso, token, row["event_id"]),
                )
                if cursor.rowcount == 1:
                    results.append(EventEnvelope(
                        event_id=row["event_id"],
                        subject=row["subject"],
                        accession_number=row["accession_number"],
                        payload=json.loads(row["payload_json"]),
                        created_at=row["created_at"],
                        lease_token=token,
                        commit_seq=row["commit_seq"],
                    ))
            conn.commit()
        return results

    def mark_published(self, event_id: str, lease_token: str | None = None) -> bool:
        """Mark an event as published.

        Returns True if the row was updated, False if the lease was stale.
        """
        now_iso = utcnow().isoformat()
        with self.storage._conn() as conn:
            if lease_token is not None:
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
                    "mark_published: stale lease for event_id=%s (token mismatch or expired)",
                    event_id,
                )
                return False
            return True

    def mark_failed(self, event_id: str, error: str, lease_token: str | None = None) -> bool:
        """Record a publish failure with exponential backoff for next attempt.

        When a ``lease_token`` is provided, the update is guarded by
        ``status = 'leased' AND lease_token = ?`` in a **single** UPDATE
        statement — matching the ``mark_published()`` pattern.  This
        prevents the stale-lease TOCTOU race described in extension_plan2
        §5: a stale publisher can no longer pass a SELECT check, lose
        ownership, and then overwrite a now-published row.
        """
        now = utcnow()
        with self.storage._conn() as conn:
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

            if lease_token is not None:
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
            dir_fd = os.open(str(target_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
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
# Event construction helpers
# ---------------------------------------------------------------------------

def build_filing_retrieved_event(
    accession_number: str,
    archive_cik: str,
    form_type: str,
    company_name: str,
    *,
    issuer_cik: str | None = None,
    issuer_name: str | None = None,
    status: str = "retrieved",
    txt_sha256: str | None = None,
    primary_sha256: str | None = None,
    primary_document_url: str | None = None,
    acceptance_datetime: str | None = None,
    filing_date: str | None = None,
    header_form_type: str | None = None,
) -> EventEnvelope:
    subject = (
        EventSubjects.FILING_RETRIEVED
        if status == "retrieved"
        else EventSubjects.FILING_PARTIAL
    )
    return EventEnvelope.new(
        subject=subject,
        accession_number=accession_number,
        payload={
            "archive_cik": archive_cik,
            "form_type": form_type,
            "company_name": company_name,
            "issuer_cik": issuer_cik,
            "issuer_name": issuer_name,
            "status": status,
            "txt_sha256": txt_sha256,
            "primary_sha256": primary_sha256,
            "primary_document_url": primary_document_url,
            "acceptance_datetime": acceptance_datetime,
            "filing_date": filing_date,
            "header_form_type": header_form_type,
        },
        business_key=accession_number,
    )


def build_form4_event(
    accession_number: str,
    form4: Form4Filing,
) -> EventEnvelope:
    # Build compact transaction summaries for downstream consumers
    txn_summaries = []
    for txn in form4.transactions[:20]:  # Cap at 20 to bound payload size
        txn_summaries.append({
            "security_title": txn.security_title,
            "transaction_date": txn.transaction_date,
            "transaction_code": txn.transaction_code,
            "shares": txn.shares,
            "price_per_share": txn.price_per_share,
            "acquired_disposed": txn.acquired_disposed,
            "shares_owned_after": txn.shares_owned_after,
            "is_derivative": txn.is_derivative,
        })
    owner_summaries = []
    for owner in form4.reporting_owners[:10]:
        owner_summaries.append({
            "cik": owner.get("cik") if isinstance(owner, dict) else getattr(owner, "cik", None),
            "name": owner.get("name") if isinstance(owner, dict) else getattr(owner, "name", None),
            "is_director": owner.get("is_director", False) if isinstance(owner, dict) else getattr(owner, "is_director", False),
            "is_officer": owner.get("is_officer", False) if isinstance(owner, dict) else getattr(owner, "is_officer", False),
            "officer_title": owner.get("officer_title") if isinstance(owner, dict) else getattr(owner, "officer_title", None),
            "is_ten_pct_owner": owner.get("is_ten_pct_owner", False) if isinstance(owner, dict) else getattr(owner, "is_ten_pct_owner", False),
        })
    return EventEnvelope.new(
        subject=EventSubjects.FORM4_PARSED,
        accession_number=accession_number,
        payload={
            "issuer_cik": form4.issuer_cik,
            "issuer_name": form4.issuer_name,
            "issuer_ticker": form4.issuer_ticker,
            "transaction_count": len(form4.transactions),
            "holding_count": len(form4.holdings),
            "owner_count": len(form4.reporting_owners),
            "transactions": txn_summaries,
            "reporting_owners": owner_summaries,
        },
        business_key=accession_number,
    )


def build_8k_events(
    accession_number: str,
    events: list[EightKEvent],
) -> list[EventEnvelope]:
    return [
        EventEnvelope.new(
            subject=EventSubjects.EIGHT_K_ITEM,
            accession_number=accession_number,
            payload={
                "item_number": ev.item_number,
                "item_description": ev.item_description,
                "company_name": ev.company_name,
                "cik": ev.cik,
                "filing_date": ev.filing_date,
            },
            business_key=f"{accession_number}:{ev.item_number}",
        )
        for ev in events
    ]


def build_filing_failed_event(
    accession_number: str,
    archive_cik: str,
    form_type: str,
    error: str,
    attempt_no: int = 1,
) -> EventEnvelope:
    return EventEnvelope.new(
        subject=EventSubjects.FILING_FAILED,
        accession_number=accession_number,
        payload={
            "archive_cik": archive_cik,
            "form_type": form_type,
            "error": error[:500],
            "attempt_no": attempt_no,
        },
        business_key=f"{accession_number}:{attempt_no}",
    )


def build_feed_gap_event(
    watermark_ts: str,
    pages_checked: int,
) -> EventEnvelope:
    return EventEnvelope.new(
        subject=EventSubjects.FEED_GAP,
        accession_number="N/A",
        payload={
            "watermark_ts": watermark_ts,
            "pages_checked": pages_checked,
        },
    )


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
    is_new_file = not target.exists()
    with open(target, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    # When creating a new daily file, fsync the parent directory so the
    # directory entry itself is durable.  Without this, a crash after
    # the file fsync but before directory metadata flush could lose the
    # file on some filesystems.  (Extension plan §4D.)
    if is_new_file:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


async def jsonl_publish_callback(
    envelope: EventEnvelope,
    publish_dir: Path,
) -> None:
    """Append a single event as a JSON line to a date-partitioned file.

    This is the default publisher for the CLI launcher.  Events are written
    to ``{publish_dir}/events-YYYY-MM-DD.jsonl``, one JSON object per line.
    Downstream consumers (e.g. an execution layer process on the same VM)
    can tail or inotify-watch the directory for new lines.

    All blocking I/O (open, write, flush, fsync) is offloaded to a thread
    via ``asyncio.to_thread`` so the event loop is never stalled.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = publish_dir / f"events-{today_str}.jsonl"
    line = json.dumps({
        "event_id": envelope.event_id,
        "subject": envelope.subject,
        "accession_number": envelope.accession_number,
        "payload": envelope.payload,
        "created_at": envelope.created_at,
        "commit_seq": envelope.commit_seq,
    }, separators=(",", ":"), sort_keys=True)
    await _asyncio.to_thread(_jsonl_write_sync, target, line)


async def jsonl_publish_batch_callback(
    envelopes: list[EventEnvelope],
    publish_dir: Path,
) -> None:
    """Batch-write multiple events with a single fsync — thread-offloaded.

    More efficient than per-event fsync under burst load while still
    guaranteeing durability for the entire batch.
    """
    if not envelopes:
        return
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = publish_dir / f"events-{today_str}.jsonl"
    lines = []
    for envelope in envelopes:
        lines.append(json.dumps({
            "event_id": envelope.event_id,
            "subject": envelope.subject,
            "accession_number": envelope.accession_number,
            "payload": envelope.payload,
            "created_at": envelope.created_at,
            "commit_seq": envelope.commit_seq,
        }, separators=(",", ":"), sort_keys=True))

    def _write_batch() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not target.exists()
        with open(target, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
            f.flush()
            os.fsync(f.fileno())
        # Fsync parent directory when creating a new day-file
        if is_new_file:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

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
        accession_number: str,
        archive_cik: str,
        form_type: str,
        company_name: str,
        # Header data
        header: SubmissionHeader,
        canonical_cik: str | None = None,
        canonical_name: str | None = None,
        canonical_name_normalized: str | None = None,
        # Artifact paths + hashes (already written atomically)
        txt_path: str | None = None,
        txt_sha256: str | None = None,
        primary_doc_path: str | None = None,
        primary_sha256: str | None = None,
        primary_document_url: str | None = None,
        # Artifact record
        artifact: FilingArtifact | None = None,
        # Structured extracts
        form4: Form4Filing | None = None,
        eight_k_events: list[EightKEvent] | None = None,
        # Retry config
        retry_base_seconds: float = 2.0,
    ) -> list[EventEnvelope]:
        """Commit all filing data + outbox events in a single transaction.

        Returns the list of envelopes written (for logging / metrics).
        """
        final_status = "retrieved" if primary_doc_path else "retrieved_partial"
        now = utcnow()
        now_iso = now.isoformat()

        # Build all outbox events
        envelopes: list[EventEnvelope] = []
        envelopes.append(build_filing_retrieved_event(
            accession_number=accession_number,
            archive_cik=archive_cik,
            form_type=form_type,
            company_name=company_name,
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
        if form4 and (form4.transactions or form4.holdings):
            envelopes.append(build_form4_event(accession_number, form4))
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
                    # SQLite UNIQUE constraints treat NULLs as distinct, so
                    # INSERT OR REPLACE won't deduplicate rows where cik is NULL.
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

            # Form 4 transactions — always delete existing rows for this
            # accession so that a reparse with fewer (or zero) rows does not
            # leave stale data behind.
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

            # Form 4 holdings — unconditional delete
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

            # 8-K events — unconditional delete before insert
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
            # Read current attempt_count
            row = conn.execute(
                "SELECT attempt_count FROM filings WHERE accession_number=?",
                (accession_number,),
            ).fetchone()
            current_attempts = (row["attempt_count"] if row else 0) + 1

            # Compute next_retry_at for partial retrievals (matching legacy
            # update_retrieval_status behaviour); clear it for fully retrieved.
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
                    raw_txt_path=?, primary_doc_path=?,
                    txt_sha256=?, primary_sha256=?,
                    primary_document_url=COALESCE(?, primary_document_url)
                WHERE accession_number=?""",
                (
                    final_status, now_iso, now_iso, current_attempts,
                    next_retry_iso,
                    txt_path, primary_doc_path,
                    txt_sha256, primary_sha256,
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