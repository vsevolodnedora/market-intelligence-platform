"""External scheduled archiver for the EDGAR ingestor.

Standalone process that archives cold filesystem artifacts and closed
prior-UTC-day event JSONL files.  Designed to run as a cron job or
systemd timer, completely separate from the daemon process.

Safety guarantees:

  1. Only archives filings with ``retrieval_status = 'retrieved'`` older
     than a configurable retention threshold.
  2. Archives **byte-identical** copies (no compression in v1).
  3. Verifies archived bytes against DB content hashes before rewriting paths.
  4. Rewrites DB paths in a single short SQLite transaction via
     ``rewrite_artifact_locations()``.
  5. Deletes originals only after DB commit succeeds.
  6. If deletion fails, leaves both copies and retries cleanup later.

Does NOT touch:
  - filings table rows beyond location fields
  - checkpoints, outbox_events, form4_*, eight_k_events tables
  - retrieval_status, attempt_count, last_attempt_at, next_retry_at
  - current-day JSONL event files
  - filings in any non-retrieved state

Usage:
  python edgar_archiver.py --db-path ./data/edgar.db --archive-dir ./data/archive
  python edgar_archiver.py --db-path ./data/edgar.db --archive-dir /mnt/archive --retention-days 14

Dependencies: Python >= 3.11, PyYAML (via edgar_core).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edgar_core import SQLiteStorage, get_logger, sha256_hex, utcnow

logger = get_logger("edgar_archiver")


# ---------------------------------------------------------------------------
# Archive result tracking
# ---------------------------------------------------------------------------

class ArchivalStats:
    """Accumulator for a single archival run."""

    __slots__ = (
        "filings_scanned", "filings_archived", "filings_skipped",
        "filings_failed", "files_copied", "files_deleted",
        "bytes_copied", "jsonl_files_archived", "errors",
        "start_time",
    )

    def __init__(self) -> None:
        self.filings_scanned = 0
        self.filings_archived = 0
        self.filings_skipped = 0
        self.filings_failed = 0
        self.files_copied = 0
        self.files_deleted = 0
        self.bytes_copied = 0
        self.jsonl_files_archived = 0
        self.errors: list[str] = []
        self.start_time = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def summary(self) -> str:
        return (
            f"archival complete in {self.elapsed:.1f}s: "
            f"scanned={self.filings_scanned} archived={self.filings_archived} "
            f"skipped={self.filings_skipped} failed={self.filings_failed} "
            f"files_copied={self.files_copied} files_deleted={self.files_deleted} "
            f"bytes_copied={self.bytes_copied} jsonl={self.jsonl_files_archived} "
            f"errors={len(self.errors)}"
        )


# ---------------------------------------------------------------------------
# File operations — byte-identical copy with verification
# ---------------------------------------------------------------------------

def _copy_and_verify(
    src: Path, dst: Path, expected_sha256: str | None,
) -> tuple[bool, str]:
    """Copy *src* to *dst* byte-identically and verify the hash.

    Uses streaming reads and a single pass to avoid holding entire files
    in memory twice (extension_plan2 §8).

    Returns (success, sha256_hex_of_copy).  If *expected_sha256* is
    provided and does not match, returns (False, actual_hash).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    _CHUNK = 256 * 1024  # 256 KiB

    # Phase 1: Stream source → temp file, computing hash on the fly
    import tempfile
    src_hasher = hashlib.sha256()
    fd, tmp_path = tempfile.mkstemp(
        dir=str(dst.parent), prefix=".archiving_", suffix=dst.suffix,
    )
    try:
        with open(src, "rb") as fin:
            while True:
                chunk = fin.read(_CHUNK)
                if not chunk:
                    break
                src_hasher.update(chunk)
                os.write(fd, chunk)
        os.fsync(fd)
        os.close(fd)
        fd = -1

        actual_hash = src_hasher.hexdigest()

        if expected_sha256 and actual_hash != expected_sha256:
            logger.error(
                "hash mismatch BEFORE copy: src=%s expected=%s actual=%s",
                src, expected_sha256, actual_hash,
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False, actual_hash

        # Atomic rename
        os.rename(tmp_path, str(dst))
        # Fsync parent directory
        dir_fd = os.open(str(dst.parent), os.O_RDONLY)
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

    # Phase 2: Streaming verify of the written copy
    verify_hasher = hashlib.sha256()
    with open(dst, "rb") as fout:
        while True:
            chunk = fout.read(_CHUNK)
            if not chunk:
                break
            verify_hasher.update(chunk)
    verify_hash = verify_hasher.hexdigest()

    if verify_hash != actual_hash:
        logger.error(
            "hash mismatch AFTER copy: dst=%s expected=%s actual=%s",
            dst, actual_hash, verify_hash,
        )
        return False, verify_hash

    return True, actual_hash


def _safe_delete(path: Path) -> bool:
    """Delete a file, returning True on success or if already gone."""
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("failed to delete %s: %s", path, exc)
        return False


def _streaming_sha256(path: Path) -> str:
    """Compute SHA-256 of a file using streaming reads (avoids full in-memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(256 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Filing archiver
# ---------------------------------------------------------------------------

def _derive_archive_path(archive_dir: Path, original_path: str) -> Path:
    """Map a raw artifact path to an archive location.

    Preserves the CIK/accession directory structure under archive_dir.
    Example:
        original: /data/edgar/.../raw/0001234567/000123456789012345/file.txt
        archived: /archive/0001234567/000123456789012345/file.txt
    """
    parts = Path(original_path).parts
    # Find the CIK directory (10-digit number) in the path
    for i, part in enumerate(parts):
        if len(part) == 10 and part.isdigit():
            # Everything from CIK onwards forms the archive subpath
            return archive_dir / Path(*parts[i:])

    # Fallback: use last 3 path components
    if len(parts) >= 3:
        return archive_dir / Path(*parts[-3:])
    return archive_dir / Path(*parts[-2:]) if len(parts) >= 2 else archive_dir / parts[-1]


def archive_filing(
    storage: SQLiteStorage,
    filing: dict[str, Any],
    archive_dir: Path,
    stats: ArchivalStats,
    *,
    dry_run: bool = False,
) -> bool:
    """Archive a single filing's artifacts.

    Follows the required workflow from extension_plan.md:
      1. Select eligible retrieved filing (already done by caller)
      2. Copy raw artifact files to archive storage
      3. Verify archived bytes against content hashes
      4. Rewrite DB path fields in one short transaction
      5. Delete original local files only after DB commit
      6. If deletion fails, leave both copies

    Returns True if archival succeeded, False otherwise.
    """
    acc = filing["accession_number"]
    raw_txt_path = filing.get("raw_txt_path")
    primary_doc_path = filing.get("primary_doc_path")
    txt_sha256 = filing.get("txt_sha256")
    primary_sha256 = filing.get("primary_sha256")

    if not raw_txt_path and not primary_doc_path:
        logger.debug("skip %s: no local artifacts", acc)
        stats.filings_skipped += 1
        return True

    # Defense-in-depth: reject filings whose artifact paths already live
    # under the archive root.  Without this guard, _derive_archive_path()
    # would produce dst == src, and the subsequent delete step would
    # destroy the only remaining copy.  (Extension plan §3A.)
    archive_prefix = str(archive_dir.resolve()).rstrip("/") + "/"
    for candidate_path in (raw_txt_path, primary_doc_path):
        if candidate_path and str(Path(candidate_path).resolve()).startswith(archive_prefix):
            logger.debug(
                "skip %s: artifact path already under archive_dir (%s)",
                acc, candidate_path,
            )
            stats.filings_skipped += 1
            return True

    # Collect all files to archive: (original_path, expected_hash)
    files_to_archive: list[tuple[Path, str | None]] = []
    if raw_txt_path:
        src = Path(raw_txt_path)
        if src.exists():
            files_to_archive.append((src, txt_sha256))
        else:
            logger.debug("skip %s raw_txt: already gone (%s)", acc, raw_txt_path)
    if primary_doc_path:
        src = Path(primary_doc_path)
        if src.exists():
            files_to_archive.append((src, primary_sha256))
        else:
            logger.debug("skip %s primary_doc: already gone (%s)", acc, primary_doc_path)

    # Also collect filing_documents rows
    doc_rows = storage.list_filing_documents_for_accession(acc)
    doc_path_updates: list[tuple[str, str]] = []

    if not files_to_archive and not doc_rows:
        logger.debug("skip %s: no local files remain", acc)
        stats.filings_skipped += 1
        return True

    if dry_run:
        for src, _hash in files_to_archive:
            dst = _derive_archive_path(archive_dir, str(src))
            logger.info("[DRY RUN] would archive %s → %s", src, dst)
        stats.filings_skipped += 1
        return True

    # Step 2: Copy files to archive
    archived_files: list[tuple[Path, Path]] = []  # (src, dst) pairs
    new_raw_txt_path: str | None = None
    new_primary_doc_path: str | None = None

    for src, expected_hash in files_to_archive:
        dst = _derive_archive_path(archive_dir, str(src))
        if dst.exists():
            # Already archived — just verify
            if expected_hash:
                existing_hash = _streaming_sha256(dst)
                if existing_hash != expected_hash:
                    logger.error(
                        "existing archive file hash mismatch: %s expected=%s actual=%s",
                        dst, expected_hash, existing_hash,
                    )
                    stats.filings_failed += 1
                    stats.errors.append(f"{acc}: hash mismatch on existing archive {dst}")
                    return False
            logger.debug("already archived: %s", dst)
        else:
            # Step 3: Copy and verify
            try:
                ok, _hash = _copy_and_verify(src, dst, expected_hash)
                if not ok:
                    stats.filings_failed += 1
                    stats.errors.append(f"{acc}: verification failed for {src} → {dst}")
                    return False
                stats.files_copied += 1
                stats.bytes_copied += src.stat().st_size
            except Exception as exc:
                logger.error("copy failed %s → %s: %s", src, dst, exc)
                stats.filings_failed += 1
                stats.errors.append(f"{acc}: copy failed {src}: {exc}")
                return False

        archived_files.append((src, dst))

        # Map back to filings table fields
        if raw_txt_path and str(src) == raw_txt_path:
            new_raw_txt_path = str(dst)
        if primary_doc_path and str(src) == primary_doc_path:
            new_primary_doc_path = str(dst)

    # Handle filing_documents rows
    for doc_row in doc_rows:
        local_path = doc_row.get("local_path", "")
        doc_sha = doc_row.get("sha256")
        src = Path(local_path)
        if not src.exists():
            continue
        dst = _derive_archive_path(archive_dir, local_path)

        if dst.exists():
            if doc_sha:
                existing_hash = _streaming_sha256(dst)
                if existing_hash != doc_sha:
                    logger.error(
                        "filing_documents archive hash mismatch: %s", local_path,
                    )
                    stats.filings_failed += 1
                    stats.errors.append(f"{acc}: doc hash mismatch {local_path}")
                    return False
        else:
            try:
                ok, _hash = _copy_and_verify(src, dst, doc_sha)
                if not ok:
                    stats.filings_failed += 1
                    stats.errors.append(f"{acc}: doc verification failed {local_path}")
                    return False
                stats.files_copied += 1
                stats.bytes_copied += src.stat().st_size
            except Exception as exc:
                logger.error("doc copy failed %s: %s", local_path, exc)
                stats.filings_failed += 1
                stats.errors.append(f"{acc}: doc copy failed {local_path}: {exc}")
                return False

        archived_files.append((src, dst))
        doc_path_updates.append((local_path, str(dst)))

    # Step 4: Rewrite DB paths in one short transaction
    try:
        storage.rewrite_artifact_locations(
            acc,
            raw_txt_path=new_raw_txt_path,
            primary_doc_path=new_primary_doc_path,
            filing_document_path_updates=doc_path_updates if doc_path_updates else None,
        )
    except Exception as exc:
        logger.error("DB path rewrite failed for %s: %s", acc, exc)
        stats.filings_failed += 1
        stats.errors.append(f"{acc}: DB rewrite failed: {exc}")
        # Archive files remain as orphans — harmless, will be cleaned up on
        # retry.  DO NOT delete originals.
        return False

    # Step 5: Delete originals only after DB commit succeeded
    for src, _dst in archived_files:
        if src.exists():
            if _safe_delete(src):
                stats.files_deleted += 1
            # Step 6: If deletion fails, leave both copies — harmless

    # Try to clean up empty parent directories
    seen_parents: set[Path] = set()
    for src, _dst in archived_files:
        parent = src.parent
        if parent not in seen_parents:
            seen_parents.add(parent)
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    stats.filings_archived += 1
    logger.info("archived %s (%d files)", acc, len(archived_files))
    return True


# ---------------------------------------------------------------------------
# JSONL event file archiver
# ---------------------------------------------------------------------------

def _atomic_copy(src: Path, dst: Path) -> str:
    """Copy *src* to *dst* using temp-file → fsync → rename (Issue #7).

    Uses streaming reads to avoid holding the entire file in memory.
    Returns the sha256 hex digest of the copied bytes.
    """
    import tempfile
    _CHUNK = 256 * 1024
    dst.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    fd, tmp_path = tempfile.mkstemp(
        dir=str(dst.parent), prefix=".jsonl_archiving_", suffix=dst.suffix,
    )
    try:
        with open(src, "rb") as fin:
            while True:
                chunk = fin.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                os.write(fd, chunk)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(tmp_path, str(dst))
        # Fsync parent directory for metadata durability
        dir_fd = os.open(str(dst.parent), os.O_RDONLY)
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

    return hasher.hexdigest()


def archive_jsonl_events(
    publish_dir: Path,
    archive_dir: Path,
    stats: ArchivalStats,
    *,
    dry_run: bool = False,
) -> None:
    """Archive prior-day UTC event JSONL files.

    Per extension_plan.md: archive only prior-day files.  The publisher
    writes to the current UTC-day file, so touching today's file would
    race with live append/fsync behavior.

    This is entirely file-level — no DB changes.
    """
    if not publish_dir.exists():
        return

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_filename = f"events-{today_str}.jsonl"
    archive_events_dir = archive_dir / "events"

    for f in sorted(publish_dir.glob("events-*.jsonl")):
        if f.name == today_filename:
            continue  # Never touch today's file

        dst = archive_events_dir / f.name
        if dst.exists():
            # Already archived — verify integrity before deleting original.
            # Without this check, a corrupt archive copy would cause the
            # only good data to be destroyed (extension plan §3C).
            if not dry_run:
                src_hash = _streaming_sha256(f)
                dst_hash = _streaming_sha256(dst)
                if src_hash != dst_hash:
                    logger.error(
                        "JSONL archive integrity mismatch for %s: "
                        "src=%s dst=%s — keeping original, re-copying",
                        f.name, src_hash, dst_hash,
                    )
                    # Re-copy from the known-good original using atomic write
                    try:
                        written_hash = _atomic_copy(f, dst)
                        if written_hash == src_hash:
                            _safe_delete(f)
                            stats.jsonl_files_archived += 1
                            logger.info("re-archived JSONL %s (replaced corrupt copy)", f.name)
                        else:
                            logger.error("JSONL re-copy verification failed: %s", f.name)
                            stats.errors.append(f"JSONL {f.name}: re-copy verification failed")
                    except Exception as exc:
                        logger.error("JSONL re-copy failed for %s: %s", f.name, exc)
                        stats.errors.append(f"JSONL {f.name}: re-copy failed: {exc}")
                else:
                    _safe_delete(f)
            continue

        if dry_run:
            logger.info("[DRY RUN] would archive JSONL %s → %s", f, dst)
            continue

        try:
            src_hash = _atomic_copy(f, dst)

            # Verify the archive copy
            verify_hash = _streaming_sha256(dst)
            if verify_hash != src_hash:
                logger.error("JSONL archive hash mismatch: %s", f.name)
                try:
                    dst.unlink()
                except OSError:
                    pass
                continue

            # Delete original after verified copy
            _safe_delete(f)
            stats.jsonl_files_archived += 1
            logger.info("archived JSONL %s", f.name)

        except Exception as exc:
            logger.error("failed to archive JSONL %s: %s", f.name, exc)
            stats.errors.append(f"JSONL {f.name}: {exc}")


# ---------------------------------------------------------------------------
# Cleanup pass — retry deletion of orphaned originals
# ---------------------------------------------------------------------------

def cleanup_orphaned_originals(
    storage: SQLiteStorage,
    archive_dir: Path,
    stats: ArchivalStats,
    *,
    raw_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Find files whose DB paths point to archive_dir but originals still exist.

    This handles Step 6 from the plan: "If deletion fails, leave both
    copies in place and retry cleanup later."

    Scans filings whose ``raw_txt_path`` or ``primary_doc_path`` already
    reside under *archive_dir*.  For each, reconstructs the likely
    original path under *raw_dir* (by replacing the archive prefix with
    the raw prefix).  If the original still exists **and** the archive
    copy matches the DB hash, the orphaned original is deleted.

    When *raw_dir* is ``None`` the function is a no-op (cannot locate
    orphaned originals without knowing the original storage root).
    """
    if raw_dir is None:
        logger.debug("cleanup_orphaned_originals: skipped (no raw_dir provided)")
        return

    archive_prefix = str(archive_dir.resolve()).rstrip("/") + "/"
    raw_prefix = str(raw_dir.resolve()).rstrip("/") + "/"
    cleaned = 0

    # Query filings whose paths are already archived
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT accession_number, raw_txt_path, primary_doc_path, "
            "txt_sha256, primary_sha256 "
            "FROM filings "
            "WHERE retrieval_status = 'retrieved' "
            "AND (raw_txt_path LIKE ? OR primary_doc_path LIKE ?)",
            (str(archive_dir) + "%", str(archive_dir) + "%"),
        ).fetchall()

    for row in rows:
        for path_field, hash_field in [
            ("raw_txt_path", "txt_sha256"),
            ("primary_doc_path", "primary_sha256"),
        ]:
            archived_path_str = row[path_field]
            if not archived_path_str:
                continue
            resolved = str(Path(archived_path_str).resolve())
            if not resolved.startswith(archive_prefix):
                continue

            # Reconstruct candidate original path: replace archive prefix
            # with raw prefix.  The CIK/accession subpath is preserved by
            # _derive_archive_path, so this reversal is exact.
            relative = resolved[len(archive_prefix):]
            candidate_original = Path(raw_prefix + relative)

            if not candidate_original.exists():
                continue  # Nothing to clean up

            # Verify the archive copy is intact before deleting the original
            archived_path = Path(archived_path_str)
            expected_hash = row[hash_field]
            if not archived_path.exists():
                logger.warning(
                    "cleanup: archive copy missing for %s — keeping original %s",
                    row["accession_number"], candidate_original,
                )
                continue

            if expected_hash:
                actual_hash = _streaming_sha256(archived_path)
                if actual_hash != expected_hash:
                    logger.warning(
                        "cleanup: archive hash mismatch for %s (%s) — keeping original",
                        row["accession_number"], archived_path,
                    )
                    continue

            # Archive is verified — safe to delete the orphaned original
            if dry_run:
                logger.info(
                    "[DRY RUN] would delete orphaned original: %s",
                    candidate_original,
                )
            else:
                if _safe_delete(candidate_original):
                    cleaned += 1
                    logger.info(
                        "cleanup: deleted orphaned original %s (acc=%s)",
                        candidate_original, row["accession_number"],
                    )
                    # Try to remove empty parent directories
                    try:
                        parent = candidate_original.parent
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                    except OSError:
                        pass

    if cleaned:
        logger.info("cleanup_orphaned_originals: deleted %d orphaned files", cleaned)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_archival(
    *,
    db_path: Path,
    archive_dir: Path,
    publish_dir: Path | None = None,
    raw_dir: Path | None = None,
    retention_days: int = 30,
    batch_size: int = 100,
    dry_run: bool = False,
) -> ArchivalStats:
    """Run a complete archival cycle.

    This is the entry point for cron/systemd invocations.
    """
    stats = ArchivalStats()
    storage = SQLiteStorage(db_path)

    logger.info(
        "archival starting: db=%s archive=%s retention=%dd batch=%d dry_run=%s",
        db_path, archive_dir, retention_days, batch_size, dry_run,
    )

    # Phase 1: Archive filing artifacts
    # Pass archive_dir so already-archived filings are excluded
    eligible = storage.list_archival_eligible(
        retention_days=retention_days,
        limit=batch_size,
        archive_dir=str(archive_dir),
    )
    stats.filings_scanned = len(eligible)
    logger.info("found %d archival-eligible filings", len(eligible))

    for filing in eligible:
        try:
            archive_filing(storage, filing, archive_dir, stats, dry_run=dry_run)
        except Exception as exc:
            acc = filing.get("accession_number", "?")
            logger.exception("unexpected error archiving %s", acc)
            stats.filings_failed += 1
            stats.errors.append(f"{acc}: unexpected: {exc}")

    # Phase 2: Archive prior-day JSONL event files
    if publish_dir and publish_dir.exists():
        archive_jsonl_events(publish_dir, archive_dir, stats, dry_run=dry_run)

    # Phase 3: Cleanup orphaned originals from prior failed deletes
    cleanup_orphaned_originals(
        storage, archive_dir, stats, raw_dir=raw_dir, dry_run=dry_run,
    )

    logger.info(stats.summary())
    if stats.errors:
        logger.warning("archival errors:\n  %s", "\n  ".join(stats.errors[:20]))

    # Record archival metrics if the metrics layer is available.
    # The archiver runs as a standalone process so metrics are only useful
    # if an external collector scrapes before exit, but recording them
    # ensures the data is available if the archiver is later integrated
    # into the daemon process or if a push-gateway is added.
    try:
        from metrics import METRICS
        METRICS.enable()
        METRICS.record_archival_run(
            filings_archived=stats.filings_archived,
            filings_failed=stats.filings_failed,
            bytes_copied=stats.bytes_copied,
            elapsed_seconds=stats.elapsed,
        )
    except Exception:
        pass  # metrics are optional — never fail the archival run

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="EDGAR filing archiver — archive cold artifacts and event files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--db-path", type=Path, default=Path("../data/edgar/2026-03-31_watchlist/edgar.db"),
        help="Path to the EDGAR SQLite database",
    )
    p.add_argument(
        "--archive-dir", type=Path, default=Path("../data/edgar/2026-03-31_watchlist/archive/"),
        help="Root directory for archived artifacts",
    )
    p.add_argument(
        "--publish-dir", type=Path, default=None,
        help="Directory containing JSONL event files to archive.  "
             "Defaults to <db_path>/../events/ if that directory exists.",
    )
    p.add_argument(
        "--raw-dir", type=Path, default=None,
        help="Original raw artifact directory.  When provided, the cleanup "
             "pass will scan for orphaned originals left behind by prior "
             "failed deletions.  Defaults to <db_path>/../raw/ if it exists.",
    )
    p.add_argument(
        "--retention-days", type=int, default=14,
        help="Only archive filings older than this many days",
    )
    p.add_argument(
        "--batch-size", type=int, default=100,
        help="Maximum number of filings to archive per run",
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Log what would be archived without making changes",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="Enable debug logging",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    if not args.db_path.exists():
        logger.error("database not found: %s", args.db_path)
        sys.exit(1)

    # Auto-derive publish_dir from db_path
    publish_dir = args.publish_dir
    if publish_dir is None:
        candidate = args.db_path.parent / "events"
        if candidate.exists():
            publish_dir = candidate

    # Auto-derive raw_dir from db_path
    raw_dir = args.raw_dir
    if raw_dir is None:
        candidate = args.db_path.parent / "raw"
        if candidate.exists():
            raw_dir = candidate

    stats = run_archival(
        db_path=args.db_path,
        archive_dir=args.archive_dir,
        publish_dir=publish_dir,
        raw_dir=raw_dir,
        retention_days=args.retention_days,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    # Exit with non-zero if there were failures
    if stats.filings_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()