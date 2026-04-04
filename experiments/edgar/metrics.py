"""In-process metrics layer for the EDGAR ingestor daemon.

Lightweight, thread-safe, in-memory instrumentation that exposes
Prometheus-compatible counters, gauges, and histograms.  All writes are
non-blocking and never touch SQLite or the filesystem.

Usage:

    from metrics import METRICS

    # Counters
    METRICS.inc("edgar_filings_discovered_total")
    METRICS.inc("edgar_sec_http_requests_total", labels={"status_class": "2xx", "method": "GET"})

    # Gauges
    METRICS.set_gauge("edgar_live_queue_depth", 42)

    # Histograms (latency)
    METRICS.observe("edgar_sec_http_request_duration_seconds", 0.123)

    # Timestamps
    METRICS.touch("edgar_last_atom_poll_success_unixtime")

    # Exposition
    print(METRICS.expose())  # Prometheus text format
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


# Default histogram buckets (seconds) suitable for HTTP latency
_DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)


def _labels_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Produce a hashable, sorted key from a label dict."""
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _format_labels(key: tuple[tuple[str, str], ...]) -> str:
    """Format label key as Prometheus label string: {k1="v1",k2="v2"}."""
    if not key:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in key)
    return "{" + inner + "}"


class _Counter:
    """Monotonically increasing counter, optionally labelled."""

    __slots__ = ("_lock", "_values")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._values[key] += amount

    def collect(self) -> dict[tuple[tuple[str, str], ...], float]:
        with self._lock:
            return dict(self._values)


class _Gauge:
    """Point-in-time value that can go up or down."""

    __slots__ = ("_lock", "_values")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        self.inc(-amount, labels)

    def collect(self) -> dict[tuple[tuple[str, str], ...], float]:
        with self._lock:
            return dict(self._values)


