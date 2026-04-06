"""SQLite persistence layer for the EDGAR ingestor.

Single module, single class.  The immediate win is extracting it from
the monolithic edgar_core.py — not splitting it further.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from domain import (
    EightKEvent,
    FilingArtifact,
    FilingDiscovery,
    FilingParty,
    FilingRecord,
    Form4Filing,
    MAX_FILING_RETRY_ATTEMPTS,
    RelevanceState,
    RetrievalStatus,
    _DEFAULT_RETRY_BASE_SECONDS,
    _TERMINAL_RELEVANCE_STATES,
    dump_json,
    get_logger,
    normalize_name,
    utcnow,
)


logger = get_logger(__name__)

_RETRIEVAL_UPDATABLE = frozenset({
    "raw_txt_path", "raw_index_path", "primary_doc_path",
    "txt_sha256", "index_sha256", "primary_sha256", "primary_document_url",
})


class SQLiteStorage:
    """Persistence layer for the ingestor.

    **Concurrency model:** Multiple async tasks open independent SQLite
    connections via ``_conn()`` and rely on WAL mode + ``busy_timeout``
    to serialize contention.  This is *not* a dedicated single-writer
    task — it is many writers serialized by SQLite WAL.  For the modest
    write volumes of an EDGAR ingest daemon this is acceptable, but
    callers should be aware that p99 latency couples to WAL checkpoint
    pressure under concurrent writes.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Enforce referential integrity on every connection.
        conn.execute("PRAGMA foreign_keys = ON")
        # busy_timeout is connection-local — must be set on every connection,
        # not just during initialize(), to avoid SQLITE_BUSY under contention.
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA busy_timeout=5000;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS filings (
                    accession_number TEXT PRIMARY KEY,
                    archive_cik TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    form_type TEXT NOT NULL,
                    filing_date TEXT,
                    accepted_at TEXT,
                    discovered_at TEXT,
                    source TEXT NOT NULL,
                    filing_href TEXT,
                    filing_index_url TEXT,
                    complete_txt_url TEXT,
                    hdr_sgml_url TEXT,
                    primary_document_url TEXT,

                    relevance_state TEXT NOT NULL DEFAULT 'unknown',
                    retrieval_status TEXT NOT NULL DEFAULT 'discovered',

                    issuer_cik TEXT,
                    issuer_name TEXT,
                    issuer_name_normalized TEXT,

                    discovery_metadata_json TEXT NOT NULL DEFAULT '{}',
                    header_metadata_json TEXT NOT NULL DEFAULT '{}',

                    raw_txt_path TEXT,
                    raw_index_path TEXT,
                    primary_doc_path TEXT,
                    txt_sha256 TEXT,
                    index_sha256 TEXT,
                    primary_sha256 TEXT,

                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    inactive_reason TEXT,

                    first_seen_at TEXT,
                    last_seen_at TEXT,

                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS filing_parties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession_number TEXT NOT NULL,
                    role TEXT NOT NULL,
                    cik TEXT,
                    name TEXT,
                    name_normalized TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(accession_number, role, cik),
                    FOREIGN KEY(accession_number) REFERENCES filings(accession_number)
                );

                CREATE TABLE IF NOT EXISTS filing_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession_number TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    content_type TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(accession_number, artifact_type, source_url),
                    FOREIGN KEY(accession_number) REFERENCES filings(accession_number)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    source TEXT PRIMARY KEY,
                    cursor_text TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS form4_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession_number TEXT NOT NULL,
                    issuer_cik TEXT,
                    issuer_name TEXT,
                    issuer_ticker TEXT,
                    reporting_owner_cik TEXT,
                    reporting_owner_name TEXT,
                    is_director INTEGER DEFAULT 0,
                    is_officer INTEGER DEFAULT 0,
                    officer_title TEXT,
                    is_ten_pct_owner INTEGER DEFAULT 0,
                    security_title TEXT,
                    transaction_date TEXT,
                    transaction_code TEXT,
                    shares REAL,
                    price_per_share REAL,
                    acquired_disposed TEXT,
                    shares_owned_after REAL,
                    direct_indirect TEXT,
                    is_derivative INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(accession_number) REFERENCES filings(accession_number)
                );

                CREATE TABLE IF NOT EXISTS form4_holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession_number TEXT NOT NULL,
                    issuer_cik TEXT,
                    issuer_name TEXT,
                    issuer_ticker TEXT,
                    reporting_owner_cik TEXT,
                    reporting_owner_name TEXT,
                    is_director INTEGER DEFAULT 0,
                    is_officer INTEGER DEFAULT 0,
                    officer_title TEXT,
                    is_ten_pct_owner INTEGER DEFAULT 0,
                    security_title TEXT,
                    shares_owned REAL,
                    direct_indirect TEXT,
                    nature_of_ownership TEXT,
                    is_derivative INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(accession_number) REFERENCES filings(accession_number)
                );

                CREATE TABLE IF NOT EXISTS eight_k_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession_number TEXT NOT NULL,
                    item_number TEXT NOT NULL,
                    item_description TEXT NOT NULL,
                    filing_date TEXT,
                    company_name TEXT,
                    cik TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(accession_number, item_number),
                    FOREIGN KEY(accession_number) REFERENCES filings(accession_number)
                );

                CREATE INDEX IF NOT EXISTS idx_filings_relevance
                    ON filings(relevance_state, retrieval_status);
                CREATE INDEX IF NOT EXISTS idx_filings_archive_cik
                    ON filings(archive_cik);
                CREATE INDEX IF NOT EXISTS idx_filings_issuer_cik
                    ON filings(issuer_cik);
                CREATE INDEX IF NOT EXISTS idx_form4_issuer_ticker
                    ON form4_transactions(issuer_ticker, transaction_date);
                CREATE INDEX IF NOT EXISTS idx_form4_owner
                    ON form4_transactions(reporting_owner_cik, transaction_date);
                CREATE INDEX IF NOT EXISTS idx_8k_item
                    ON eight_k_events(item_number, filing_date);
                CREATE INDEX IF NOT EXISTS idx_form4_holdings_issuer
                    ON form4_holdings(issuer_ticker);
                CREATE INDEX IF NOT EXISTS idx_form4_holdings_owner
                    ON form4_holdings(reporting_owner_cik);
            """)
            conn.commit()

            # --- Schema migrations for existing databases ---
            self._run_migrations(conn)

        logger.info("database initialized at %s", self.db_path)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply incremental schema migrations to existing databases.

        Each migration checks whether the column/index already exists before
        altering.  This is safe to run on every startup — migrations are
        idempotent.
        """
        # Migration 1: add is_derivative to form4_transactions
        cols = {row[1] for row in conn.execute("PRAGMA table_info(form4_transactions)").fetchall()}
        if "is_derivative" not in cols:
            conn.execute(
                "ALTER TABLE form4_transactions ADD COLUMN is_derivative INTEGER DEFAULT 0"
            )
            logger.info("migration: added is_derivative column to form4_transactions")
            conn.commit()

    def upsert_discovery(self, d: FilingDiscovery) -> bool:
        now = utcnow().isoformat()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT retrieval_status, discovery_metadata_json FROM filings WHERE accession_number=?",
                (d.accession_number,),
            ).fetchone()

            if existing is None:
                initial_meta = {**d.metadata, "_discovery_sources": [d.source]}
                conn.execute(
                    """INSERT INTO filings (
                        accession_number, archive_cik, company_name, form_type, filing_date,
                        accepted_at, discovered_at, source, filing_href,
                        filing_index_url, complete_txt_url, hdr_sgml_url, primary_document_url,
                        discovery_metadata_json, first_seen_at, last_seen_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        d.accession_number, d.archive_cik, d.company_name, d.form_type,
                        d.filing_date.isoformat() if d.filing_date else None,
                        d.accepted_at.isoformat() if d.accepted_at else None,
                        d.discovered_at.isoformat() if d.discovered_at else None,
                        d.source, d.filing_href, d.filing_index_url,
                        d.complete_txt_url, d.hdr_sgml_url, d.primary_document_url,
                        dump_json(initial_meta), now, now, now,
                    ),
                )
                conn.commit()
                logger.info(
                    "new filing discovered: acc=%s form=%s cik=%s company=%s source=%s",
                    d.accession_number, d.form_type, d.archive_cik, d.company_name, d.source,
                )
                return True

            # Existing — merge
            status = existing["retrieval_status"]
            is_retrieved = status == "retrieved"

            prev_meta = json.loads(existing["discovery_metadata_json"]) if existing["discovery_metadata_json"] else {}
            merged_meta = {**prev_meta, **d.metadata}
            sources_seen = prev_meta.get("_discovery_sources", [])
            if d.source not in sources_seen:
                sources_seen = sources_seen + [d.source]
            merged_meta["_discovery_sources"] = sources_seen
            merged_meta_json = dump_json(merged_meta)

            if is_retrieved:
                conn.execute(
                    """UPDATE filings SET
                        filing_date = COALESCE(filings.filing_date, ?),
                        accepted_at = COALESCE(filings.accepted_at, ?),
                        filing_href = COALESCE(filings.filing_href, ?),
                        filing_index_url = COALESCE(filings.filing_index_url, ?),
                        complete_txt_url = COALESCE(filings.complete_txt_url, ?),
                        hdr_sgml_url = COALESCE(filings.hdr_sgml_url, ?),
                        primary_document_url = COALESCE(filings.primary_document_url, ?),
                        discovery_metadata_json = ?,
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE accession_number = ?""",
                    (
                        d.filing_date.isoformat() if d.filing_date else None,
                        d.accepted_at.isoformat() if d.accepted_at else None,
                        d.filing_href, d.filing_index_url,
                        d.complete_txt_url, d.hdr_sgml_url, d.primary_document_url,
                        merged_meta_json, now, now, d.accession_number,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE filings SET
                        archive_cik = ?, company_name = ?, form_type = ?,
                        filing_date = COALESCE(?, filings.filing_date),
                        accepted_at = COALESCE(?, filings.accepted_at),
                        discovered_at = COALESCE(filings.discovered_at, ?),
                        source = CASE WHEN filings.source = 'latest_filings_atom'
                                 THEN filings.source ELSE ? END,
                        filing_href = COALESCE(?, filings.filing_href),
                        filing_index_url = COALESCE(?, filings.filing_index_url),
                        complete_txt_url = COALESCE(?, filings.complete_txt_url),
                        hdr_sgml_url = COALESCE(?, filings.hdr_sgml_url),
                        primary_document_url = COALESCE(?, filings.primary_document_url),
                        discovery_metadata_json = ?,
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE accession_number = ?""",
                    (
                        d.archive_cik, d.company_name, d.form_type,
                        d.filing_date.isoformat() if d.filing_date else None,
                        d.accepted_at.isoformat() if d.accepted_at else None,
                        d.discovered_at.isoformat() if d.discovered_at else None,
                        d.source, d.filing_href, d.filing_index_url,
                        d.complete_txt_url, d.hdr_sgml_url, d.primary_document_url,
                        merged_meta_json, now, now, d.accession_number,
                    ),
                )
            conn.commit()
            return False

    def update_relevance(
        self, accession: str, state: RelevanceState,
        *, issuer_cik: str | None = None, issuer_name: str | None = None,
    ) -> None:
        """Update a filing's relevance state.

        When setting a **terminal** header-gate outcome (``hdr_failed`` or
        ``unresolved``), also resets ``retrieval_status`` back to
        ``discovered`` so that stale ``queued`` status cannot cause the
        filing to be replayed into retrieval by ``list_stranded_work()``
        or startup replay.  This closes the state-machine bug described
        in extension_plan2 §3.
        """
        now = utcnow().isoformat()
        # Terminal header-gate outcomes must clear retrieval_status to
        # prevent stranded-work replay from sending them into retrieval.
        _terminal_hdr_states = (
            RelevanceState.HDR_FAILED.value,
            RelevanceState.UNRESOLVED.value,
        )
        reset_retrieval = state.value in _terminal_hdr_states
        with self._conn() as conn:
            if issuer_cik or issuer_name:
                if reset_retrieval:
                    conn.execute(
                        """UPDATE filings SET relevance_state=?,
                           retrieval_status='discovered',
                           issuer_cik=COALESCE(?,issuer_cik),
                           issuer_name=COALESCE(?,issuer_name),
                           issuer_name_normalized=COALESCE(?,issuer_name_normalized),
                           updated_at=? WHERE accession_number=?""",
                        (state.value, issuer_cik, issuer_name,
                         normalize_name(issuer_name) if issuer_name else None,
                         now, accession),
                    )
                else:
                    conn.execute(
                        """UPDATE filings SET relevance_state=?, issuer_cik=COALESCE(?,issuer_cik),
                           issuer_name=COALESCE(?,issuer_name),
                           issuer_name_normalized=COALESCE(?,issuer_name_normalized),
                           updated_at=? WHERE accession_number=?""",
                        (state.value, issuer_cik, issuer_name,
                         normalize_name(issuer_name) if issuer_name else None,
                         now, accession),
                    )
            else:
                if reset_retrieval:
                    conn.execute(
                        "UPDATE filings SET relevance_state=?, retrieval_status='discovered', "
                        "updated_at=? WHERE accession_number=?",
                        (state.value, now, accession),
                    )
                else:
                    conn.execute(
                        "UPDATE filings SET relevance_state=?, updated_at=? WHERE accession_number=?",
                        (state.value, now, accession),
                    )
            conn.commit()

    def set_hdr_transient_fail(
        self, accession: str,
        retry_base_seconds: float = _DEFAULT_RETRY_BASE_SECONDS,
    ) -> None:
        """Mark a header-gate failure as transient, with backoff for retry."""
        now = utcnow()
        now_iso = now.isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM filings WHERE accession_number=?",
                (accession,),
            ).fetchone()
            current_attempts = (row["attempt_count"] if row else 0) + 1
            backoff_seconds = min(3600, retry_base_seconds * (2 ** current_attempts))
            next_retry = (now + timedelta(seconds=backoff_seconds)).isoformat()
            conn.execute(
                """UPDATE filings SET
                    relevance_state=?, attempt_count=?,
                    last_attempt_at=?, next_retry_at=?, updated_at=?
                WHERE accession_number=?""",
                (RelevanceState.HDR_TRANSIENT_FAIL.value, current_attempts,
                 now_iso, next_retry, now_iso, accession),
            )
            conn.commit()
            logger.info(
                "hdr_transient_fail: acc=%s attempt=%d next_retry_at=%s",
                accession, current_attempts, next_retry,
            )

    def update_retrieval_status(
        self, accession: str, status: str,
        retry_base_seconds: float = _DEFAULT_RETRY_BASE_SECONDS,
        **fields: str | None,
    ) -> None:
        sets = ["retrieval_status=?", "updated_at=?", "last_attempt_at=?",
                "attempt_count=attempt_count+1"]
        now = utcnow()
        now_iso = now.isoformat()
        vals: list[str | None] = [status, now_iso, now_iso]

        # Compute next_retry_at for failed/partial statuses using exponential backoff.
        if status in (RetrievalStatus.RETRIEVAL_FAILED.value,
                      RetrievalStatus.RETRIEVED_PARTIAL.value,
                      "retrieval_failed", "retrieved_partial"):
            # Read current attempt_count to compute backoff.
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT attempt_count FROM filings WHERE accession_number=?",
                    (accession,),
                ).fetchone()
            current_attempts = (row["attempt_count"] if row else 0) + 1  # +1 for this attempt
            backoff_seconds = min(3600, retry_base_seconds * (2 ** current_attempts))
            next_retry = (now + timedelta(seconds=backoff_seconds)).isoformat()
            sets.append("next_retry_at=?")
            vals.append(next_retry)
            logger.info(
                "retry backoff for %s: attempt #%d, next_retry_at=%s (%.0fs, base=%.1fs)",
                accession, current_attempts, next_retry, backoff_seconds, retry_base_seconds,
            )
        else:
            # Clear next_retry_at for terminal statuses.
            sets.append("next_retry_at=?")
            vals.append(None)

        for k, v in fields.items():
            if k not in _RETRIEVAL_UPDATABLE:
                raise ValueError(f"Disallowed field: {k}")
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(accession)
        with self._conn() as conn:
            conn.execute(f"UPDATE filings SET {', '.join(sets)} WHERE accession_number=?", vals)
            conn.commit()

    def set_retrieval_queued(self, accession: str, *, force: bool = False) -> bool:
        """Mark a filing as queued for retrieval (without incrementing attempt_count).

        When *force* is False (default), this method **respects** the retry
        backoff schedule: if ``next_retry_at`` is in the future, the status
        change is refused and the method returns ``False``.  This prevents
        rediscovery, audit, and reconciliation flows from bypassing the
        exponential backoff clock.

        Returns True if the filing was actually set to queued, False if
        the request was refused because the filing is still cooling down.
        """
        now = utcnow()
        now_iso = now.isoformat()
        with self._conn() as conn:
            if not force:
                row = conn.execute(
                    "SELECT next_retry_at FROM filings WHERE accession_number=?",
                    (accession,),
                ).fetchone()
                if row and row["next_retry_at"]:
                    if row["next_retry_at"] > now_iso:
                        logger.debug(
                            "set_retrieval_queued refused for %s: cooling down until %s",
                            accession, row["next_retry_at"],
                        )
                        return False
            conn.execute(
                "UPDATE filings SET retrieval_status=?, updated_at=? WHERE accession_number=?",
                (RetrievalStatus.QUEUED.value, now_iso, accession),
            )
            conn.commit()
        return True

    def is_retry_cooling_down(self, accession: str) -> bool:
        """Return True if the filing has a future ``next_retry_at`` (still in backoff)."""
        now_iso = utcnow().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT next_retry_at FROM filings WHERE accession_number=?",
                (accession,),
            ).fetchone()
            if not row or not row["next_retry_at"]:
                return False
            return row["next_retry_at"] > now_iso

    def set_retrieval_in_progress(self, accession: str) -> None:
        """Mark a filing as actively being retrieved."""
        now = utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE filings SET retrieval_status=?, updated_at=? WHERE accession_number=?",
                (RetrievalStatus.IN_PROGRESS.value, now, accession),
            )
            conn.commit()

    def is_filing_terminal(self, accession: str) -> bool:
        """Return True if the filing's relevance + retrieval state means no more work is needed.

        Terminal means: relevance is resolved AND retrieval is complete (or irrelevant).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT relevance_state, retrieval_status FROM filings WHERE accession_number=?",
                (accession,),
            ).fetchone()
            if not row:
                return False
            rel = row["relevance_state"]
            ret = row["retrieval_status"]
            # Irrelevant or unmatched = no retrieval needed.
            if rel in (RelevanceState.IRRELEVANT.value, RelevanceState.DIRECT_UNMATCHED.value):
                return True
            # Header-gate terminal states: resolution settled, no further reclassification.
            if rel in (RelevanceState.HDR_FAILED.value, RelevanceState.UNRESOLVED.value):
                return True
            # Successfully retrieved = done.
            if ret == RetrievalStatus.RETRIEVED.value:
                return True
            return False

    def save_header_metadata(self, accession: str, header_meta: dict[str, object]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE filings SET header_metadata_json=?, updated_at=? WHERE accession_number=?",
                (dump_json(header_meta), utcnow().isoformat(), accession),
            )
            conn.commit()

    def promote_canonical_issuer(
        self, accession: str, issuer_cik: str | None,
        issuer_name: str | None, issuer_name_normalized: str | None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE filings SET
                    issuer_cik=?, issuer_name=?, issuer_name_normalized=?,
                    updated_at=?
                WHERE accession_number=?""",
                (issuer_cik, issuer_name, issuer_name_normalized,
                 utcnow().isoformat(), accession),
            )
            conn.commit()

    def save_filing_parties(self, accession: str, parties: list[FilingParty]) -> None:
        now = utcnow().isoformat()
        with self._conn() as conn:
            for p in parties:
                # SQLite UNIQUE constraints treat NULLs as distinct, so
                # INSERT OR REPLACE won't deduplicate rows where cik is NULL.
                # Explicitly remove any prior NULL-CIK row for this role first.
                if p.cik is None:
                    conn.execute(
                        "DELETE FROM filing_parties "
                        "WHERE accession_number=? AND role=? AND cik IS NULL",
                        (accession, p.role),
                    )
                conn.execute(
                    """INSERT OR REPLACE INTO filing_parties
                    (accession_number, role, cik, name, name_normalized, created_at)
                    VALUES (?,?,?,?,?,?)""",
                    (accession, p.role, p.cik, p.name, p.name_normalized, now),
                )
            conn.commit()

    def attach_artifact(self, a: FilingArtifact) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO filing_documents
                (accession_number,artifact_type,source_url,local_path,
                 sha256,content_type,metadata_json,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (a.accession_number, a.artifact_type, a.source_url,
                 str(a.local_path), a.sha256, a.content_type,
                 dump_json(a.metadata), utcnow().isoformat()),
            )
            conn.commit()

    def get_filing(self, accession: str) -> FilingRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT accession_number,archive_cik,form_type,relevance_state,"
                "retrieval_status,company_name,source,issuer_cik,issuer_name,"
                "attempt_count "
                "FROM filings WHERE accession_number=?", (accession,),
            ).fetchone()
            if not row:
                return None
            return FilingRecord(
                accession_number=row["accession_number"],
                archive_cik=row["archive_cik"],
                form_type=row["form_type"],
                relevance_state=row["relevance_state"],
                retrieval_status=row["retrieval_status"],
                company_name=row["company_name"],
                source=row["source"],
                issuer_cik=row["issuer_cik"],
                issuer_name=row["issuer_name"],
                attempt_count=row["attempt_count"],
            )

    def known_accessions(self) -> set[str]:
        with self._conn() as conn:
            return {
                row[0] for row in conn.execute(
                    "SELECT accession_number FROM filings"
                ).fetchall()
            }

    def accession_exists(self, accession: str) -> bool:
        """Check if a single accession number exists (uses the PK index).

        Prefer this over ``known_accessions()`` when checking one or a few
        accessions — it avoids materialising the entire filings table into a
        Python set.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM filings WHERE accession_number = ? LIMIT 1",
                (accession,),
            ).fetchone()
            return row is not None

    def accessions_exist_batch(self, accessions: list[str]) -> set[str]:
        """Return the subset of *accessions* that already exist in the DB."""
        if not accessions:
            return set()
        existing: set[str] = set()
        # SQLite has a limit on the number of variables in a single query.
        # Process in chunks of 500 to stay well within the limit.
        chunk_size = 500
        with self._conn() as conn:
            for i in range(0, len(accessions), chunk_size):
                chunk = accessions[i:i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT accession_number FROM filings "
                    f"WHERE accession_number IN ({placeholders})",
                    chunk,
                ).fetchall()
                existing.update(r["accession_number"] for r in rows)
        return existing

    def get_checkpoint(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cursor_text FROM checkpoints WHERE source=?", (key,),
            ).fetchone()
            return row["cursor_text"] if row else None

    def set_checkpoint(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints (source, cursor_text, updated_at) "
                "VALUES (?, ?, ?)",
                (key, value, utcnow().isoformat()),
            )
            conn.commit()

    def list_retry_candidates(self, limit: int = 50, max_attempts: int = MAX_FILING_RETRY_ATTEMPTS) -> list[FilingRecord]:
        """Return filings that are actually retryable.

        Only includes retrieval_failed and retrieved_partial rows that:
          - have a relevant or pending relevance state (not terminal)
          - have not exceeded the max attempt count
          - whose next_retry_at has passed (or is NULL for legacy rows)

        Excludes terminal relevance states (irrelevant, direct_unmatched,
        hdr_failed, unresolved) — these should never be retried for
        retrieval (extension_plan2 §3).
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT accession_number,archive_cik,form_type,relevance_state,"
                "retrieval_status,company_name,source,issuer_cik,issuer_name,"
                "attempt_count "
                "FROM filings "
                "WHERE retrieval_status IN ('retrieval_failed','retrieved_partial') "
                "AND relevance_state NOT IN ('irrelevant','direct_unmatched','hdr_failed','unresolved') "
                "AND attempt_count < ? "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY updated_at ASC LIMIT ?",
                (max_attempts, utcnow().isoformat(), limit),
            ).fetchall()
            results = [FilingRecord(
                accession_number=r["accession_number"],
                archive_cik=r["archive_cik"],
                form_type=r["form_type"],
                relevance_state=r["relevance_state"],
                retrieval_status=r["retrieval_status"],
                company_name=r["company_name"],
                source=r["source"],
                issuer_cik=r["issuer_cik"],
                issuer_name=r["issuer_name"],
                attempt_count=r["attempt_count"],
            ) for r in rows]
            if results:
                logger.info(
                    "found %d retry candidates (max_attempts=%d)",
                    len(results), max_attempts,
                )
            return results

    def list_hdr_pending(self, limit: int = 100) -> list[FilingRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT accession_number,archive_cik,form_type,relevance_state,"
                "retrieval_status,company_name,source,issuer_cik,issuer_name,"
                "attempt_count "
                "FROM filings "
                "WHERE relevance_state = 'hdr_pending' "
                "ORDER BY updated_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [FilingRecord(
                accession_number=r["accession_number"],
                archive_cik=r["archive_cik"],
                form_type=r["form_type"],
                relevance_state=r["relevance_state"],
                retrieval_status=r["retrieval_status"],
                company_name=r["company_name"],
                source=r["source"],
                issuer_cik=r["issuer_cik"],
                issuer_name=r["issuer_name"],
                attempt_count=r["attempt_count"],
            ) for r in rows]

    def list_hdr_transient_fail(
        self, limit: int = 100, max_attempts: int = MAX_FILING_RETRY_ATTEMPTS,
    ) -> list[FilingRecord]:
        """Return filings with transient header-gate failures eligible for retry."""
        now_iso = utcnow().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT accession_number,archive_cik,form_type,relevance_state,"
                "retrieval_status,company_name,source,issuer_cik,issuer_name,"
                "attempt_count "
                "FROM filings "
                "WHERE relevance_state = 'hdr_transient_fail' "
                "AND attempt_count < ? "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY updated_at ASC LIMIT ?",
                (max_attempts, now_iso, limit),
            ).fetchall()
            return [FilingRecord(
                accession_number=r["accession_number"],
                archive_cik=r["archive_cik"],
                form_type=r["form_type"],
                relevance_state=r["relevance_state"],
                retrieval_status=r["retrieval_status"],
                company_name=r["company_name"],
                source=r["source"],
                issuer_cik=r["issuer_cik"],
                issuer_name=r["issuer_name"],
                attempt_count=r["attempt_count"],
            ) for r in rows]

    def list_stranded_work(self, limit: int = 200) -> list[FilingRecord]:
        """Return filings stuck in ``queued`` or ``in_progress`` after a restart.

        These filings had been picked up by a previous daemon run but never
        reached a terminal retrieval state (retrieved / retrieval_failed /
        retrieved_partial).  Without this query, they are stranded
        indefinitely because the existing retry scanner only looks at
        *failed* statuses.

        Excludes relevance states that should never enter retrieval:
          - ``hdr_pending`` — covered by ``list_hdr_pending``
          - ``hdr_transient_fail`` — covered by ``list_hdr_transient_fail``
          - ``irrelevant``, ``direct_unmatched`` — no retrieval needed
          - ``hdr_failed``, ``unresolved`` — terminal header-gate outcomes
            that must not be replayed into retrieval (extension_plan2 §3)
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT accession_number,archive_cik,form_type,relevance_state,"
                "retrieval_status,company_name,source,issuer_cik,issuer_name,"
                "attempt_count "
                "FROM filings "
                "WHERE retrieval_status IN ('queued','in_progress') "
                "AND relevance_state NOT IN ('irrelevant','direct_unmatched','hdr_pending','hdr_transient_fail','hdr_failed','unresolved') "
                "ORDER BY updated_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [FilingRecord(
                accession_number=r["accession_number"],
                archive_cik=r["archive_cik"],
                form_type=r["form_type"],
                relevance_state=r["relevance_state"],
                retrieval_status=r["retrieval_status"],
                company_name=r["company_name"],
                source=r["source"],
                issuer_cik=r["issuer_cik"],
                issuer_name=r["issuer_name"],
                attempt_count=r["attempt_count"],
            ) for r in rows]

    def list_unprocessed_discoveries(self, limit: int = 200) -> list[FilingRecord]:
        """Return filings persisted by the Atom poller but never classified.

        The Atom poller writes discoveries before advancing the watermark.
        If the daemon crashes after the watermark moves but before the
        consumer runs ``_handle_discovery()``, filings remain in
        ``relevance_state='unknown'`` + ``retrieval_status='discovered'``.
        None of the other replay queries (retry, hdr_pending, stranded)
        cover that combination, so these filings would be stranded.

        This query closes that gap.  Matched filings are re-enqueued as
        fresh discoveries on startup.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT accession_number,archive_cik,form_type,relevance_state,"
                "retrieval_status,company_name,source,issuer_cik,issuer_name,"
                "attempt_count "
                "FROM filings "
                "WHERE relevance_state = 'unknown' "
                "AND retrieval_status = 'discovered' "
                "ORDER BY updated_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            results = [FilingRecord(
                accession_number=r["accession_number"],
                archive_cik=r["archive_cik"],
                form_type=r["form_type"],
                relevance_state=r["relevance_state"],
                retrieval_status=r["retrieval_status"],
                company_name=r["company_name"],
                source=r["source"],
                issuer_cik=r["issuer_cik"],
                issuer_name=r["issuer_name"],
                attempt_count=r["attempt_count"],
            ) for r in rows]
            if results:
                logger.info(
                    "found %d unprocessed discoveries (unknown+discovered)",
                    len(results),
                )
            return results

    def mark_soft_inactive(self, accession: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE filings SET inactive_reason=?, updated_at=? WHERE accession_number=?",
                (reason, utcnow().isoformat(), accession),
            )
            conn.commit()

    def filing_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM filings").fetchone()
            return row[0] if row else 0

    def save_form4_transactions(self, filing: Form4Filing) -> None:
        now = utcnow().isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM form4_transactions WHERE accession_number=?", (filing.accession_number,))
            for txn in filing.transactions:
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
                        filing.accession_number, txn.issuer_cik, txn.issuer_name,
                        txn.issuer_ticker, txn.reporting_owner_cik,
                        txn.reporting_owner_name, int(txn.is_director),
                        int(txn.is_officer), txn.officer_title,
                        int(txn.is_ten_pct_owner), txn.security_title,
                        txn.transaction_date, txn.transaction_code,
                        txn.shares, txn.price_per_share, txn.acquired_disposed,
                        txn.shares_owned_after, txn.direct_indirect,
                        int(txn.is_derivative), now,
                    ),
                )
            conn.commit()

    def save_form4_holdings(self, filing: Form4Filing) -> None:
        now = utcnow().isoformat()
        with self._conn() as conn:
            # Always delete existing holdings for this accession so that a
            # reparse producing fewer (or zero) holdings does not leave stale
            # rows behind.
            conn.execute("DELETE FROM form4_holdings WHERE accession_number=?", (filing.accession_number,))
            for h in filing.holdings:
                conn.execute(
                    """INSERT INTO form4_holdings (
                        accession_number, issuer_cik, issuer_name, issuer_ticker,
                        reporting_owner_cik, reporting_owner_name,
                        is_director, is_officer, officer_title, is_ten_pct_owner,
                        security_title, shares_owned, direct_indirect,
                        nature_of_ownership, is_derivative, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        filing.accession_number, h.issuer_cik, h.issuer_name,
                        h.issuer_ticker, h.reporting_owner_cik,
                        h.reporting_owner_name, int(h.is_director),
                        int(h.is_officer), h.officer_title,
                        int(h.is_ten_pct_owner), h.security_title,
                        h.shares_owned, h.direct_indirect,
                        h.nature_of_ownership, int(h.is_derivative), now,
                    ),
                )
            conn.commit()

    def save_8k_events(self, accession_number: str, events: list[EightKEvent]) -> None:
        now = utcnow().isoformat()
        with self._conn() as conn:
            # Always delete existing 8-K events for this accession so that a
            # reparse producing fewer (or zero) items does not leave stale rows
            # behind.
            conn.execute(
                "DELETE FROM eight_k_events WHERE accession_number=?",
                (accession_number,),
            )
            for ev in events:
                conn.execute(
                    """INSERT OR REPLACE INTO eight_k_events (
                        accession_number, item_number, item_description,
                        filing_date, company_name, cik, created_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (ev.accession_number, ev.item_number, ev.item_description,
                     ev.filing_date, ev.company_name, ev.cik, now),
                )
            conn.commit()

    # --- Archival support ---

    def rewrite_artifact_locations(
        self,
        accession_number: str,
        *,
        raw_txt_path: str | None = None,
        primary_doc_path: str | None = None,
        filing_document_path_updates: list[tuple[str, str]] | None = None,
    ) -> None:
        """Rewrite artifact file-system locations after archival.

        Updates **only** location fields — never touches ``retrieval_status``,
        ``attempt_count``, ``last_attempt_at``, or ``next_retry_at``.  All
        changes happen in a single short SQLite transaction so the DB is
        never left in an inconsistent state where ``filings`` and
        ``filing_documents`` disagree about where files are.

        This method is the **only** safe API for external archival path
        rewrites.  Using ``update_retrieval_status()`` would corrupt retry
        metadata and alter daemon behavior.

        Args:
            accession_number: Filing to update.
            raw_txt_path: New path for the raw .txt artifact (or None to skip).
            primary_doc_path: New path for the primary document (or None to skip).
            filing_document_path_updates: List of (old_local_path, new_local_path)
                pairs for rows in ``filing_documents``.  Matched on
                ``accession_number`` + ``local_path = old_path``.
        """
        now_iso = utcnow().isoformat()
        with self._conn() as conn:
            # Update filings table location columns
            sets: list[str] = ["updated_at=?"]
            vals: list[str | None] = [now_iso]

            if raw_txt_path is not None:
                sets.append("raw_txt_path=?")
                vals.append(raw_txt_path)
            if primary_doc_path is not None:
                sets.append("primary_doc_path=?")
                vals.append(primary_doc_path)

            if len(sets) > 1:  # at least one location field changed
                vals.append(accession_number)
                conn.execute(
                    f"UPDATE filings SET {', '.join(sets)} WHERE accession_number=?",
                    vals,
                )

            # Update filing_documents rows
            if filing_document_path_updates:
                for old_path, new_path in filing_document_path_updates:
                    conn.execute(
                        "UPDATE filing_documents SET local_path=? "
                        "WHERE accession_number=? AND local_path=?",
                        (new_path, accession_number, old_path),
                    )

            conn.commit()
            logger.info(
                "rewrite_artifact_locations: acc=%s raw_txt=%s primary=%s docs=%d",
                accession_number,
                "updated" if raw_txt_path is not None else "unchanged",
                "updated" if primary_doc_path is not None else "unchanged",
                len(filing_document_path_updates) if filing_document_path_updates else 0,
            )

    def list_archival_eligible(
        self,
        *,
        retention_days: int = 30,
        limit: int = 200,
        archive_dir: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return retrieved filings older than *retention_days* with local artifacts.

        Only returns filings where ``retrieval_status = 'retrieved'`` and
        ``updated_at`` is older than the retention threshold.  These are the
        filings whose raw artifacts can be safely moved to archive storage.

        When *archive_dir* is provided, filings whose ``raw_txt_path`` and
        ``primary_doc_path`` **both** already reside under the archive root
        are excluded — they have already been archived and should not be
        re-processed (which would otherwise cause the archiver to delete
        the only remaining copy of the file).

        Each result dict contains: accession_number, archive_cik,
        raw_txt_path, primary_doc_path, txt_sha256, primary_sha256, updated_at.
        """
        cutoff = (utcnow() - timedelta(days=retention_days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT accession_number, archive_cik, "
                "raw_txt_path, primary_doc_path, txt_sha256, primary_sha256, "
                "updated_at "
                "FROM filings "
                "WHERE retrieval_status = 'retrieved' "
                "AND updated_at < ? "
                "AND (raw_txt_path IS NOT NULL OR primary_doc_path IS NOT NULL) "
                "ORDER BY updated_at ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            results = [dict(r) for r in rows]

            # Post-filter: skip filings whose artifact paths are already
            # under the archive root.  This prevents the critical
            # re-archival data-loss bug (extension_plan §3A).
            if archive_dir:
                archive_prefix = str(archive_dir).rstrip("/") + "/"
                filtered: list[dict[str, Any]] = []
                for r in results:
                    raw = r.get("raw_txt_path") or ""
                    pri = r.get("primary_doc_path") or ""
                    raw_archived = raw.startswith(archive_prefix) if raw else True
                    pri_archived = pri.startswith(archive_prefix) if pri else True
                    if raw_archived and pri_archived:
                        # Both paths (where present) are already archived
                        continue
                    filtered.append(r)
                return filtered
            return results

    def list_filing_documents_for_accession(
        self, accession_number: str,
    ) -> list[dict[str, Any]]:
        """Return all filing_documents rows for a given accession."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, accession_number, artifact_type, source_url, "
                "local_path, sha256, content_type "
                "FROM filing_documents WHERE accession_number=?",
                (accession_number,),
            ).fetchall()
            return [dict(r) for r in rows]