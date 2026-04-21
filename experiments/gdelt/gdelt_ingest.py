#!/usr/bin/env python3
"""
Incremental GDELT ingester.

This script is built for scheduled execution (for example every 15 minutes or once per
 day). It maintains a SQLite file-based database plus an on-disk archive of the original
 downloaded GDELT files. Every run re-reads the authoritative GDELT master file lists,
 discovers files that exist upstream but have not yet been applied locally, downloads
 only those missing files, verifies their checksums, ingests their records, and marks
 them as applied.

Key properties
--------------
* Idempotent: reruns do not duplicate file or record ingestion.
* Backfilling: if the scheduler is delayed or offline, the next run discovers and ingests
  every missing interval still present in the upstream master lists.
* Provenance-preserving: the original ZIP files are archived on disk and every ingested
  record retains the source feed and interval timestamp.
* Dependency-light: Python standard library only.

The default enabled feeds target GDELT's canonical raw v2 news feeds:
* English Event export
* English Mentions
* English GKG
* Translingual Event export
* Translingual Mentions
* Translingual GKG

Usage examples
--------------
# First historical load (may be very large)
python gdelt_ingest.py --db ./gdelt.sqlite --archive-dir ./archive

# Catch up only from a point in time
python gdelt_ingest.py --db ./gdelt.sqlite --archive-dir ./archive --from 20260101000000

# Typical scheduled run every 15 minutes
*/15 * * * * /usr/bin/python3 /opt/gdelt_ingest.py --db /var/lib/gdelt/gdelt.sqlite --archive-dir /var/lib/gdelt/archive

# Daily catch-up run
0 2 * * * /usr/bin/python3 /opt/gdelt_ingest.py --db /var/lib/gdelt/gdelt.sqlite --archive-dir /var/lib/gdelt/archive
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import gzip
import hashlib
import io
import itertools
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("gdelt_ingest")
UTC = dt.timezone.utc
MASTERFILE_LINE_RE = re.compile(
    r"^\s*(?P<size>\d+)\s+(?P<md5>[0-9A-Fa-f]{32})\s+(?P<url>https?://\S+)\s*$"
)
URL_TS_RE = re.compile(r"/(?P<ts>\d{14})")
HTTP_URL_RE = re.compile(r"https?://\S+")

GDELT_MASTER_EN = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
GDELT_MASTER_TRANSLATION = "http://data.gdeltproject.org/gdeltv2/masterfilelist-translation.txt"


@dataclasses.dataclass(frozen=True)
class FeedConfig:
    name: str
    source_list_url: str
    file_pattern: re.Pattern[str]
    compression: str = "zip"
    unique_strategy: str = "line_sha256"
    document_field_hint: Optional[str] = None


FEEDS: Dict[str, FeedConfig] = {
    "events_en": FeedConfig(
        name="events_en",
        source_list_url=GDELT_MASTER_EN,
        file_pattern=re.compile(r"/\d{14}\.export\.CSV\.zip$"),
        unique_strategy="first_field",
        document_field_hint="sourceurl_last_field",
    ),
    "mentions_en": FeedConfig(
        name="mentions_en",
        source_list_url=GDELT_MASTER_EN,
        file_pattern=re.compile(r"/\d{14}\.mentions\.CSV\.zip$"),
        unique_strategy="line_sha256",
        document_field_hint="mentions_identifier_field_6",
    ),
    "gkg_en": FeedConfig(
        name="gkg_en",
        source_list_url=GDELT_MASTER_EN,
        file_pattern=re.compile(r"/\d{14}\.gkg\.csv\.zip$"),
        unique_strategy="first_field",
        document_field_hint="first_url_within_first_10_fields",
    ),
    "events_trans": FeedConfig(
        name="events_trans",
        source_list_url=GDELT_MASTER_TRANSLATION,
        file_pattern=re.compile(r"/\d{14}\.translation\.export\.CSV\.zip$"),
        unique_strategy="first_field",
        document_field_hint="sourceurl_last_field",
    ),
    "mentions_trans": FeedConfig(
        name="mentions_trans",
        source_list_url=GDELT_MASTER_TRANSLATION,
        file_pattern=re.compile(r"/\d{14}\.translation\.mentions\.CSV\.zip$"),
        unique_strategy="line_sha256",
        document_field_hint="mentions_identifier_field_6",
    ),
    "gkg_trans": FeedConfig(
        name="gkg_trans",
        source_list_url=GDELT_MASTER_TRANSLATION,
        file_pattern=re.compile(r"/\d{14}\.translation\.gkg\.csv\.zip$"),
        unique_strategy="first_field",
        document_field_hint="first_url_within_first_10_fields",
    ),
}


@dataclasses.dataclass(frozen=True)
class FileEntry:
    feed_name: str
    source_list_url: str
    source_url: str
    interval_ts: str
    md5_hex: str
    size_bytes: int


class HttpClient:
    """Tiny HTTP helper with retries and timeouts."""

    def __init__(
        self,
        timeout_seconds: int = 60,
        max_attempts: int = 5,
        user_agent: str = "gdelt-ingest/1.0 (+https://www.gdeltproject.org/)",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.user_agent = user_agent

    def fetch_bytes(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "identity",
            },
        )
        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    return resp.read()
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                if attempt >= self.max_attempts:
                    break
                sleep_for = min(30.0, (2 ** (attempt - 1)) + random.random())
                LOGGER.warning(
                    "HTTP fetch failed for %s on attempt %s/%s: %s; retrying in %.1fs",
                    url,
                    attempt,
                    self.max_attempts,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)
        assert last_err is not None
        raise last_err

    def fetch_text(self, url: str, encoding: str = "utf-8") -> str:
        return self.fetch_bytes(url).decode(encoding, errors="replace")


class GDELTIngestor:
    def __init__(
        self,
        db_path: Path,
        archive_dir: Path,
        feeds: Sequence[FeedConfig],
        http: Optional[HttpClient] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.archive_dir = Path(archive_dir)
        self.feeds = list(feeds)
        self.http = http or HttpClient()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed_registry (
                feed_name TEXT PRIMARY KEY,
                source_list_url TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                last_discovered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS file_manifest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_name TEXT NOT NULL,
                source_list_url TEXT NOT NULL,
                source_url TEXT NOT NULL UNIQUE,
                interval_ts TEXT NOT NULL,
                md5_hex TEXT,
                size_bytes INTEGER,
                discovered_at TEXT NOT NULL,
                download_status TEXT NOT NULL DEFAULT 'pending',
                download_attempts INTEGER NOT NULL DEFAULT 0,
                downloaded_at TEXT,
                applied_at TEXT,
                archive_path TEXT,
                payload_sha256 TEXT,
                record_count INTEGER,
                error_text TEXT,
                FOREIGN KEY (feed_name) REFERENCES feed_registry(feed_name)
            );
            CREATE INDEX IF NOT EXISTS idx_file_manifest_feed_interval
                ON file_manifest(feed_name, interval_ts);
            CREATE INDEX IF NOT EXISTS idx_file_manifest_pending
                ON file_manifest(applied_at, feed_name, interval_ts);

            CREATE TABLE IF NOT EXISTS raw_records (
                feed_name TEXT NOT NULL,
                record_key TEXT NOT NULL,
                interval_ts TEXT NOT NULL,
                source_url TEXT NOT NULL,
                document_id TEXT,
                raw_line TEXT NOT NULL,
                inserted_at TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                PRIMARY KEY (feed_name, record_key),
                FOREIGN KEY (file_id) REFERENCES file_manifest(id)
            );
            CREATE INDEX IF NOT EXISTS idx_raw_records_document_id
                ON raw_records(document_id);
            CREATE INDEX IF NOT EXISTS idx_raw_records_interval
                ON raw_records(feed_name, interval_ts);

            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                first_seen_interval_ts TEXT NOT NULL,
                last_seen_interval_ts TEXT NOT NULL,
                first_seen_feed TEXT NOT NULL,
                last_seen_feed TEXT NOT NULL,
                first_source_url TEXT NOT NULL,
                last_source_url TEXT NOT NULL,
                observations INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        now = utc_now_iso()
        for feed in self.feeds:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO feed_registry(feed_name, source_list_url, registered_at)
                VALUES (?, ?, ?)
                """,
                (feed.name, feed.source_list_url, now),
            )
        self.conn.commit()

    def run(
        self,
        from_ts: Optional[str] = None,
        until_ts: Optional[str] = None,
        max_files_per_run: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict:
        until_ts = until_ts or utc_now_compact()
        validate_ts(until_ts)
        if from_ts is not None:
            validate_ts(from_ts)

        discovered = self.discover_all()
        LOGGER.info("Discovered %s candidate file(s) across %s feed(s)", discovered, len(self.feeds))

        pending = self._load_pending_files(from_ts=from_ts, until_ts=until_ts, max_files=max_files_per_run)
        LOGGER.info("%s file(s) pending application in requested window", len(pending))

        applied_files = 0
        inserted_records = 0
        skipped_records = 0
        failed_files = 0

        for file_row in pending:
            try:
                if dry_run:
                    LOGGER.info("[dry-run] would apply %s", file_row["source_url"])
                    continue
                file_inserted, file_skipped = self._apply_file(file_row)
                inserted_records += file_inserted
                skipped_records += file_skipped
                applied_files += 1
            except Exception as exc:  # noqa: BLE001
                failed_files += 1
                LOGGER.exception("Failed to apply %s: %s", file_row["source_url"], exc)
                self._mark_file_failed(file_id=file_row["id"], error_text=str(exc))

        summary = {
            "discovered_files": discovered,
            "pending_files": len(pending),
            "applied_files": applied_files,
            "failed_files": failed_files,
            "inserted_records": inserted_records,
            "skipped_duplicate_records": skipped_records,
            "until_ts": until_ts,
            "from_ts": from_ts,
            "dry_run": dry_run,
        }
        LOGGER.info("Run summary: %s", json.dumps(summary, sort_keys=True))
        return summary

    def discover_all(self) -> int:
        total_new = 0
        for source_list_url, feed_group in group_feeds_by_source(self.feeds).items():
            text = self.http.fetch_text(source_list_url)
            entries = list(parse_masterfile_entries(text))
            LOGGER.info(
                "Fetched master file list %s with %s line(s)",
                source_list_url,
                len(entries),
            )
            discovered_at = utc_now_iso()
            for feed in feed_group:
                matching = 0
                for size_bytes, md5_hex, source_url in entries:
                    if not feed.file_pattern.search(source_url):
                        continue
                    interval_ts = extract_interval_ts_from_url(source_url)
                    file_entry = FileEntry(
                        feed_name=feed.name,
                        source_list_url=source_list_url,
                        source_url=source_url,
                        interval_ts=interval_ts,
                        md5_hex=md5_hex,
                        size_bytes=size_bytes,
                    )
                    if self._register_file(file_entry, discovered_at=discovered_at):
                        total_new += 1
                    matching += 1
                LOGGER.info("Matched %s file(s) for feed %s", matching, feed.name)
            self.conn.execute(
                "UPDATE feed_registry SET last_discovered_at = ? WHERE source_list_url = ?",
                (discovered_at, source_list_url),
            )
            self.conn.commit()
        return total_new

    def _register_file(self, entry: FileEntry, discovered_at: str) -> bool:
        existing = self.conn.execute(
            "SELECT id, md5_hex, size_bytes, feed_name, interval_ts FROM file_manifest WHERE source_url = ?",
            (entry.source_url,),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO file_manifest(
                    feed_name, source_list_url, source_url, interval_ts, md5_hex, size_bytes, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.feed_name,
                    entry.source_list_url,
                    entry.source_url,
                    entry.interval_ts,
                    entry.md5_hex,
                    entry.size_bytes,
                    discovered_at,
                ),
            )
            return True

        self.conn.execute(
            """
            UPDATE file_manifest
            SET md5_hex = COALESCE(md5_hex, ?),
                size_bytes = COALESCE(size_bytes, ?),
                discovered_at = ?
            WHERE id = ?
            """,
            (entry.md5_hex, entry.size_bytes, discovered_at, existing["id"]),
        )
        return False

    def _load_pending_files(
        self,
        *,
        from_ts: Optional[str],
        until_ts: str,
        max_files: Optional[int],
    ) -> List[sqlite3.Row]:
        params: List[object] = [until_ts]
        sql = """
            SELECT *
            FROM file_manifest
            WHERE applied_at IS NULL
              AND interval_ts <= ?
        """
        if from_ts is not None:
            sql += " AND interval_ts >= ?"
            params.append(from_ts)
        sql += " ORDER BY interval_ts ASC, id ASC"
        if max_files is not None:
            sql += " LIMIT ?"
            params.append(max_files)
        return list(self.conn.execute(sql, params).fetchall())

    def _apply_file(self, file_row: sqlite3.Row) -> Tuple[int, int]:
        file_id = int(file_row["id"])
        feed = FEEDS[file_row["feed_name"]]
        source_url = str(file_row["source_url"])
        interval_ts = str(file_row["interval_ts"])

        self.conn.execute(
            "UPDATE file_manifest SET download_status = 'in_progress', download_attempts = download_attempts + 1 WHERE id = ?",
            (file_id,),
        )
        self.conn.commit()

        archive_path, payload_bytes = self._obtain_archive(file_row)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        expected_md5 = file_row["md5_hex"]
        if expected_md5:
            actual_md5 = hashlib.md5(payload_bytes).hexdigest()
            if actual_md5.lower() != str(expected_md5).lower():
                raise ValueError(
                    f"Checksum mismatch for {source_url}: expected md5 {expected_md5}, got {actual_md5}"
                )
        expected_size = file_row["size_bytes"]
        if expected_size is not None and int(expected_size) != len(payload_bytes):
            raise ValueError(
                f"Size mismatch for {source_url}: expected {expected_size}, got {len(payload_bytes)}"
            )

        inserted = 0
        skipped = 0
        with self.conn:
            self.conn.execute("SAVEPOINT apply_file")
            try:
                for raw_line in iter_payload_lines(payload_bytes, compression=feed.compression):
                    raw_line = raw_line.rstrip("\r\n")
                    if not raw_line:
                        continue
                    record_key = derive_record_key(feed, raw_line)
                    document_id = derive_document_id(feed, raw_line)
                    cur = self.conn.execute(
                        """
                        INSERT OR IGNORE INTO raw_records(
                            feed_name, record_key, interval_ts, source_url, document_id, raw_line, inserted_at, file_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            feed.name,
                            record_key,
                            interval_ts,
                            source_url,
                            document_id,
                            raw_line,
                            utc_now_iso(),
                            file_id,
                        ),
                    )
                    if cur.rowcount == 1:
                        inserted += 1
                        if document_id:
                            self._upsert_document(
                                document_id=document_id,
                                interval_ts=interval_ts,
                                feed_name=feed.name,
                                source_url=source_url,
                            )
                    else:
                        skipped += 1
                self.conn.execute(
                    """
                    UPDATE file_manifest
                    SET download_status = 'applied',
                        downloaded_at = COALESCE(downloaded_at, ?),
                        applied_at = ?,
                        archive_path = ?,
                        payload_sha256 = ?,
                        record_count = ?,
                        error_text = NULL
                    WHERE id = ?
                    """,
                    (
                        utc_now_iso(),
                        utc_now_iso(),
                        str(archive_path),
                        payload_sha256,
                        inserted,
                        file_id,
                    ),
                )
                self.conn.execute("RELEASE SAVEPOINT apply_file")
            except Exception:
                self.conn.execute("ROLLBACK TO SAVEPOINT apply_file")
                self.conn.execute("RELEASE SAVEPOINT apply_file")
                raise
        LOGGER.info(
            "Applied %s (%s): inserted=%s skipped_duplicates=%s",
            source_url,
            feed.name,
            inserted,
            skipped,
        )
        return inserted, skipped

    def _upsert_document(self, document_id: str, interval_ts: str, feed_name: str, source_url: str) -> None:
        existing = self.conn.execute(
            "SELECT document_id FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        now = utc_now_iso()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO documents(
                    document_id,
                    first_seen_interval_ts,
                    last_seen_interval_ts,
                    first_seen_feed,
                    last_seen_feed,
                    first_source_url,
                    last_source_url,
                    observations,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    document_id,
                    interval_ts,
                    interval_ts,
                    feed_name,
                    feed_name,
                    source_url,
                    source_url,
                    now,
                ),
            )
            return
        self.conn.execute(
            """
            UPDATE documents
            SET last_seen_interval_ts = CASE WHEN last_seen_interval_ts < ? THEN ? ELSE last_seen_interval_ts END,
                last_seen_feed = ?,
                last_source_url = ?,
                observations = observations + 1,
                updated_at = ?
            WHERE document_id = ?
            """,
            (interval_ts, interval_ts, feed_name, source_url, now, document_id),
        )

    def _obtain_archive(self, file_row: sqlite3.Row) -> Tuple[Path, bytes]:
        source_url = str(file_row["source_url"])
        archive_path = build_archive_path(self.archive_dir, file_row["feed_name"], file_row["interval_ts"], source_url)
        if archive_path.exists():
            payload = archive_path.read_bytes()
            return archive_path, payload

        payload = self.http.fetch_bytes(source_url)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(archive_path, payload)
        return archive_path, payload

    def _mark_file_failed(self, *, file_id: int, error_text: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE file_manifest SET download_status = 'failed', error_text = ? WHERE id = ?",
                (error_text[:4000], file_id),
            )


def group_feeds_by_source(feeds: Sequence[FeedConfig]) -> Dict[str, List[FeedConfig]]:
    grouped: Dict[str, List[FeedConfig]] = {}
    for feed in feeds:
        grouped.setdefault(feed.source_list_url, []).append(feed)
    return grouped


def parse_masterfile_entries(text: str) -> Iterator[Tuple[int, str, str]]:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = MASTERFILE_LINE_RE.match(line)
        if match:
            yield int(match.group("size")), match.group("md5"), match.group("url")
            continue
        # Defensive fallback: recover the URL from unusual whitespace, then infer the other columns.
        url_match = HTTP_URL_RE.search(line)
        if not url_match:
            continue
        url = url_match.group(0)
        left = line[: url_match.start()].strip()
        parts = left.split()
        if len(parts) >= 2 and re.fullmatch(r"\d+", parts[0]) and re.fullmatch(r"[0-9A-Fa-f]{32}", parts[1]):
            yield int(parts[0]), parts[1], url


def extract_interval_ts_from_url(url: str) -> str:
    match = URL_TS_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract 14-digit interval timestamp from URL: {url}")
    return match.group("ts")


def derive_record_key(feed: FeedConfig, raw_line: str) -> str:
    if feed.unique_strategy == "first_field":
        first_field = raw_line.split("\t", 1)[0].strip()
        if first_field:
            return first_field
    return hashlib.sha256(raw_line.encode("utf-8", errors="replace")).hexdigest()


def derive_document_id(feed: FeedConfig, raw_line: str) -> Optional[str]:
    fields = raw_line.split("\t")
    hint = feed.document_field_hint
    if hint == "sourceurl_last_field":
        candidate = fields[-1].strip() if fields else ""
        return candidate if candidate.startswith(("http://", "https://")) else None
    if hint == "mentions_identifier_field_6":
        if len(fields) > 5:
            candidate = fields[5].strip()
            return candidate if candidate.startswith(("http://", "https://")) else candidate or None
        return None
    if hint == "first_url_within_first_10_fields":
        for field in fields[:10]:
            candidate = field.strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
        return None
    return None


def iter_payload_lines(payload_bytes: bytes, *, compression: str) -> Iterator[str]:
    if compression == "zip":
        with zipfile.ZipFile(io.BytesIO(payload_bytes)) as zf:
            members = [name for name in zf.namelist() if not name.endswith("/")]
            if not members:
                return
            for member in members:
                with zf.open(member, "r") as handle:
                    wrapper = io.TextIOWrapper(handle, encoding="utf-8", errors="replace", newline="")
                    try:
                        for line in wrapper:
                            yield line
                    finally:
                        wrapper.detach()
        return
    if compression == "gz":
        with gzip.GzipFile(fileobj=io.BytesIO(payload_bytes), mode="rb") as gz:
            wrapper = io.TextIOWrapper(gz, encoding="utf-8", errors="replace", newline="")
            try:
                for line in wrapper:
                    yield line
            finally:
                wrapper.detach()
        return
    raise ValueError(f"Unsupported compression: {compression}")


def build_archive_path(root: Path, feed_name: str, interval_ts: str, source_url: str) -> Path:
    dt_obj = parse_compact_ts(interval_ts)
    filename = source_url.rsplit("/", 1)[-1]
    return root / feed_name / f"{dt_obj.year:04d}" / f"{dt_obj.month:02d}" / f"{dt_obj.day:02d}" / filename


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)


def parse_compact_ts(value: str) -> dt.datetime:
    validate_ts(value)
    return dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def validate_ts(value: str) -> None:
    if not re.fullmatch(r"\d{14}", value):
        raise ValueError(f"Timestamp must be YYYYMMDDHHMMSS, got: {value}")
    dt.datetime.strptime(value, "%Y%m%d%H%M%S")


def utc_now_iso() -> str:
    return dt.datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def utc_now_compact() -> str:
    return dt.datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally ingest raw GDELT files into SQLite.")
    parser.add_argument("--db", required=True, help="Path to the SQLite database file.")
    parser.add_argument("--archive-dir", required=True, help="Directory where original downloaded archives are stored.")
    parser.add_argument(
        "--feeds",
        default=",".join(FEEDS.keys()),
        help=(
            "Comma-separated feed names. Available: "
            + ", ".join(sorted(FEEDS))
            + ". Default: all canonical v2 feeds."
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_ts",
        help="Optional lower bound interval in YYYYMMDDHHMMSS. If omitted, all discovered but unapplied files are eligible.",
    )
    parser.add_argument(
        "--until",
        dest="until_ts",
        help="Optional upper bound interval in YYYYMMDDHHMMSS. Default: current UTC time.",
    )
    parser.add_argument(
        "--max-files-per-run",
        type=int,
        default=None,
        help="Optional safety cap on how many files to apply this run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover pending files without downloading or applying them.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="HTTP timeout per request. Default: 60 seconds.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="Maximum HTTP retry attempts per request. Default: 5.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    feed_names = [name.strip() for name in args.feeds.split(",") if name.strip()]
    unknown = [name for name in feed_names if name not in FEEDS]
    if unknown:
        raise SystemExit(f"Unknown feed(s): {', '.join(sorted(unknown))}")

    http = HttpClient(timeout_seconds=args.timeout_seconds, max_attempts=args.max_attempts)
    ingestor = GDELTIngestor(
        db_path=Path(args.db),
        archive_dir=Path(args.archive_dir),
        feeds=[FEEDS[name] for name in feed_names],
        http=http,
    )
    try:
        summary = ingestor.run(
            from_ts=args.from_ts,
            until_ts=args.until_ts,
            max_files_per_run=args.max_files_per_run,
            dry_run=args.dry_run,
        )
    finally:
        ingestor.close()
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