class _Histogram:
    """Cumulative histogram with configurable bucket boundaries."""

    __slots__ = ("_lock", "_buckets", "_data")

    def __init__(self, buckets: tuple[float, ...] = _DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = buckets
        # Per label-set: {le_boundary: count}, plus _sum and _count
        self._data: dict[
            tuple[tuple[str, str], ...],
            dict[str, float],
        ] = defaultdict(lambda: self._make_empty())

    def _make_empty(self) -> dict[str, float]:
        d: dict[str, float] = {}
        for b in self._buckets:
            d[str(b)] = 0.0
        d["+Inf"] = 0.0
        d["_sum"] = 0.0
        d["_count"] = 0.0
        return d

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            if key not in self._data:
                self._data[key] = self._make_empty()
            d = self._data[key]
            d["_sum"] += value
            d["_count"] += 1.0
            for b in self._buckets:
                if value <= b:
                    d[str(b)] += 1.0
            d["+Inf"] += 1.0

    def collect(
        self,
    ) -> dict[tuple[tuple[str, str], ...], dict[str, float]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


class MetricsRegistry:
    """Central registry holding all metric objects.

    Thread-safe for concurrent writes from asyncio workers running in
    the event loop, and from the HTTP exporter reading on a different
    thread.  All operations are in-memory only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}
        self._enabled: bool = False
        # Startup timestamp for uptime computation
        self._start_time: float = time.time()

    # --- Configuration ---

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # --- Registration (lazy) ---

    def _get_counter(self, name: str) -> _Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = _Counter()
            return self._counters[name]

    def _get_gauge(self, name: str) -> _Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = _Gauge()
            return self._gauges[name]

    def _get_histogram(self, name: str) -> _Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = _Histogram()
            return self._histograms[name]

    # --- Write API (non-blocking, no-op when disabled) ---

    def inc(
        self,
        name: str,
        amount: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Increment a counter."""
        if not self._enabled:
            return
        self._get_counter(name).inc(amount, labels)

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Set a gauge to an absolute value."""
        if not self._enabled:
            return
        self._get_gauge(name).set(value, labels)

    def inc_gauge(
        self,
        name: str,
        amount: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Increment (or decrement) a gauge."""
        if not self._enabled:
            return
        self._get_gauge(name).inc(amount, labels)

    def observe(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Record an observation in a histogram."""
        if not self._enabled:
            return
        self._get_histogram(name).observe(value, labels)

    def touch(self, name: str) -> None:
        """Set a gauge to the current Unix timestamp (for heartbeat metrics)."""
        if not self._enabled:
            return
        self._get_gauge(name).set(time.time())

    # --- Read API (Prometheus text exposition) ---

    def expose(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []

        # Counters
        with self._lock:
            counter_names = sorted(self._counters.keys())
        for name in counter_names:
            counter = self._get_counter(name)
            lines.append(f"# TYPE {name} counter")
            for lk, val in sorted(counter.collect().items()):
                lines.append(f"{name}{_format_labels(lk)} {val}")

        # Gauges
        with self._lock:
            gauge_names = sorted(self._gauges.keys())
        for name in gauge_names:
            gauge = self._get_gauge(name)
            lines.append(f"# TYPE {name} gauge")
            for lk, val in sorted(gauge.collect().items()):
                lines.append(f"{name}{_format_labels(lk)} {val}")

        # Histograms
        with self._lock:
            hist_names = sorted(self._histograms.keys())
        for name in hist_names:
            hist = self._get_histogram(name)
            lines.append(f"# TYPE {name} histogram")
            for lk, buckets_data in sorted(hist.collect().items()):
                base_labels = _format_labels(lk)
                for b in hist._buckets:
                    le_label = f'le="{b}"'
                    if base_labels:
                        combined = base_labels[:-1] + "," + le_label + "}"
                    else:
                        combined = "{" + le_label + "}"
                    lines.append(f"{name}_bucket{combined} {buckets_data[str(b)]}")
                # +Inf bucket
                le_inf = 'le="+Inf"'
                if base_labels:
                    combined = base_labels[:-1] + "," + le_inf + "}"
                else:
                    combined = "{" + le_inf + "}"
                lines.append(f"{name}_bucket{combined} {buckets_data['+Inf']}")
                lines.append(f"{name}_sum{base_labels} {buckets_data['_sum']}")
                lines.append(f"{name}_count{base_labels} {buckets_data['_count']}")

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def health_check(self) -> dict[str, Any]:
        """Return a health summary dict for the /healthz endpoint."""
        uptime = time.time() - self._start_time

        # Collect key gauges for health assessment
        up_gauge = self._get_gauge("edgar_up").collect()
        up = up_gauge.get((), 0.0)

        bootstrap_gauge = self._get_gauge("edgar_bootstrap_complete").collect()
        bootstrap = bootstrap_gauge.get((), 0.0)

        return {
            "status": "ok" if up >= 1.0 else "degraded",
            "uptime_seconds": round(uptime, 1),
            "metrics_enabled": self._enabled,
            "bootstrap_complete": bootstrap >= 1.0,
        }

    # --- Convenience helpers for latency-critical trading metrics ---

    def record_discovery_to_event_latency(self, seconds: float) -> None:
        """Track end-to-end latency from filing discovery to outbox event commit.

        This is the primary SLA metric for the trading pipeline: how quickly
        a new SEC filing becomes a durable, publishable event.
        """
        if not self._enabled:
            return
        self.observe("edgar_discovery_to_event_seconds", seconds)

    def record_event_to_publish_latency(self, seconds: float) -> None:
        """Track latency from outbox event commit to JSONL publication.

        Combined with discovery-to-event, this gives total pipeline latency.
        """
        if not self._enabled:
            return
        self.observe("edgar_event_to_publish_seconds", seconds)

    # --- Archival metrics ---

    def record_archival_run(
        self,
        filings_archived: int,
        filings_failed: int,
        bytes_copied: int,
        elapsed_seconds: float,
    ) -> None:
        """Record summary metrics for a completed archival run."""
        if not self._enabled:
            return
        self.inc("edgar_archival_filings_total", float(filings_archived))
        self.inc("edgar_archival_filings_failed_total", float(filings_failed))
        self.inc("edgar_archival_bytes_total", float(bytes_copied))
        self.observe("edgar_archival_run_duration_seconds", elapsed_seconds)
        self.touch("edgar_last_archival_run_unixtime")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

METRICS = MetricsRegistry()