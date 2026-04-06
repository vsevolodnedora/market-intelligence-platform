"""Unified EDGAR ingestor daemon: live streaming, backfill, and repair.

Single-process daemon that runs all logical stages concurrently:
  1. Live discovery from the Atom feed (highest priority)
  2. Header-gate resolution for ambiguous forms
  3. Full artifact retrieval
  4. Low-priority issuer audit + resumable backfill
  5. Daily reconcile + retry failed

One event loop, one rate limiter, WAL-serialized SQLite.  All work is priority-
scheduled through a shared async queue so that live latency is never blocked
by backfill or repair.  Blocking filesystem and DB writes are offloaded to
a thread pool to prevent event-loop stalls.

Usage:
  python edgar_daemon.py --user-agent "Firm ops@firm.com" [options]

Dependencies: Python >= 3.11, PyYAML.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from edgar_core import (
    DEFAULT_AMBIGUOUS_FORMS,
    DEFAULT_DIRECT_FORMS,
    EightKEvent,
    FeedWatermark,
    FilingArtifact,
    FilingDiscovery,
    FilingParty,
    FilingPriority,
    FilingRecord,
    Form4Filing,
    HeaderResolver,
    MAX_FILING_RETRY_ATTEMPTS,
    RelevanceState,
    RetrievalStatus,
    SECClient,
    SECHTTPError,
    SQLiteStorage,
    Settings,
    SubmissionHeader,
    WatchlistCompany,
    WatchlistIndex,
    _TERMINAL_RELEVANCE_STATES,
    _TERMINAL_RETRIEVAL_STATUSES,
    _RETRYABLE_RETRIEVAL_STATUSES,
    accession_nodashes,
    choose_primary_document,
    choose_primary_document_from_header,
    derive_archive_base,
    derive_complete_txt_url,
    derive_hdr_sgml_url,
    derive_index_url,
    extract_primary_document_bytes,
    extract_submissions_rollover_urls,
    filter_by_forms,
    filename_from_url,
    get_logger,
    guess_content_type_from_filename,
    is_ambiguous_form,
    is_direct_form,
    is_textual_primary_filename,
    load_watchlist_yaml,
    normalize_cik,
    normalized_header_metadata,
    parse_company_idx,
    parse_latest_filings_atom,
    parse_submission_text,
    parse_submissions_json,
    parse_submissions_rollover_json,
    safe_filename,
    sec_business_date,
    sha256_hex,
    try_parse_date,
    try_parse_datetime,
    utcnow,
    ACTIVIST_FORMS,
    LATEST_FILINGS_ATOM_URL,
    _OWNERSHIP_FORM_RE,
)

from event_outbox import (
    ArtifactWriter,
    EventEnvelope,
    EventSubjects,
    FilingCommitService,
    OutboxStore,
    build_feed_gap_event,
    build_filing_failed_event,
    make_jsonl_publisher,
    make_jsonl_batch_publisher,
)

from form_registry import FormRegistry
from form_form4 import Form4Handler
from form_eight_k import EightKHandler
from retrieval import FilingRetriever

from metrics import METRICS
from metrics_http import start_metrics_server, stop_metrics_server


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Async storage proxy — offloads ALL SQLite calls to a thread pool
# ---------------------------------------------------------------------------

class AsyncStorageProxy:
    """Thin async wrapper around ``SQLiteStorage``.

    Every method delegates to the underlying synchronous storage via
    ``asyncio.to_thread()``, ensuring that no SQLite call blocks the
    event loop.  This addresses extension_plan Issue #1.

    The synchronous ``storage`` object is still available as ``.sync``
    for the few paths that already run inside ``asyncio.to_thread()``
    (e.g. ``FilingCommitService`` callbacks).
    """

    __slots__ = ("sync",)

    def __init__(self, storage: SQLiteStorage) -> None:
        self.sync = storage

    # --- Discovery / relevance ---

    async def upsert_discovery(self, d: Any) -> bool:
        return await asyncio.to_thread(self.sync.upsert_discovery, d)

    async def update_relevance(self, accession: str, state: Any, **kw: Any) -> None:
        return await asyncio.to_thread(self.sync.update_relevance, accession, state, **kw)

    async def set_hdr_transient_fail(self, accession: str, **kw: Any) -> None:
        return await asyncio.to_thread(self.sync.set_hdr_transient_fail, accession, **kw)

    # --- Retrieval lifecycle ---

    async def set_retrieval_queued(self, accession: str, *, force: bool = False) -> bool:
        return await asyncio.to_thread(self.sync.set_retrieval_queued, accession, force=force)

    async def set_retrieval_in_progress(self, accession: str) -> None:
        return await asyncio.to_thread(self.sync.set_retrieval_in_progress, accession)

    # --- Queries ---

    async def is_filing_terminal(self, accession: str) -> bool:
        return await asyncio.to_thread(self.sync.is_filing_terminal, accession)

    async def is_retry_cooling_down(self, accession: str) -> bool:
        return await asyncio.to_thread(self.sync.is_retry_cooling_down, accession)

    async def get_filing(self, accession: str) -> Any:
        return await asyncio.to_thread(self.sync.get_filing, accession)

    async def accession_exists(self, accession: str) -> bool:
        return await asyncio.to_thread(self.sync.accession_exists, accession)

    async def accessions_exist_batch(self, accessions: list[str]) -> set[str]:
        return await asyncio.to_thread(self.sync.accessions_exist_batch, accessions)

    # --- Checkpoints ---

    async def get_checkpoint(self, key: str) -> str | None:
        return await asyncio.to_thread(self.sync.get_checkpoint, key)

    async def set_checkpoint(self, key: str, value: str) -> None:
        return await asyncio.to_thread(self.sync.set_checkpoint, key, value)

    # --- List queries ---

    async def list_retry_candidates(self, limit: int = 50) -> list[Any]:
        return await asyncio.to_thread(self.sync.list_retry_candidates, limit=limit)

    async def list_hdr_pending(self, limit: int = 100) -> list[Any]:
        return await asyncio.to_thread(self.sync.list_hdr_pending, limit=limit)

    async def list_hdr_transient_fail(self, limit: int = 100) -> list[Any]:
        return await asyncio.to_thread(self.sync.list_hdr_transient_fail, limit=limit)

    async def list_stranded_work(self, limit: int = 200) -> list[Any]:
        return await asyncio.to_thread(self.sync.list_stranded_work, limit=limit)

    async def list_unprocessed_discoveries(self, limit: int = 200) -> list[Any]:
        return await asyncio.to_thread(self.sync.list_unprocessed_discoveries, limit=limit)

    # --- Filing parties ---

    async def save_filing_parties(self, accession: str, parties: list[Any]) -> None:
        return await asyncio.to_thread(self.sync.save_filing_parties, accession, parties)


# ---------------------------------------------------------------------------
# Work item — the unit of schedulable daemon work
# ---------------------------------------------------------------------------

@dataclass(slots=True, order=True)
class WorkItem:
    priority_rank: int = field(compare=True)
    created_at: float = field(compare=True)
    kind: str = field(compare=False)
    accession_number: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)

_PRIORITY_RANKS: dict[str, int] = {
    FilingPriority.LIVE: 0,
    FilingPriority.RETRY: 1,
    FilingPriority.HEADER_GATE: 2,
    FilingPriority.RETRIEVAL: 3,
    FilingPriority.AUDIT: 4,
    FilingPriority.BACKFILL: 5,
    FilingPriority.REPAIR: 6,
}

# so that backfill/audit never block latency-critical work.
_LIVE_PRIORITIES: frozenset[str] = frozenset({
    FilingPriority.LIVE,
    FilingPriority.RETRY,
    FilingPriority.HEADER_GATE,
    FilingPriority.RETRIEVAL,
})

_LIVE_QUEUE_MAXSIZE = 2000
_HIST_QUEUE_MAXSIZE = 5000


def make_work(kind: str, accession: str, priority: str, **kw: Any) -> WorkItem:
    return WorkItem(
        priority_rank=_PRIORITY_RANKS.get(priority, 99),
        created_at=_time.monotonic(),
        kind=kind,
        accession_number=accession,
        payload=kw,
    )


def is_live_priority(priority: str) -> bool:
    """Return True if *priority* should be routed to the live lane."""
    return priority in _LIVE_PRIORITIES


# FilingRetriever is now imported from edgar.retrieval


# ---------------------------------------------------------------------------
# Daemon — the unified runtime
# ---------------------------------------------------------------------------

class IngestionDaemon:
    """Always-on daemon coordinating all ingestion work."""

    _ATOM_WM_KEY = "atom_watermark"
    _RECONCILE_KEY = "daily_reconcile_date"
    _BOOTSTRAP_KEY = "bootstrap_complete"
    _WEEKLY_REPAIR_KEY = "weekly_repair_last_run"
    _DAILY_INDEX_BASE = "https://www.sec.gov/Archives/edgar/daily-index"
    _GRACE_DAYS_FOR_404 = 2

    def __init__(
        self,
        settings: Settings,
        storage: SQLiteStorage,
        client: SECClient,
        watchlist: WatchlistIndex,
        *,
        hist_client: SECClient | None = None,
        publish_callback: Callable[[EventEnvelope], Awaitable[None]] | None = None,
        batch_publish_callback: Callable[[list[EventEnvelope]], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        # Wrap storage in async proxy so ALL SQLite calls from daemon methods
        # are offloaded to a thread pool (extension_plan Issue #1).
        # The raw synchronous storage is passed to outbox / commit_service /
        # retriever which already handle their own threading.
        self.storage = AsyncStorageProxy(storage)
        self.client = client
        # Historical lane gets its own rate-limited client so backfill/audit/
        # reconcile/repair can never consume the live lane's SEC request budget.
        self.hist_client = hist_client or client
        self.watchlist = watchlist
        # --- Outbox and commit service (use raw sync storage) ---
        self.outbox = OutboxStore(
            storage,
            publish_retry_base_seconds=settings.retry_base_seconds,
        )
        self.commit_service = FilingCommitService(storage, self.outbox)
        # --- Form handler registry (Phase 3) ---
        self.form_registry = FormRegistry()
        self.form_registry.register(Form4Handler())
        self.form_registry.register(EightKHandler())
        self.retriever = FilingRetriever(
            client, storage, settings.raw_dir,
            retry_base_seconds=settings.retry_base_seconds,
            commit_service=self.commit_service,
            out_form4_transactions_cap=self.settings.out_form4_transactions_cap,
            out_form4_owners_cap=self.settings.out_form4_owners_cap,
            form_registry=self.form_registry,
        )
        self.header_resolver = HeaderResolver(watchlist)

        self._live_queue: asyncio.PriorityQueue[WorkItem] = asyncio.PriorityQueue(
            maxsize=_LIVE_QUEUE_MAXSIZE,
        )
        self._hist_queue: asyncio.PriorityQueue[WorkItem] = asyncio.PriorityQueue(
            maxsize=_HIST_QUEUE_MAXSIZE,
        )
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

        self._publish_callback = publish_callback
        self._batch_publish_callback = batch_publish_callback
        # Wake-up signal so the outbox publisher reacts immediately to new
        # events instead of waiting for the next poll cycle
        self._outbox_wake: asyncio.Event = asyncio.Event()
        # Synchronization flag so audit waits for bootstrap
        self._bootstrap_done = asyncio.Event()

    # --- Lane-aware enqueue ---

    async def _enqueue(self, item: WorkItem) -> bool:
        """Route *item* to the correct queue based on its priority rank.

        Returns True if the item was enqueued, False if it was deferred.

        Both lanes use bounded enqueue with a brief timeout so that neither
        the Atom poller nor any other producer can be stalled indefinitely
        by downstream congestion.  The filing is already persisted in SQLite,
        so replay/retry/reconcile will rediscover it.
        """
        if item.priority_rank <= _PRIORITY_RANKS[FilingPriority.RETRIEVAL]:
            try:
                await asyncio.wait_for(self._live_queue.put(item), timeout=5.0)
                return True
            except (asyncio.QueueFull, asyncio.TimeoutError):
                logger.warning(
                    "live queue full (%d) — deferring %s/%s "
                    "(already persisted in SQLite, will be recovered by "
                    "replay/retry/reconcile)",
                    self._live_queue.qsize(), item.kind, item.accession_number,
                )
                return False
        else:
            try:
                # Block briefly (up to 5s) to let the consumer drain the queue
                # rather than dropping immediately on transient pressure.
                await asyncio.wait_for(self._hist_queue.put(item), timeout=5.0)
                return True
            except (asyncio.QueueFull, asyncio.TimeoutError):
                logger.warning(
                    "historical queue full (%d) — deferring %s/%s "
                    "(already persisted in SQLite, will be recovered by "
                    "replay/reconcile/audit)",
                    self._hist_queue.qsize(), item.kind, item.accession_number,
                )
                return False

    # --- Lifecycle ---

    async def run(self) -> None:
        logger.info(
            "starting daemon: watchlist=%d rps=%.1f atom_poll=%ds audit=%ds "
            "live_q=%d hist_q=%d live_workers=%d",
            len(self.watchlist), self.settings.max_rps,
            self.settings.latest_poll_seconds, self.settings.watchlist_audit_seconds,
            _LIVE_QUEUE_MAXSIZE, _HIST_QUEUE_MAXSIZE, max(1, self.settings.live_workers),
        )

        # --- Metrics initialisation ---
        metrics_server = None
        if self.settings.metrics_enabled:
            METRICS.enable()
            METRICS.set_gauge("edgar_up", 1.0)
            METRICS.set_gauge("edgar_live_workers_configured",
                              float(max(1, self.settings.live_workers)))
            METRICS.set_gauge("edgar_bootstrap_complete",
                              0.0 if not await self._is_bootstrap_complete() else 1.0)
            try:
                metrics_server = await start_metrics_server(
                    METRICS,
                    host=self.settings.metrics_host,
                    port=self.settings.metrics_port,
                )
            except Exception:
                logger.warning("failed to start metrics HTTP server — continuing without metrics",
                               exc_info=True)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_shutdown, sig)

        bootstrap_needed = not await self._is_bootstrap_complete()
        if not bootstrap_needed:
            # No bootstrap needed — audit can start immediately.
            self._bootstrap_done.set()

        tasks = []
        # Multiple live-lane workers for concurrent processing — the per-
        # accession in-flight guard and global SEC token bucket prevent both
        # duplicate work and rate-limit violations.
        n_live = max(1, self.settings.live_workers)
        for i in range(n_live):
            tasks.append(asyncio.create_task(
                self._consumer_loop(self._live_queue),
                name=f"live_consumer_{i}",
            ))
        tasks.extend([
            asyncio.create_task(self._consumer_loop(self._hist_queue), name="hist_consumer"),
            asyncio.create_task(self._atom_poller(), name="atom_poller"),
            asyncio.create_task(self._retry_scanner(), name="retry_scanner"),
            asyncio.create_task(self._stranded_scanner(), name="stranded_scanner"),
            asyncio.create_task(self._daily_reconciler(), name="reconciler"),
            asyncio.create_task(self._outbox_publisher(), name="outbox_publisher"),
            asyncio.create_task(self._weekly_repair(), name="weekly_repair"),
        ])
        if METRICS.is_enabled:
            tasks.append(asyncio.create_task(
                self._metrics_gauge_updater(), name="metrics_gauge_updater",
            ))
        if len(self.watchlist) > 0:
            tasks.append(asyncio.create_task(self._issuer_audit(), name="audit"))

        if bootstrap_needed:
            tasks.append(asyncio.create_task(self._bootstrap(), name="bootstrap"))

        await self._replay_stale_work()

        # Wait until shutdown is signalled, then cancel all tasks.
        await self._shutdown.wait()
        logger.info("shutdown signalled — cancelling %d tasks", len(tasks))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self._requeue_inflight()

        # --- Metrics teardown ---
        METRICS.set_gauge("edgar_up", 0.0)
        await stop_metrics_server(metrics_server)

        logger.info("graceful shutdown complete")

    def _signal_shutdown(self, sig: signal.Signals) -> None:
        """Called by the event loop's signal handler."""
        logger.info("received signal %s — initiating graceful shutdown", sig.name)
        self._shutdown.set()

    async def _requeue_inflight(self) -> None:
        """Reset any in-flight filings back to ``queued`` so they survive restart.

        After the consumer loop stops, filings that were actively being
        processed (``in_progress``) would otherwise be stranded.  Resetting
        them to ``queued`` ensures ``_replay_stale_work`` picks them up on
        the next start via ``list_stranded_work``.
        """
        async with self._inflight_lock:
            accessions = list(self._inflight)
        if not accessions:
            return
        for acc in accessions:
            try:
                await self.storage.set_retrieval_queued(acc, force=True)
            except Exception:
                logger.warning("failed to requeue in-flight filing %s", acc, exc_info=True)
        logger.info("requeued %d in-flight filings for restart recovery", len(accessions))

    # --- Metrics gauge updater ---

    async def _metrics_gauge_updater(self) -> None:
        """Periodically refresh queue-depth and inflight gauges.

        Runs every 2 seconds — cheap reads that never touch SQLite.
        """
        while not self._shutdown.is_set():
            try:
                METRICS.set_gauge("edgar_live_queue_depth",
                                  float(self._live_queue.qsize()))
                METRICS.set_gauge("edgar_hist_queue_depth",
                                  float(self._hist_queue.qsize()))
                async with self._inflight_lock:
                    inflight = len(self._inflight)
                METRICS.set_gauge("edgar_inflight_accessions", float(inflight))
            except Exception:
                pass  # metrics must never affect daemon
            await asyncio.sleep(2.0)

    # --- Consumer ---

    async def _consumer_loop(self, queue: asyncio.PriorityQueue[WorkItem]) -> None:
        while not self._shutdown.is_set():
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process_item(item)
            except Exception:
                logger.exception("error processing work item %s/%s", item.kind, item.accession_number)
            finally:
                queue.task_done()

    async def _process_item(self, item: WorkItem) -> None:
        acc = item.accession_number
        async with self._inflight_lock:
            if acc in self._inflight:
                logger.debug(
                    "work skip (in-flight): kind=%s acc=%s live_q=%d hist_q=%d",
                    item.kind, acc, self._live_queue.qsize(), self._hist_queue.qsize(),
                )
                return
            self._inflight.add(acc)
        try:
            logger.debug(
                "work start: kind=%s acc=%s priority=%d live_q=%d hist_q=%d inflight=%d",
                item.kind, acc, item.priority_rank,
                self._live_queue.qsize(), self._hist_queue.qsize(),
                len(self._inflight),
            )
            if item.kind == "discovery":
                await self._handle_discovery(item)
            elif item.kind == "header_gate":
                await self._handle_header_gate(item)
            elif item.kind in ("retrieval", "retry"):
                await self._handle_retrieval(item)
            else:
                logger.warning("unknown work item kind: %s", item.kind)
        finally:
            async with self._inflight_lock:
                self._inflight.discard(acc)

    # --- Discovery classification ---

    async def _handle_discovery(self, item: WorkItem) -> None:
        discovery = item.payload.get("discovery")
        if not isinstance(discovery, FilingDiscovery):
            return

        acc = discovery.accession_number

        if await self.storage.is_filing_terminal(acc):
            logger.debug(
                "discovery skip (terminal): acc=%s form=%s source=%s",
                acc, discovery.form_type, discovery.source,
            )
            return

        is_new = await self.storage.upsert_discovery(discovery)
        if is_new:
            METRICS.inc("edgar_filings_discovered_total")
        form = discovery.form_type.upper().strip()

        if is_direct_form(form, self.settings.direct_forms):
            if self.watchlist.contains_cik(discovery.archive_cik):
                await self.storage.update_relevance(acc, RelevanceState.DIRECT_MATCH)
                queued = await self.storage.set_retrieval_queued(acc)
                if queued:
                    await self._enqueue(make_work(
                        "retrieval", acc,
                        FilingPriority.RETRIEVAL, discovery=discovery,
                    ))
                    logger.info(
                        "discovery→retrieval: acc=%s form=%s cik=%s new=%s",
                        acc, form, discovery.archive_cik, is_new,
                    )
                else:
                    logger.debug(
                        "discovery→cooling_down: acc=%s form=%s (backoff not expired)",
                        acc, form,
                    )
            else:
                await self.storage.update_relevance(acc, RelevanceState.DIRECT_UNMATCHED)
                logger.debug(
                    "discovery→unmatched: acc=%s form=%s cik=%s",
                    acc, form, discovery.archive_cik,
                )

        elif is_ambiguous_form(form, self.settings.ambiguous_forms):
            await self.storage.update_relevance(acc, RelevanceState.HDR_PENDING)
            queued = await self.storage.set_retrieval_queued(acc)
            if queued:
                await self._enqueue(make_work(
                    "header_gate", acc,
                    FilingPriority.HEADER_GATE, discovery=discovery,
                ))
                logger.info(
                    "discovery→header_gate: acc=%s form=%s cik=%s new=%s",
                    acc, form, discovery.archive_cik, is_new,
                )
            else:
                logger.debug(
                    "discovery→header_gate cooling_down: acc=%s form=%s (backoff not expired)",
                    acc, form,
                )

    async def _handle_header_gate(self, item: WorkItem) -> None:
        discovery = item.payload.get("discovery")
        if not isinstance(discovery, FilingDiscovery):
            return
        acc = discovery.accession_number

        # Skip if already resolved to a terminal state (e.g. by a parallel path).
        if await self.storage.is_filing_terminal(acc):
            logger.debug("header_gate skip (terminal): acc=%s", acc)
            return

        try:
            header = await self.retriever.fetch_header_only(discovery)
        except SECHTTPError as exc:
            if exc.is_retryable:
                logger.warning(
                    "header-gate transient failure for %s (HTTP %d) — will retry",
                    acc, exc.status,
                )
                await self.storage.set_hdr_transient_fail(
                    acc, retry_base_seconds=self.settings.retry_base_seconds,
                )
                return
            # Non-retryable HTTP errors (404, 403, etc.) are permanent.
            logger.warning("header-gate permanent HTTP failure for %s (HTTP %d)", acc, exc.status)
            await self.storage.update_relevance(acc, RelevanceState.HDR_FAILED)
            return
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("header-gate transient error for %s: %s — will retry", acc, exc)
            await self.storage.set_hdr_transient_fail(
                acc, retry_base_seconds=self.settings.retry_base_seconds,
            )
            return
        except Exception:
            # Unexpected parse/logic errors are permanent.
            logger.exception("header-gate permanent failure for %s", acc)
            await self.storage.update_relevance(acc, RelevanceState.HDR_FAILED)
            return

        state, matched_company, canonical = self.header_resolver.resolve(header)

        if canonical:
            await self.storage.update_relevance(
                acc, state,
                issuer_cik=canonical.cik, issuer_name=canonical.name,
            )
            if header.parties:
                await self.storage.save_filing_parties(acc, header.parties)
        else:
            await self.storage.update_relevance(acc, state)
            logger.info(
                "header-gate resolved without canonical issuer: acc=%s state=%s form=%s",
                acc, state.value, discovery.form_type,
            )

        if state == RelevanceState.HDR_MATCH:
            METRICS.inc("edgar_header_gate_matched_total")
            logger.info("header-gate MATCH for %s → %s (%s)",
                acc,
                matched_company.ticker if matched_company else "?",
                discovery.form_type)
            queued = await self.storage.set_retrieval_queued(acc)
            if queued:
                await self._enqueue(make_work(
                    "retrieval", acc,
                    FilingPriority.RETRIEVAL, discovery=discovery,
                ))
            else:
                logger.debug(
                    "header-gate MATCH but cooling_down: acc=%s (backoff not expired)", acc,
                )
        elif state == RelevanceState.UNRESOLVED:
            METRICS.inc("edgar_header_gate_unresolved_total")
            logger.info("header-gate UNRESOLVED for %s (13D/G)", acc)
        else:
            logger.debug("header-gate %s for %s", state.value, acc)

    async def _handle_retrieval(self, item: WorkItem) -> None:
        discovery = item.payload.get("discovery")
        if not isinstance(discovery, FilingDiscovery):
            return
        acc = discovery.accession_number

        existing = await self.storage.get_filing(acc)
        if existing is None:
            logger.warning("retrieval skip: filing %s not found in DB", acc)
            return
        if existing.retrieval_status in _TERMINAL_RETRIEVAL_STATUSES:
            logger.debug("retrieval skip (already retrieved): acc=%s kind=%s", acc, item.kind)
            return

        # A filing whose header-gate resolved to hdr_failed, unresolved,
        # irrelevant, or direct_unmatched should never enter retrieval.
        # This guards against stale queue entries or replay races.
        _NON_RETRIEVAL_RELEVANCE = frozenset({
            RelevanceState.HDR_FAILED.value,
            RelevanceState.UNRESOLVED.value,
            RelevanceState.IRRELEVANT.value,
            RelevanceState.DIRECT_UNMATCHED.value,
        })
        if existing.relevance_state in _NON_RETRIEVAL_RELEVANCE:
            logger.info(
                "retrieval skip (terminal relevance): acc=%s relevance=%s kind=%s",
                acc, existing.relevance_state, item.kind,
            )
            return

        if item.kind == "retry":
            if existing.retrieval_status not in (
                RetrievalStatus.RETRIEVAL_FAILED.value,
                RetrievalStatus.RETRIEVED_PARTIAL.value,
            ):
                logger.debug(
                    "retry skip (status=%s not retryable): acc=%s",
                    existing.retrieval_status, acc,
                )
                return
            if existing.attempt_count >= MAX_FILING_RETRY_ATTEMPTS:
                logger.warning(
                    "retry skip (max attempts %d reached): acc=%s",
                    existing.attempt_count, acc,
                )
                return

        await self.storage.set_retrieval_in_progress(acc)
        logger.info("retrieval starting: acc=%s kind=%s", acc, item.kind)
        t0 = _time.monotonic()
        success = await self.retriever.retrieve_full(discovery)
        elapsed = _time.monotonic() - t0
        METRICS.observe("edgar_retrieval_duration_seconds", elapsed)
        if success:
            # Track end-to-end latency from enqueue to event commit — only
            # on success so SLA dashboards aren't blurred by failure events
            # (extension_plan2 §9).
            pipeline_latency = _time.monotonic() - item.created_at
            METRICS.record_discovery_to_event_latency(pipeline_latency)
        else:
            METRICS.inc("edgar_retrieval_failed_events_total")
        # Wake the outbox publisher so new events are published immediately
        # instead of waiting for the next poll cycle.
        self._outbox_wake.set()

    # --- Outbox publisher ---

    async def _outbox_publisher(self) -> None:
        """Claim-and-publish loop for outbox events.

        When a batch publisher is available, events are published in batches
        with a single fsync for the entire batch — much more efficient under
        burst load.  Falls back to per-event publishing when only a single-
        event callback is configured.

        All SQLite operations (lease, mark_published, mark_failed) are
        offloaded to a thread pool to avoid blocking the event loop.
        """
        # Reset any stale leases from a previous crash on startup
        reset = await asyncio.to_thread(self.outbox.reset_stale_leases)
        if reset:
            logger.info("outbox publisher: reset %d stale leases from previous run", reset)

        has_publisher = (
            self._publish_callback is not None
            or self._batch_publish_callback is not None
        )

        if not has_publisher:
            logger.warning(
                "outbox publisher: no publish_callback configured — events will "
                "accumulate in 'pending' state.  Configure a callback (NATS/Kafka) "
                "for production use."
            )
            while not self._shutdown.is_set():
                pending = await asyncio.to_thread(self.outbox.pending_count)
                if pending > 0:
                    logger.info(
                        "outbox publisher: %d pending events (no publisher configured)",
                        pending,
                    )
                await asyncio.sleep(30.0)
            return

        while not self._shutdown.is_set():
            try:
                events = await asyncio.to_thread(self.outbox.lease_pending, 50)
                if events:
                    METRICS.set_gauge("edgar_outbox_leased", float(len(events)))
                    if self._batch_publish_callback is not None:
                        # Batch publish — one fsync for the whole batch
                        t0 = _time.monotonic()
                        try:
                            await self._batch_publish_callback(events)
                            elapsed = _time.monotonic() - t0
                            now_ts = utcnow()
                            published_count = 0
                            stale_count = 0
                            for env in events:
                                ok = await asyncio.to_thread(
                                    self.outbox.mark_published, env.event_id, env.lease_token,
                                )
                                if not ok:
                                    # Stale lease: another process or expired lease
                                    # claimed this event.  Do NOT count as published.
                                    # (Extension plan §4E.)
                                    stale_count += 1
                                    logger.warning(
                                        "outbox: stale lease for event %s — "
                                        "skipping publish metrics",
                                        env.event_id,
                                    )
                                    METRICS.inc("edgar_outbox_stale_lease_total")
                                    continue
                                published_count += 1
                                # Track event-to-publish latency for trading SLA
                                try:
                                    created = try_parse_datetime(env.created_at)
                                    if created:
                                        e2p = (now_ts - created).total_seconds()
                                        METRICS.record_event_to_publish_latency(e2p)
                                except Exception:
                                    pass
                            if published_count:
                                METRICS.inc("edgar_outbox_published_total", float(published_count))
                            METRICS.observe("edgar_outbox_publish_duration_seconds", elapsed)
                            METRICS.touch("edgar_last_outbox_publish_success_unixtime")
                        except Exception as exc:
                            logger.warning(
                                "outbox batch publish failed (%d events): %s",
                                len(events), exc,
                            )
                            for env in events:
                                await asyncio.to_thread(
                                    self.outbox.mark_failed, env.event_id, str(exc), env.lease_token,
                                )
                            METRICS.inc("edgar_outbox_failed_total", float(len(events)))
                    else:
                        # Per-event publish (legacy path)
                        for env in events:
                            t0 = _time.monotonic()
                            try:
                                await self._publish_callback(env)
                                elapsed = _time.monotonic() - t0
                                ok = await asyncio.to_thread(
                                    self.outbox.mark_published, env.event_id, env.lease_token,
                                )
                                if not ok:
                                    # Stale lease — do not count as published (§4E).
                                    logger.warning(
                                        "outbox: stale lease for event %s — "
                                        "skipping publish metrics",
                                        env.event_id,
                                    )
                                    METRICS.inc("edgar_outbox_stale_lease_total")
                                    continue
                                METRICS.inc("edgar_outbox_published_total")
                                METRICS.observe("edgar_outbox_publish_duration_seconds", elapsed)
                                METRICS.touch("edgar_last_outbox_publish_success_unixtime")
                            except Exception as exc:
                                logger.warning(
                                    "outbox publish failed for event %s: %s",
                                    env.event_id, exc,
                                )
                                await asyncio.to_thread(
                                    self.outbox.mark_failed, env.event_id, str(exc), env.lease_token,
                                )
                                METRICS.inc("edgar_outbox_failed_total")
                # Update pending gauge on every cycle
                METRICS.set_gauge("edgar_outbox_pending",
                                  float(await asyncio.to_thread(self.outbox.pending_count)))
                METRICS.set_gauge("edgar_outbox_leased", 0.0)
            except Exception:
                logger.exception("error in outbox publisher")
            # Wait for a wake signal (set by _handle_retrieval after new events
            # are committed) or fall back to a short poll as backstop.
            self._outbox_wake.clear()
            try:
                await asyncio.wait_for(self._outbox_wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    # --- Atom feed poller (paged) ---

    async def _atom_poller(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._poll_atom_once()
            except Exception:
                logger.exception("error during Atom poll")
            await asyncio.sleep(self.settings.latest_poll_seconds)

    async def _poll_atom_once(self) -> int:
        wm = FeedWatermark.deserialize(await self.storage.get_checkpoint(self._ATOM_WM_KEY))

        all_discoveries: list[FilingDiscovery] = []
        boundary_found = wm.accepted_at is None
        page = 0
        max_pages = 10

        while page < max_pages:
            url = LATEST_FILINGS_ATOM_URL.format(start=page * 100, count=100)
            xml_text = await self.client.get_text(url)
            discoveries = parse_latest_filings_atom(xml_text)
            discoveries = filter_by_forms(discoveries, self.settings.all_forms)

            if not discoveries:
                break

            for d in discoveries:
                if wm.accepted_at and d.accepted_at:
                    if d.accepted_at < wm.accepted_at:
                        boundary_found = True
                    elif d.accepted_at == wm.accepted_at:
                        if d.accession_number in wm.accessions_at_boundary:
                            boundary_found = True

            if wm.accepted_at:
                filtered = []
                for d in discoveries:
                    if d.accepted_at is None:
                        filtered.append(d)
                    elif d.accepted_at > wm.accepted_at:
                        filtered.append(d)
                    elif d.accepted_at == wm.accepted_at and d.accession_number not in wm.accessions_at_boundary:
                        filtered.append(d)
                discoveries = filtered

            all_discoveries.extend(discoveries)
            if boundary_found:
                break
            page += 1

        if not boundary_found and wm.accepted_at is not None:
            logger.warning(
                "FEED GAP: prior watermark %s not found in %d Atom pages.",
                wm.accepted_at.isoformat(), page + 1,
            )
            # Emit a durable feed-gap event so downstream consumers are aware
            gap_event = build_feed_gap_event(
                watermark_ts=wm.accepted_at.isoformat(),
                pages_checked=page + 1,
            )
            def _insert_gap_event() -> None:
                with self.storage.sync._conn() as conn:
                    self.outbox.insert_event(conn, gap_event)
                    conn.commit()
            await asyncio.to_thread(_insert_gap_event)

        seen: set[str] = set()
        deduped: list[FilingDiscovery] = []
        for d in all_discoveries:
            if d.accession_number not in seen:
                seen.add(d.accession_number)
                deduped.append(d)

        # If the daemon restarts after the watermark moves but
        # before the consumer processes the in-memory queue, the filings
        # are already durably recorded and will be recovered by
        # _replay_stale_work().
        for d in deduped:
            await self.storage.upsert_discovery(d)

        if deduped:
            await self._advance_watermark(deduped, wm)

        count = 0
        for d in deduped:
            await self._enqueue(make_work(
                "discovery", d.accession_number, FilingPriority.LIVE, discovery=d,
            ))
            count += 1

        if count > 0:
            logger.info("Atom poll: %d new discoveries from %d pages", count, page + 1)
        METRICS.touch("edgar_last_atom_poll_success_unixtime")
        return count

    async def _advance_watermark(self, discoveries: list[FilingDiscovery], prev: FeedWatermark) -> None:
        max_ts = prev.accepted_at
        for d in discoveries:
            if d.accepted_at and (max_ts is None or d.accepted_at > max_ts):
                max_ts = d.accepted_at
        if max_ts is None:
            return
        if max_ts == prev.accepted_at:
            at_max = set(prev.accessions_at_boundary)
        else:
            at_max: set[str] = set()
        for d in discoveries:
            if d.accepted_at == max_ts:
                at_max.add(d.accession_number)
        wm = FeedWatermark(accepted_at=max_ts, accessions_at_boundary=frozenset(at_max))
        await self.storage.set_checkpoint(self._ATOM_WM_KEY, wm.serialize())

    # --- Retry scanner ---

    async def _retry_scanner(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(self.settings.retry_failed_poll_seconds)
            try:
                # Retrieval retries (existing)
                candidates = await self.storage.list_retry_candidates(limit=20)
                enqueued = 0
                for record in candidates:
                    async with self._inflight_lock:
                        if record.accession_number in self._inflight:
                            logger.debug(
                                "retry skip (in-flight): acc=%s", record.accession_number,
                            )
                            continue
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source=record.source,
                        complete_txt_url=derive_complete_txt_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "retry", d.accession_number, FilingPriority.RETRY, discovery=d,
                    ))
                    enqueued += 1

                hdr_retries = await self.storage.list_hdr_transient_fail(limit=20)
                hdr_enqueued = 0
                for record in hdr_retries:
                    async with self._inflight_lock:
                        if record.accession_number in self._inflight:
                            continue
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="retry_hdr_transient",
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    # Reset to hdr_pending so the handler processes it normally
                    await self.storage.update_relevance(
                        record.accession_number, RelevanceState.HDR_PENDING,
                    )
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "header_gate", d.accession_number,
                        FilingPriority.HEADER_GATE, discovery=d,
                    ))
                    hdr_enqueued += 1

                # hdr_pending recovery — filings that were set to hdr_pending
                # but whose enqueue was deferred under queue pressure during
                # backfill/bootstrap.  Without this sweep they are only
                # recovered on restart via _replay_stale_work.
                hdr_pending = await self.storage.list_hdr_pending(limit=20)
                hdr_pending_enqueued = 0
                for record in hdr_pending:
                    async with self._inflight_lock:
                        if record.accession_number in self._inflight:
                            continue
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="retry_hdr_pending",
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "header_gate", d.accession_number,
                        FilingPriority.HEADER_GATE, discovery=d,
                    ))
                    hdr_pending_enqueued += 1

                if enqueued or hdr_enqueued or hdr_pending_enqueued:
                    logger.info(
                        "retry scanner: enqueued %d retrieval + %d hdr_transient "
                        "+ %d hdr_pending retries",
                        enqueued, hdr_enqueued, hdr_pending_enqueued,
                    )
            except Exception:
                logger.exception("error in retry scanner")

    # --- Fast stranded-work scanner
    async def _stranded_scanner(self) -> None:
        """Frequent scanner for filings stranded by queue saturation.

        Runs every 10 seconds and re-enqueues ``queued`` filings that are
        not currently in-flight.  This closes the liveness gap when ``_enqueue()`` fails due to queue pressure,
        the filing remains ``queued`` in SQLite but has no in-memory
        representation.  Without this scanner, such filings would only be
        recovered by startup replay or weekly repair — far too slow for a
        latency-sensitive signal system.

        This deliberately overlaps with the retry scanner and weekly repair;
        the per-accession in-flight guard prevents duplicate work.
        """
        while not self._shutdown.is_set():
            await asyncio.sleep(10.0)
            try:
                stranded = await self.storage.list_stranded_work(limit=50)
                if not stranded:
                    continue
                enqueued = 0
                for record in stranded:
                    async with self._inflight_lock:
                        if record.accession_number in self._inflight:
                            continue
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="stranded_scanner",
                        complete_txt_url=derive_complete_txt_url(
                            record.archive_cik, record.accession_number,
                        ),
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    # Route based on relevance state: hdr_match/direct_match → retrieval,
                    # hdr_pending → header_gate, else → discovery for re-classification.
                    if record.relevance_state in (
                        RelevanceState.DIRECT_MATCH.value,
                        RelevanceState.HDR_MATCH.value,
                    ):
                        await self.storage.set_retrieval_queued(record.accession_number, force=True)
                        ok = await self._enqueue(make_work(
                            "retrieval", d.accession_number,
                            FilingPriority.RETRY, discovery=d,
                        ))
                    elif record.relevance_state == RelevanceState.HDR_PENDING.value:
                        await self.storage.set_retrieval_queued(record.accession_number, force=True)
                        ok = await self._enqueue(make_work(
                            "header_gate", d.accession_number,
                            FilingPriority.HEADER_GATE, discovery=d,
                        ))
                    else:
                        ok = await self._enqueue(make_work(
                            "discovery", d.accession_number,
                            FilingPriority.RETRY, discovery=d,
                        ))
                    if ok:
                        enqueued += 1
                if enqueued:
                    logger.info(
                        "stranded scanner: re-enqueued %d/%d stranded filings",
                        enqueued, len(stranded),
                    )
            except Exception:
                logger.exception("error in stranded scanner")

    # --- Issuer audit ---

    async def _issuer_audit(self) -> None:
        # Wait for bootstrap to finish before first audit cycle ──
        if not self._bootstrap_done.is_set():
            logger.info("issuer audit: waiting for bootstrap to complete before first sweep")
            await self._bootstrap_done.wait()
            logger.info("issuer audit: bootstrap complete, starting first sweep")

        for cycle in range(999999):
            if cycle > 0:
                await asyncio.sleep(self.settings.watchlist_audit_seconds)
            if self._shutdown.is_set():
                return
            try:
                logger.info(
                    "issuer audit: starting sweep %d of %d issuers",
                    cycle + 1, len(self.watchlist),
                )
                sweep_new = 0
                sweep_skipped = 0
                for company in self.watchlist.companies:
                    if self._shutdown.is_set():
                        return
                    try:
                        payload = await self.hist_client.get_json(
                            f"https://data.sec.gov/submissions/CIK{company.cik}.json",
                        )
                        discoveries = parse_submissions_json(payload)
                        discoveries = filter_by_forms(discoveries, self.settings.all_forms)

                        candidate_accs = [d.accession_number for d in discoveries]
                        known_in_batch = await self.storage.accessions_exist_batch(candidate_accs)

                        for d in discoveries:
                            if d.accession_number in known_in_batch:
                                if await self.storage.is_filing_terminal(d.accession_number):
                                    sweep_skipped += 1
                                    continue
                                # Non-terminal but known — do not re-enqueue if
                                # still cooling down (backoff clock is authoritative).
                                if await self.storage.is_retry_cooling_down(d.accession_number):
                                    sweep_skipped += 1
                                    continue
                            await self._enqueue(make_work(
                                "discovery", d.accession_number,
                                FilingPriority.AUDIT, discovery=d,
                            ))
                            sweep_new += 1
                    except Exception:
                        logger.exception("audit fetch failed for %s (%s)", company.ticker, company.cik)
                logger.info(
                    "issuer audit: sweep %d complete — %d enqueued, %d skipped (terminal)",
                    cycle + 1, sweep_new, sweep_skipped,
                )
            except Exception:
                logger.exception("error in issuer audit")

    # --- Daily reconciler ---

    async def _daily_reconciler(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(self.settings.reconcile_poll_seconds)
            try:
                await self._reconcile_once()
            except Exception:
                logger.exception("error in daily reconciler")

    async def _reconcile_once(self) -> int:
        """Run one incremental daily-index reconciliation cycle."""
        # Use SEC's Eastern-Time business date, not the VM's
        # local date, so reconciliation windows align with SEC publishing.
        today = sec_business_date()

        last_str = await self.storage.get_checkpoint(self._RECONCILE_KEY)
        if last_str:
            last_date = try_parse_date(last_str)
        else:
            last_date = None

        if last_date is None:
            start = today - timedelta(days=14)
        else:
            start = last_date + timedelta(days=1)

        if start > today:
            return 0

        # Collect business days
        days_to_check: list[date] = []
        current = start
        while current <= today:
            if current.weekday() < 5:
                days_to_check.append(current)
            current += timedelta(days=1)

        if not days_to_check:
            return 0

        logger.info(
            "daily-index reconciliation: checking %d business days (%s to %s)",
            len(days_to_check), days_to_check[0].isoformat(), days_to_check[-1].isoformat(),
        )

        total_new = 0
        latest_success: date | None = last_date
        grace_cutoff = today - timedelta(days=self._GRACE_DAYS_FOR_404)

        for d in days_to_check:
            qtr = (d.month - 1) // 3 + 1
            url = f"{self._DAILY_INDEX_BASE}/{d.year}/QTR{qtr}/company.{d.strftime('%Y%m%d')}.idx"
            try:
                text = await self.hist_client.get_text(url)
            except SECHTTPError as exc:
                if exc.status == 404:
                    if d > grace_cutoff:
                        logger.info("daily index 404 for %s — within grace window, will retry", d.isoformat())
                        break
                    else:
                        latest_success = d
                        continue
                logger.warning("daily index error for %s: %s — will retry next cycle", d.isoformat(), exc)
                break

            discoveries = parse_company_idx(text)
            discoveries = filter_by_forms(discoveries, self.settings.all_forms)
            new_discoveries: list[FilingDiscovery] = []
            for disc in discoveries:
                if not await self.storage.accession_exists(disc.accession_number):
                    new_discoveries.append(disc)

            for disc in new_discoveries:
                await self.storage.upsert_discovery(disc)
                form = disc.form_type.upper().strip()
                # Set relevance + queued state before enqueueing
                if is_direct_form(form, self.settings.direct_forms):
                    if self.watchlist.contains_cik(disc.archive_cik):
                        await self.storage.update_relevance(
                            disc.accession_number, RelevanceState.DIRECT_MATCH,
                        )
                        await self.storage.set_retrieval_queued(disc.accession_number)
                        await self._enqueue(make_work(
                            "retrieval", disc.accession_number,
                            FilingPriority.REPAIR, discovery=disc,
                        ))
                    else:
                        await self.storage.update_relevance(
                            disc.accession_number, RelevanceState.DIRECT_UNMATCHED,
                        )
                elif is_ambiguous_form(form, self.settings.ambiguous_forms):
                    await self.storage.update_relevance(
                        disc.accession_number, RelevanceState.HDR_PENDING,
                    )
                    await self.storage.set_retrieval_queued(disc.accession_number)
                    await self._enqueue(make_work(
                        "header_gate", disc.accession_number,
                        FilingPriority.REPAIR, discovery=disc,
                    ))

            total_new += len(new_discoveries)
            latest_success = d
            logger.info("reconciled %s: %d new filings", d.isoformat(), len(new_discoveries))

        if latest_success and (last_date is None or latest_success > last_date):
            await self.storage.set_checkpoint(self._RECONCILE_KEY, latest_success.isoformat())

        return total_new

    # --- Bootstrap ---

    async def _is_bootstrap_complete(self) -> bool:
        raw = await self.storage.get_checkpoint(self._BOOTSTRAP_KEY)
        return raw == "true"

    async def _bootstrap(self) -> None:
        """Non-blocking staged bootstrap for historical data.

        Only marks ``bootstrap_complete`` when **every** watched issuer
        (including all rollover history files) has been successfully processed.
        Per-issuer failures are logged and tracked; the overall flag remains
        ``false`` until all issuers pass.

        If the initial bootstrap has failures, retries every
        30 minutes inside the running process instead of requiring a restart.
        """
        _BOOTSTRAP_RETRY_INTERVAL = 1800  # 30 minutes

        logger.info("starting non-blocking bootstrap (lookback=%d days)", self.settings.backfill_lookback_days)
        try:
            all_success = await self._bootstrap_direct()
            if all_success:
                await self.storage.set_checkpoint(self._BOOTSTRAP_KEY, "true")
                METRICS.set_gauge("edgar_bootstrap_complete", 1.0)
                logger.info("bootstrap complete — all issuers processed successfully")
            else:
                logger.warning(
                    "bootstrap finished with incomplete coverage — "
                    "scheduling periodic retry (every %ds)",
                    _BOOTSTRAP_RETRY_INTERVAL,
                )
        except Exception:
            logger.exception("bootstrap failed — scheduling periodic retry")
            all_success = False
        finally:
            # Always signal bootstrap completion so audit can proceed
            self._bootstrap_done.set()
            logger.info("bootstrap done event set — issuer audit unblocked")

        # Reriodic retry for failed bootstrap
        retry_count = 0
        while not all_success and not self._shutdown.is_set():
            retry_count += 1
            logger.info(
                "bootstrap retry #%d: waiting %ds before re-attempting incomplete issuers",
                retry_count, _BOOTSTRAP_RETRY_INTERVAL,
            )
            # Interruptible sleep
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=_BOOTSTRAP_RETRY_INTERVAL,
                )
                break  # shutdown signalled
            except asyncio.TimeoutError:
                pass

            try:
                all_success = await self._bootstrap_direct()
                if all_success:
                    await self.storage.set_checkpoint(self._BOOTSTRAP_KEY, "true")
                    METRICS.set_gauge("edgar_bootstrap_complete", 1.0)
                    logger.info(
                        "bootstrap retry #%d complete — all issuers processed successfully",
                        retry_count,
                    )
                else:
                    logger.warning(
                        "bootstrap retry #%d still incomplete — will retry in %ds",
                        retry_count, _BOOTSTRAP_RETRY_INTERVAL,
                    )
            except Exception:
                logger.exception("bootstrap retry #%d failed", retry_count)

    async def _bootstrap_direct(self) -> bool:
        """Phase A: Direct backfill via per-CIK submissions JSON.

        For each watched issuer, fetch submissions JSON (+ rollover files),
        filter to forms within the lookback window, and enqueue at backfill priority.

        Returns True only if **every** watched issuer was successfully processed
        (including all rollover history files).  Per-issuer failures are tracked
        so that the overall ``bootstrap_complete`` flag remains false until all
        issuers pass.
        """
        cutoff = sec_business_date() - timedelta(days=self.settings.backfill_lookback_days)
        checkpoint_key = "bootstrap_direct_last_cik"
        last_cik = await self.storage.get_checkpoint(checkpoint_key)

        companies = self.watchlist.companies
        # Resume from checkpoint
        start_idx = 0
        if last_cik:
            for i, c in enumerate(companies):
                if c.cik == last_cik:
                    start_idx = i + 1
                    break

        failed_issuers: list[str] = []
        for i in range(start_idx, len(companies)):
            if self._shutdown.is_set():
                return False
            company = companies[i]
            try:
                payload = await self.hist_client.get_json(
                    f"https://data.sec.gov/submissions/CIK{company.cik}.json",
                )
                discoveries = parse_submissions_json(payload)

                # Fetch rollover files for full history — track failures
                rollover_urls = extract_submissions_rollover_urls(payload)
                cik = normalize_cik(str(payload.get("cik", "0")))
                name = str(payload.get("name", "UNKNOWN"))
                rollover_failed = False
                for rurl in rollover_urls:
                    try:
                        rpayload = await self.hist_client.get_json(rurl)
                        discoveries.extend(
                            parse_submissions_rollover_json(rpayload, cik, name)
                        )
                    except Exception:
                        logger.warning("rollover fetch failed: %s", rurl)
                        rollover_failed = True

                if rollover_failed:
                    # Rollover history was incomplete — mark this issuer as failed
                    # so bootstrap_complete stays false and we retry on restart.
                    failed_issuers.append(company.cik)
                    logger.warning(
                        "bootstrap: %s (%s) has incomplete rollover history — "
                        "will retry on next start",
                        company.ticker, company.cik,
                    )

                # Filter to relevant forms within lookback window
                discoveries = filter_by_forms(discoveries, self.settings.all_forms)
                discoveries = [d for d in discoveries if d.filing_date is None or d.filing_date >= cutoff]

                batch_seen: set[str] = set()
                new_count = 0
                deferred_count = 0
                for d in discoveries:
                    if d.accession_number in batch_seen:
                        continue
                    if await self.storage.accession_exists(d.accession_number):
                        batch_seen.add(d.accession_number)
                        continue
                    await self.storage.upsert_discovery(d)
                    form = d.form_type.upper().strip()
                    # Set relevance + queued state before enqueueing
                    # so crash recovery can find these filings via replay.
                    if is_direct_form(form, self.settings.direct_forms):
                        await self.storage.update_relevance(
                            d.accession_number, RelevanceState.DIRECT_MATCH,
                        )
                        await self.storage.set_retrieval_queued(d.accession_number, force=True)
                        enqueued = await self._enqueue(make_work(
                            "retrieval", d.accession_number,
                            FilingPriority.BACKFILL, discovery=d,
                        ))
                        if not enqueued:
                            deferred_count += 1
                    elif is_ambiguous_form(form, self.settings.ambiguous_forms):
                        await self.storage.update_relevance(
                            d.accession_number, RelevanceState.HDR_PENDING,
                        )
                        await self.storage.set_retrieval_queued(d.accession_number, force=True)
                        enqueued = await self._enqueue(make_work(
                            "header_gate", d.accession_number,
                            FilingPriority.BACKFILL, discovery=d,
                        ))
                        if not enqueued:
                            deferred_count += 1
                    new_count += 1
                    batch_seen.add(d.accession_number)

                logger.info(
                    "bootstrap: %s (%s) — %d new filings enqueued, %d deferred",
                    company.ticker, company.cik, new_count, deferred_count,
                )
                # Only advance the per-CIK checkpoint when all items were
                # successfully enqueued.  Deferred items are already persisted
                # in SQLite and will be recovered by replay/audit, but skipping
                # the checkpoint means this CIK will be revisited on restart.
                if deferred_count == 0 and not rollover_failed:
                    await self.storage.set_checkpoint(checkpoint_key, company.cik)

            except Exception:
                logger.exception("bootstrap failed for %s (%s)", company.ticker, company.cik)
                failed_issuers.append(company.cik)

        if failed_issuers:
            logger.warning(
                "bootstrap: %d issuers had failures — %s",
                len(failed_issuers), ", ".join(failed_issuers[:10]),
            )
            return False
        return True

    # --- Weekly repair ---

    @staticmethod
    def _parse_cron_dow_hour(cron_expr: str) -> tuple[int, int, int]:
        """Extract (day_of_week, hour, minute) from a simple cron expression.

        Only handles ``minute hour * * dow`` — sufficient for the weekly
        repair schedule.  Returns ``(dow, hour, minute)`` where dow is
        0=Mon..6=Sun (Python convention, converted from cron's 0=Sun).
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return (5, 11, 0)  # Default: Saturday 11:00 UTC
        try:
            minute = int(parts[0])
            hour = int(parts[1])
            cron_dow = int(parts[4])
            # Cron: 0=Sun,1=Mon..6=Sat → Python: 0=Mon..6=Sun
            py_dow = (cron_dow - 1) % 7
            return (py_dow, hour, minute)
        except (ValueError, IndexError):
            return (5, 11, 0)

    async def _weekly_repair(self) -> None:
        """Periodic comprehensive repair sweep.

        Parses the ``weekly_repair_cron`` setting to determine day-of-week
        and hour, then sleeps until the next scheduled window.  Each run:
          1. Re-enqueues all stranded (queued/in_progress) work.
          2. Re-enqueues retrieval_failed / retrieved_partial filings
             that haven't exhausted retry attempts.
          3. Re-enqueues hdr_transient_fail filings.
          4. Re-enqueues unprocessed discoveries.

        This is deliberately redundant with the retry scanner and startup
        replay — the goal is to catch anything those faster loops missed
        due to timing, queue pressure, or transient issues.
        """
        dow, hour, minute = self._parse_cron_dow_hour(self.settings.weekly_repair_cron)

        # Wait for bootstrap to finish before running the first repair.
        if not self._bootstrap_done.is_set():
            await self._bootstrap_done.wait()

        while not self._shutdown.is_set():
            # Check if it's time to run.
            now = utcnow()
            last_run_str = await self.storage.get_checkpoint(self._WEEKLY_REPAIR_KEY)
            if last_run_str:
                last_run = try_parse_date(last_run_str)
                if last_run and (now.date() - last_run).days < 6:
                    # Not enough time has passed; sleep and recheck.
                    await asyncio.sleep(3600)
                    continue

            # Wait until the scheduled day/hour/minute.
            if now.weekday() != dow or now.hour < hour or (now.hour == hour and now.minute < minute):
                await asyncio.sleep(60)
                continue

            logger.info("weekly repair: starting comprehensive sweep")
            try:
                total = 0

                # 1. Stranded work
                stranded = await self.storage.list_stranded_work(limit=500)
                for record in stranded:
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="weekly_repair_stranded",
                        complete_txt_url=derive_complete_txt_url(
                            record.archive_cik, record.accession_number,
                        ),
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    # force=True: weekly repair intentionally overrides backoff
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "retrieval", d.accession_number,
                        FilingPriority.REPAIR, discovery=d,
                    ))
                    total += 1

                # 2. Retry candidates (failed / partial)
                retries = await self.storage.list_retry_candidates(limit=200)
                for record in retries:
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="weekly_repair_retry",
                        complete_txt_url=derive_complete_txt_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    # force=True: weekly repair intentionally overrides backoff
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "retry", d.accession_number,
                        FilingPriority.REPAIR, discovery=d,
                    ))
                    total += 1

                # 3. Transient header failures
                hdr_transient = await self.storage.list_hdr_transient_fail(limit=200)
                for record in hdr_transient:
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="weekly_repair_hdr_transient",
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    await self.storage.update_relevance(
                        record.accession_number, RelevanceState.HDR_PENDING,
                    )
                    # force=True: weekly repair intentionally overrides backoff
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "header_gate", d.accession_number,
                        FilingPriority.REPAIR, discovery=d,
                    ))
                    total += 1

                # 4. Unprocessed discoveries
                unprocessed = await self.storage.list_unprocessed_discoveries(limit=200)
                for record in unprocessed:
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="weekly_repair_unprocessed",
                        complete_txt_url=derive_complete_txt_url(
                            record.archive_cik, record.accession_number,
                        ),
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    await self._enqueue(make_work(
                        "discovery", d.accession_number,
                        FilingPriority.REPAIR, discovery=d,
                    ))
                    total += 1

                # 5. Stranded hdr_pending — filings that were deferred by
                # queue pressure during backfill and never re-enqueued
                hdr_pending = await self.storage.list_hdr_pending(limit=200)
                for record in hdr_pending:
                    d = FilingDiscovery(
                        accession_number=record.accession_number,
                        archive_cik=record.archive_cik,
                        form_type=record.form_type,
                        company_name=record.company_name,
                        source="weekly_repair_hdr_pending",
                        hdr_sgml_url=derive_hdr_sgml_url(
                            record.archive_cik, record.accession_number,
                        ),
                    )
                    # force=True: weekly repair intentionally overrides backoff
                    await self.storage.set_retrieval_queued(record.accession_number, force=True)
                    await self._enqueue(make_work(
                        "header_gate", d.accession_number,
                        FilingPriority.REPAIR, discovery=d,
                    ))
                    total += 1

                await self.storage.set_checkpoint(
                    self._WEEKLY_REPAIR_KEY, now.date().isoformat(),
                )
                logger.info(
                    "weekly repair complete: %d items re-enqueued "
                    "(%d stranded, %d retries, %d hdr_transient, %d unprocessed, "
                    "%d hdr_pending)",
                    total, len(stranded), len(retries),
                    len(hdr_transient), len(unprocessed), len(hdr_pending),
                )
            except Exception:
                logger.exception("error in weekly repair")

            # Sleep until next week
            await asyncio.sleep(3600 * 24)

    # --- Stale work replay ---

    async def _replay_stale_work(self) -> None:
        retries = await self.storage.list_retry_candidates(limit=100)
        pending = await self.storage.list_hdr_pending(limit=100)
        hdr_transient = await self.storage.list_hdr_transient_fail(limit=100)
        # Recover filings stranded in queued/in_progress ──
        stranded = await self.storage.list_stranded_work(limit=200)
        # Recover filings left in unknown+discovered after a crash ──
        unprocessed = await self.storage.list_unprocessed_discoveries(limit=200)

        for record in retries:
            d = FilingDiscovery(
                accession_number=record.accession_number,
                archive_cik=record.archive_cik,
                form_type=record.form_type,
                company_name=record.company_name,
                source="startup_replay",
                complete_txt_url=derive_complete_txt_url(record.archive_cik, record.accession_number),
            )
            await self.storage.set_retrieval_queued(record.accession_number, force=True)
            await self._enqueue(make_work(
                "retry", d.accession_number, FilingPriority.RETRY, discovery=d,
            ))
        for record in pending:
            d = FilingDiscovery(
                accession_number=record.accession_number,
                archive_cik=record.archive_cik,
                form_type=record.form_type,
                company_name=record.company_name,
                source="startup_replay",
                hdr_sgml_url=derive_hdr_sgml_url(record.archive_cik, record.accession_number),
            )
            await self.storage.set_retrieval_queued(record.accession_number, force=True)
            await self._enqueue(make_work(
                "header_gate", d.accession_number, FilingPriority.HEADER_GATE, discovery=d,
            ))

        # Re-enqueue transient header failures as header_gate work.
        for record in hdr_transient:
            d = FilingDiscovery(
                accession_number=record.accession_number,
                archive_cik=record.archive_cik,
                form_type=record.form_type,
                company_name=record.company_name,
                source="startup_replay_hdr_transient",
                hdr_sgml_url=derive_hdr_sgml_url(record.archive_cik, record.accession_number),
            )
            await self.storage.update_relevance(record.accession_number, RelevanceState.HDR_PENDING)
            await self.storage.set_retrieval_queued(record.accession_number, force=True)
            await self._enqueue(make_work(
                "header_gate", d.accession_number, FilingPriority.HEADER_GATE, discovery=d,
            ))

        # Stranded items already had their relevance resolved in a previous
        # run and are not hdr_pending (those are covered by list_hdr_pending
        # above).  They go straight to retrieval.
        for record in stranded:
            d = FilingDiscovery(
                accession_number=record.accession_number,
                archive_cik=record.archive_cik,
                form_type=record.form_type,
                company_name=record.company_name,
                source="startup_replay_stranded",
                complete_txt_url=derive_complete_txt_url(record.archive_cik, record.accession_number),
                hdr_sgml_url=derive_hdr_sgml_url(record.archive_cik, record.accession_number),
            )
            await self.storage.set_retrieval_queued(record.accession_number, force=True)
            await self._enqueue(make_work(
                "retrieval", d.accession_number, FilingPriority.RETRY, discovery=d,
            ))

        # Unprocessed discoveries: re-enter the discovery classification path
        # so they get relevance-resolved and routed to retrieval or header-gate.
        for record in unprocessed:
            d = FilingDiscovery(
                accession_number=record.accession_number,
                archive_cik=record.archive_cik,
                form_type=record.form_type,
                company_name=record.company_name,
                source="startup_replay_unprocessed",
                complete_txt_url=derive_complete_txt_url(record.archive_cik, record.accession_number),
                hdr_sgml_url=derive_hdr_sgml_url(record.archive_cik, record.accession_number),
            )
            await self._enqueue(make_work(
                "discovery", d.accession_number, FilingPriority.RETRY, discovery=d,
            ))

        total = len(retries) + len(pending) + len(hdr_transient) + len(stranded) + len(unprocessed)
        if total:
            logger.info(
                "startup replay: enqueued %d retries + %d hdr_pending + %d hdr_transient "
                "+ %d stranded + %d unprocessed_discoveries (max_attempts=%d)",
                len(retries), len(pending), len(hdr_transient),
                len(stranded), len(unprocessed),
                MAX_FILING_RETRY_ATTEMPTS,
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="EDGAR filing ingestor daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--user-agent", required=True)
    p.add_argument("--db-path", type=Path, default=Path("./data/edgar.db"))
    p.add_argument("--raw-dir", type=Path, default=Path("./data/raw"))
    p.add_argument("--watchlist-file", type=Path, default=None)
    p.add_argument("--max-rps", type=float, default=5.0)
    p.add_argument("--latest-poll-seconds", type=int, default=5,
                   help="Atom feed poll interval in seconds")
    p.add_argument("--watchlist-audit-seconds", type=int, default=21600)
    p.add_argument("--backfill-lookback-days", type=int, default=365)
    p.add_argument("--reconcile-poll-seconds", type=int, default=3600)
    p.add_argument("--weekly-repair-cron", default="0 11 * * 6",
                   help="Weekly repair schedule.  Only 'minute hour * * dow' "
                        "format is supported (no ranges/lists/steps)")
    p.add_argument("--http-timeout-seconds", type=float, default=20.0)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--retry-base-seconds", type=float, default=2.0)
    p.add_argument("--retry-failed-poll-seconds", type=int, default=300)
    p.add_argument("--publish-dir", type=Path, default=None,
                   help="Directory for JSON-lines event publishing.  When set, "
                        "outbox events are written to date-partitioned .jsonl "
                        "files that downstream consumers can tail.  When unset, "
                        "defaults to <db_path>/../events/")
    p.add_argument("--live-workers", type=int, default=3,
                   help="Number of concurrent live-lane consumer workers.  "
                        "Higher values reduce head-of-line blocking when a "
                        "single retrieval is slow.  The per-accession in-flight "
                        "guard and global SEC rate limiter prevent duplicate "
                        "work and rate-limit violations.")
    p.add_argument("--live-rps-share", type=float, default=0.6,
                   help="Fraction of max-rps reserved for the live lane (0.1-0.9).  "
                        "The remainder is available for historical backfill, audit, "
                        "and reconciliation.  Higher values protect live latency at "
                        "the cost of slower historical processing.")
    # --- Metrics ---
    p.add_argument("--metrics-enabled", action="store_true", default=False,
                   help="Enable the in-process Prometheus metrics exporter.")
    p.add_argument("--metrics-host", default="127.0.0.1",
                   help="Bind address for the metrics HTTP server.")
    p.add_argument("--metrics-port", type=int, default=9108,
                   help="Port for the metrics HTTP server.")
    return p


async def _run(settings: Settings, watchlist: list[WatchlistCompany]) -> None:
    storage = SQLiteStorage(settings.db_path)
    storage.initialize()

    # Outbox schema is owned by event_outbox.py — initialize it here so
    # there is exactly one canonical DDL definition (no duplication).
    outbox = OutboxStore(
        storage,
        publish_retry_base_seconds=settings.retry_base_seconds,
    )
    with storage._conn() as conn:
        outbox.ensure_schema(conn)
        conn.commit()

    # Default publisher: write events as JSON-lines to the publish directory
    # so downstream consumers can tail/inotify-watch them.  Without this,
    # outbox events remain in 'pending' state and never leave SQLite.
    #
    # The batch publisher is preferred: it writes all leased events with a
    # single fsync, reducing I/O overhead under burst load while still
    # guaranteeing durability.
    publish_callback = None
    batch_publish_callback = None
    if settings.publish_dir:
        batch_publish_callback = make_jsonl_batch_publisher(settings.publish_dir)
        logger.info("JSONL batch publisher enabled: %s", settings.publish_dir)

    index = WatchlistIndex(watchlist)

    # Separate rate limiters for live and historical lanes ---
    # Live lane gets a protected share of SEC request capacity so that
    # historical backfill/audit/reconcile can never starve live discovery
    # and retrieval.
    from edgar_core import AsyncTokenBucket
    live_rps = settings.max_rps * settings.live_rps_share
    hist_rps = settings.max_rps * (1.0 - settings.live_rps_share)
    live_limiter = AsyncTokenBucket(live_rps)
    hist_limiter = AsyncTokenBucket(hist_rps)
    logger.info(
        "rate limiter split: live=%.2f rps, historical=%.2f rps (total=%.1f, share=%.0f%%)",
        live_rps, hist_rps, settings.max_rps, settings.live_rps_share * 100,
    )

    async with SECClient(settings, rate_limiter=live_limiter) as live_client, \
               SECClient(settings, rate_limiter=hist_limiter) as hist_client:
        daemon = IngestionDaemon(
            settings, storage, live_client, index,
            hist_client=hist_client,
            publish_callback=publish_callback,
            batch_publish_callback=batch_publish_callback,
        )
        await daemon.run()


def main() -> None:
    args = build_parser().parse_args()
    # Auto-derive publish_dir from db_path when not explicitly set so that
    # the default CLI launcher always has a publisher enabled.
    publish_dir = args.publish_dir
    if publish_dir is None:
        publish_dir = args.db_path.parent / "events"
    settings = Settings(
        user_agent=args.user_agent,
        db_path=args.db_path,
        raw_dir=args.raw_dir,
        watchlist_file=args.watchlist_file,
        max_rps=args.max_rps,
        latest_poll_seconds=args.latest_poll_seconds,
        watchlist_audit_seconds=args.watchlist_audit_seconds,
        backfill_lookback_days=args.backfill_lookback_days,
        reconcile_poll_seconds=args.reconcile_poll_seconds,
        weekly_repair_cron=args.weekly_repair_cron,
        http_timeout_seconds=args.http_timeout_seconds,
        max_retries=args.max_retries,
        retry_base_seconds=args.retry_base_seconds,
        retry_failed_poll_seconds=args.retry_failed_poll_seconds,
        publish_dir=publish_dir,
        live_workers=args.live_workers,
        live_rps_share=args.live_rps_share,
        metrics_enabled=args.metrics_enabled,
        metrics_host=args.metrics_host,
        metrics_port=args.metrics_port,
    )
    settings.ensure_directories()
    watchlist: list[WatchlistCompany] = []
    if settings.watchlist_file:
        watchlist = load_watchlist_yaml(settings.watchlist_file)
    logger.info("starting EDGAR daemon: db=%s watchlist=%s rps=%.1f",
                settings.db_path, settings.watchlist_file or "(none)", settings.max_rps)
    asyncio.run(_run(settings, watchlist))


if __name__ == "__main__":
    main()