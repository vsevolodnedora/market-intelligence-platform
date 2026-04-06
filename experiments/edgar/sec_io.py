"""SEC-facing I/O: HTTP transport, rate limiting, Atom/submissions/index
parsers, SGML header parsing, and document extraction.

These pieces are tightly related to SEC document acquisition and
interpretation.  Keeping them together in one module is a balance
between cohesion and file count.
"""

from __future__ import annotations

import asyncio
import gzip
import html
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from typing import Any, Protocol
from urllib.parse import urljoin

from domain import (
    ACCESSION_RE,
    ACTIVIST_FORMS,
    ARCHIVE_LINK_RE,
    ATOM_NS,
    CIK_IN_TITLE_RE,
    DOC_EXT_RE,
    DOC_FIELD_PATTERNS,
    DOCUMENT_RE,
    FALLBACK_CIK_RE,
    FALLBACK_NAME_RE,
    FIELD_PATTERNS,
    HREF_CIK_RE,
    ITEM_INFO_RE,
    LATEST_FILINGS_ATOM_URL,
    SECTION_CIK_RE,
    SECTION_NAME_RE,
    TEXTUAL_PRIMARY_EXTENSIONS,
    TITLE_RE,
    IndexDocumentRow,
    FilingDiscovery,
    FilingParty,
    Settings,
    SubmissionDocument,
    SubmissionHeader,
    _ANGLE_SECTION_RE,
    _COLON_SECTION_RE,
    _DEFAULT_MAX_RETRIES,
    _DEFAULT_RETRY_BASE_SECONDS,
    _DOC_END_RE_B,
    _DOC_START_RE_B,
    _FILENAME_RE_B,
    _INDEX_TABLE_ROW_RE,
    _OWNERSHIP_FORM_RE,
    _RETRYABLE_HTTP_CODES,
    _TEXT_END_RE_B,
    _TEXT_START_RE_B,
    _XML_WRAPPER_END_B,
    _XML_WRAPPER_START_B,
    accession_nodashes,
    derive_archive_base,
    derive_complete_txt_url,
    derive_hdr_sgml_url,
    derive_index_url,
    extract_accession,
    get_logger,
    is_textual_primary_filename,
    looks_like_json_payload,
    normalize_cik,
    normalize_name,
    try_parse_date,
    try_parse_datetime,
    utcnow,
)


logger = get_logger(__name__)

class AsyncTokenBucket:
    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.rate = rate_per_second
        self.capacity = capacity or max(1.0, rate_per_second)
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
                self.updated_at = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                # Compute required wait *inside* the lock, but sleep *outside*.
                deficit = tokens - self.tokens
                wait_seconds = deficit / self.rate
            # Lock is released — other waiters can proceed while we sleep.
            await asyncio.sleep(wait_seconds)



# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class HTTPTransport(Protocol):
    async def request(
        self, method: str, url: str, headers: dict[str, str],
    ) -> tuple[int, bytes, dict[str, str]]: ...


class PooledTransport:
    """Connection-pooled HTTP transport with TLS session reuse.

    Uses **thread-local** connection pools so that each executor thread
    maintains its own ``http.client.HTTPS/HTTPConnection`` per host.
    This is critical because ``http.client`` connections are **not**
    thread-safe — their internal state machine (Idle → Request-sent →
    response-ready) corrupts if two threads interleave ``request()``
    and ``getresponse()`` on the same object.

    Since ``asyncio.to_thread`` dispatches to a reusable thread pool,
    connections are still reused across requests within the same thread,
    giving us both connection reuse *and* zero-contention correctness.

    The SSL context is shared (it *is* thread-safe) so TLS session
    tickets are reused across all threads.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        max_reuses: int = 100,
    ) -> None:
        import http.client
        import threading
        from urllib.parse import urlparse as _urlparse

        self.timeout = timeout
        self.max_reuses = max_reuses
        self._ssl_ctx = ssl.create_default_context()
        self._local = threading.local()
        self._http_client = http.client
        self._urlparse = _urlparse

    def _thread_pool(self) -> dict[str, tuple[Any, int]]:
        """Return the per-thread connection pool, creating it if needed."""
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = {}
            self._local.pool = pool
        return pool

    def _get_conn(self, host: str, port: int, scheme: str) -> Any:
        """Get or create a persistent connection for this thread + host."""
        pool = self._thread_pool()
        key = f"{scheme}://{host}:{port}"

        entry = pool.get(key)
        if entry is not None:
            conn, reuses = entry
            if reuses < self.max_reuses:
                pool[key] = (conn, reuses + 1)
                return conn
            # Exceeded reuse limit — close and create fresh
            try:
                conn.close()
            except Exception:
                pass

        if scheme == "https":
            conn = self._http_client.HTTPSConnection(
                host, port, timeout=self.timeout, context=self._ssl_ctx,
            )
        else:
            conn = self._http_client.HTTPConnection(
                host, port, timeout=self.timeout,
            )
        pool[key] = (conn, 1)
        return conn

    def _evict_conn(self, host: str, port: int, scheme: str) -> None:
        """Remove a stale connection from this thread's pool."""
        pool = self._thread_pool()
        key = f"{scheme}://{host}:{port}"
        entry = pool.pop(key, None)
        if entry:
            try:
                entry[0].close()
            except Exception:
                pass

    def _do_request_sync(
        self, method: str, url: str, headers: dict[str, str],
    ) -> tuple[int, bytes, dict[str, str]]:
        parsed = self._urlparse(url)
        scheme = parsed.scheme or "https"
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        for attempt in range(2):  # retry once on stale connection
            conn = self._get_conn(host, port, scheme)
            try:
                conn.request(method, path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                # Decompress here on the worker thread so large gzipped
                # SEC payloads don't stall the event loop.
                encoding = resp_headers.get("content-encoding", "").lower()
                if encoding == "gzip":
                    body = gzip.decompress(body)
                elif encoding == "deflate":
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        body = zlib.decompress(body, -zlib.MAX_WBITS)
                return resp.status, body, resp_headers
            except (
                ConnectionError, OSError, self._http_client.HTTPException,
            ):
                self._evict_conn(host, port, scheme)
                if attempt == 0:
                    continue  # transparent retry on stale connection
                raise

        raise IOError(f"Failed to connect to {host}:{port}")

    def close(self) -> None:
        """Explicitly close all pooled connections."""
        pool = getattr(self._local, "pool", None)
        if pool:
            for key, (conn, _) in list(pool.items()):
                try:
                    conn.close()
                except Exception:
                    pass
            pool.clear()

    async def request(
        self, method: str, url: str, headers: dict[str, str],
    ) -> tuple[int, bytes, dict[str, str]]:
        # Decompression is handled inside _do_request_sync on the worker
        # thread so the event loop is never stalled by CPU-bound gzip work.
        return await asyncio.to_thread(
            self._do_request_sync, method, url, headers,
        )


class UrllibTransport:
    """Legacy per-request transport.  Retained for backward compatibility.

    For lower-latency operation, prefer ``PooledTransport`` which reuses
    TCP connections and TLS sessions across requests.
    """
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def request(
        self, method: str, url: str, headers: dict[str, str],
    ) -> tuple[int, bytes, dict[str, str]]:
        req = urllib.request.Request(url, method=method, headers=headers)
        ctx = ssl.create_default_context()
        loop = asyncio.get_running_loop()

        def _do_request() -> tuple[int, bytes, dict[str, str]]:
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout, context=ctx)
                body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, body, resp_headers
            except urllib.error.HTTPError as exc:
                body = exc.read() if exc.fp else b""
                resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
                return exc.code, body, resp_headers

        status, body, resp_headers = await loop.run_in_executor(None, _do_request)
        encoding = resp_headers.get("content-encoding", "").lower()
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        return status, body, resp_headers


class MockTransport:
    """In-memory transport for deterministic testing."""
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def request(
        self, method: str, url: str, headers: dict[str, str],
    ) -> tuple[int, bytes, dict[str, str]]:
        return self._handler(method, url, headers)



class SECHTTPError(IOError):
    def __init__(self, status: int, url: str) -> None:
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} for {url}")

    @property
    def is_retryable(self) -> bool:
        return self.status in _RETRYABLE_HTTP_CODES


class SECResponseFormatError(IOError):
    def __init__(self, url: str, message: str) -> None:
        self.url = url
        self.message = message
        super().__init__(f"Invalid response from {url}: {message}")



class SECClient:
    def __init__(
        self, settings: Settings, *,
        transport: HTTPTransport | None = None,
        rate_limiter: AsyncTokenBucket | None = None,
    ) -> None:
        self.settings = settings
        self.rate_limiter = rate_limiter or AsyncTokenBucket(settings.max_rps)
        self._transport = transport or PooledTransport(timeout=settings.http_timeout_seconds)
        self._headers = {
            "User-Agent": settings.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        }
        self._max_retries = settings.max_retries
        self._retry_base = settings.retry_base_seconds

    async def aclose(self) -> None:
        """Close the underlying transport's connection pool.

        For PooledTransport, this explicitly closes all pooled TCP/TLS
        connections.  Without this, connections leak until GC.
        """
        transport = self._transport
        if hasattr(transport, "close"):
            # PooledTransport.close() is synchronous but fast
            await asyncio.get_running_loop().run_in_executor(None, transport.close)

    async def __aenter__(self) -> SECClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def get_bytes(self, url: str) -> tuple[bytes, str | None]:
        from metrics import METRICS

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            await self.rate_limiter.acquire()
            t0 = time.monotonic()
            try:
                status, body, rh = await self._transport.request("GET", url, self._headers)
            except (OSError, asyncio.TimeoutError) as exc:
                elapsed = time.monotonic() - t0
                METRICS.observe("edgar_sec_http_request_duration_seconds", elapsed,
                                labels={"method": "GET", "status_class": "err"})
                METRICS.inc("edgar_sec_http_requests_total",
                            labels={"status_class": "err", "method": "GET"})
                last_exc = exc
                if attempt < self._max_retries:
                    METRICS.inc("edgar_sec_http_retries_total")
                    backoff = self._retry_base * (2 ** (attempt - 1))
                    logger.warning(
                        "network error fetching %s (attempt %d/%d): %s — retrying in %.1fs",
                        url, attempt, self._max_retries, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.warning(
                        "network error fetching %s (attempt %d/%d, final): %s",
                        url, attempt, self._max_retries, exc,
                    )
                continue

            elapsed = time.monotonic() - t0
            status_class = f"{status // 100}xx"
            METRICS.observe("edgar_sec_http_request_duration_seconds", elapsed,
                            labels={"method": "GET", "status_class": status_class})
            METRICS.inc("edgar_sec_http_requests_total",
                        labels={"status_class": status_class, "method": "GET"})

            if status < 400:
                return body, rh.get("content-type")
            err = SECHTTPError(status, url)
            if not err.is_retryable:
                raise err
            last_exc = err
            if attempt < self._max_retries:
                METRICS.inc("edgar_sec_http_retries_total")
                backoff = self._retry_base * (2 ** (attempt - 1))
                if status == 429:
                    METRICS.inc("edgar_sec_http_429_total")
                    try:
                        retry_after = float(rh.get("retry-after", "0"))
                        backoff = max(backoff, retry_after)
                    except (ValueError, TypeError):
                        pass
                logger.warning(
                    "HTTP %d from %s (attempt %d/%d) — retrying in %.1fs",
                    status, url, attempt, self._max_retries, backoff,
                )
                await asyncio.sleep(backoff)
            else:
                if status == 429:
                    METRICS.inc("edgar_sec_http_429_total")
                logger.warning(
                    "HTTP %d from %s (attempt %d/%d, final)",
                    status, url, attempt, self._max_retries,
                )
        raise last_exc or IOError(f"Failed to fetch {url} after {self._max_retries} attempts")

    async def get_text(self, url: str, encoding: str | None = None) -> str:
        data, _ = await self.get_bytes(url)
        if encoding:
            return data.decode(encoding, errors="replace")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="replace")

    async def get_json(self, url: str) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            data, content_type = await self.get_bytes(url)
            text = data.decode("utf-8", errors="replace")
            try:
                if not text.strip():
                    raise SECResponseFormatError(url, "empty response body")
                if content_type and "json" not in content_type.lower() and not looks_like_json_payload(text):
                    raise SECResponseFormatError(url, f"unexpected content-type={content_type!r}")
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise SECResponseFormatError(url, f"JSON root must be object, got {type(payload).__name__}")
                return payload
            except (json.JSONDecodeError, SECResponseFormatError) as exc:
                last_exc = exc
                backoff = self._retry_base * (2 ** (attempt - 1))
                logger.warning("invalid JSON from %s (attempt %d/%d): %s", url, attempt, self._max_retries, exc)
                await asyncio.sleep(backoff)
        raise last_exc or SECResponseFormatError(url, "invalid JSON payload")



# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_atom_title(title: str) -> tuple[str, str, str | None]:
    form_type, company_name, cik = "", title, None
    m = TITLE_RE.match(title)
    if m:
        form_type = m.group("form").strip()
        company_name = m.group("rest").strip()
    cm = CIK_IN_TITLE_RE.search(company_name)
    if cm:
        company_name = cm.group("company").strip()
        cik = normalize_cik(cm.group("cik"))
    return form_type, company_name, cik


def _extract_field(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_atom_entry(entry: ET.Element) -> FilingDiscovery | None:
    title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
    updated = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)
    summary = entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or ""
    link_el = entry.find("atom:link", ATOM_NS)
    href = link_el.attrib.get("href") if link_el is not None else None
    cat_el = entry.find("atom:category", ATOM_NS)
    category_term = cat_el.attrib.get("term", "") if cat_el is not None else ""

    form_type, company_name, cik = _parse_atom_title(title)
    form_type = category_term or form_type
    accession = extract_accession(" ".join([title, summary, href or ""]))

    if not cik and href:
        m = HREF_CIK_RE.search(href)
        if m:
            cik = normalize_cik(m.group("cik"))

    filing_date_str = _extract_field(summary, r"Filed:?\s*(\d{4}-\d{2}-\d{2}|\d{8})")
    accepted = try_parse_datetime(updated) or try_parse_datetime(
        _extract_field(summary, r"Accepted:?\s*([^<\n]+)")
    )

    if not accession or not cik:
        return None

    return FilingDiscovery(
        accession_number=accession, archive_cik=cik,
        company_name=company_name or "UNKNOWN",
        form_type=form_type or "UNKNOWN",
        filing_date=try_parse_date(filing_date_str),
        accepted_at=accepted, discovered_at=utcnow(),
        source="latest_filings_atom", filing_href=href,
        filing_index_url=derive_index_url(cik, accession),
        complete_txt_url=derive_complete_txt_url(cik, accession),
        hdr_sgml_url=derive_hdr_sgml_url(cik, accession),
        metadata={"title": title, "summary": html.unescape(summary)},
    )


def parse_latest_filings_atom(xml_text: str) -> list[FilingDiscovery]:
    root = ET.fromstring(xml_text)
    results: list[FilingDiscovery] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        d = _parse_atom_entry(entry)
        if d is not None:
            results.append(d)
    logger.info("parsed %d discoveries from Atom feed", len(results))
    return results


def filter_by_forms(
    discoveries: list[FilingDiscovery], allowed: tuple[str, ...],
) -> list[FilingDiscovery]:
    s = {f.upper() for f in allowed}
    return [d for d in discoveries if d.form_type.upper() in s]


def _parse_submissions_recent(
    recent: dict[str, Any], cik: str, company_name: str,
) -> list[FilingDiscovery]:
    accessions = recent.get("accessionNumber", []) or []
    forms = recent.get("form", []) or []
    filing_dates = recent.get("filingDate", []) or []
    accept_dts = recent.get("acceptanceDateTime", []) or []
    primary_docs = recent.get("primaryDocument", []) or []

    results: list[FilingDiscovery] = []
    for idx, acc in enumerate(accessions):
        acc = str(acc)
        form = str(forms[idx]) if idx < len(forms) else "UNKNOWN"
        fd = filing_dates[idx] if idx < len(filing_dates) else None
        adt = accept_dts[idx] if idx < len(accept_dts) else None
        pdoc = primary_docs[idx] if idx < len(primary_docs) else None
        results.append(FilingDiscovery(
            accession_number=acc, archive_cik=cik, company_name=company_name,
            form_type=form,
            filing_date=try_parse_date(str(fd) if fd else None),
            accepted_at=try_parse_datetime(str(adt) if adt else None),
            discovered_at=utcnow(), source="submissions_json",
            filing_index_url=derive_index_url(cik, acc),
            complete_txt_url=derive_complete_txt_url(cik, acc),
            hdr_sgml_url=derive_hdr_sgml_url(cik, acc),
            primary_document_url=(
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{accession_nodashes(acc)}/{pdoc}"
                if pdoc else None
            ),
            metadata={"primary_document": pdoc},
        ))
    return results


def parse_submissions_json(payload: dict[str, Any]) -> list[FilingDiscovery]:
    cik = normalize_cik(str(payload.get("cik", "0")))
    company_name = str(payload.get("name", "UNKNOWN"))
    recent = ((payload.get("filings") or {}).get("recent")) or {}
    results = _parse_submissions_recent(recent, cik, company_name)
    logger.info("parsed %d filings from submissions JSON for CIK %s (%s)", len(results), cik, company_name)
    return results


def extract_submissions_rollover_urls(payload: dict[str, Any]) -> list[str]:
    cik = normalize_cik(str(payload.get("cik", "0")))
    files_list = ((payload.get("filings") or {}).get("files")) or []
    urls: list[str] = []
    for entry in files_list:
        name = entry.get("name") if isinstance(entry, dict) else str(entry)
        if name:
            urls.append(f"https://data.sec.gov/submissions/{name}")
    if urls:
        logger.info("found %d rollover submission files for CIK %s", len(urls), cik)
    return urls


def parse_submissions_rollover_json(
    payload: dict[str, Any], cik: str, company_name: str,
) -> list[FilingDiscovery]:
    results = _parse_submissions_recent(payload, cik, company_name)
    logger.info("parsed %d filings from rollover JSON for CIK %s (%s)", len(results), cik, company_name)
    return results


def parse_company_idx(text: str) -> list[FilingDiscovery]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("CIK|"):
            start = i + 1
            break
    results: list[FilingDiscovery] = []
    for line in lines[start:]:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik_raw, company_name, form_type, filing_date, filename = parts
        acc = extract_accession(filename)
        if not acc:
            continue
        cik = str(cik_raw).zfill(10)
        results.append(FilingDiscovery(
            accession_number=acc, archive_cik=cik,
            company_name=company_name, form_type=form_type,
            filing_date=try_parse_date(filing_date), discovered_at=utcnow(),
            source="daily_index_reconciliation",
            filing_href=f"https://www.sec.gov/Archives/{filename}",
            hdr_sgml_url=derive_hdr_sgml_url(cik, acc),
            metadata={"filename": filename},
        ))
    return results


def extract_archive_links(index_html: str, index_url: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for m in ARCHIVE_LINK_RE.finditer(index_html):
        href = html.unescape(m.group("href")).strip()
        absolute = urljoin(index_url, href)
        if "/Archives/" in absolute and DOC_EXT_RE.search(absolute) and absolute not in seen:
            links.append(absolute)
            seen.add(absolute)
    return links


def parse_index_document_rows(index_html: str) -> list[IndexDocumentRow]:
    rows: list[IndexDocumentRow] = []
    for m in _INDEX_TABLE_ROW_RE.finditer(index_html):
        seq = (m.group("seq") or "").strip() or None
        desc = (m.group("desc") or "").strip() or None
        href = (m.group("href") or "").strip() or None
        filename = (m.group("filename") or "").strip() or None
        doc_type = (m.group("doc_type") or "").strip() or None
        size = (m.group("size") or "").strip() or None
        if seq and html.unescape(seq).strip() == "":
            seq = None
        if doc_type and html.unescape(doc_type).strip() == "":
            doc_type = None
        if not seq and not doc_type:
            continue
        rows.append(IndexDocumentRow(
            sequence=seq, description=desc, href=href,
            filename=filename, doc_type=doc_type, size=size,
        ))
    return rows


def choose_primary_document(
    index_html: str, index_url: str, form_type: str | None = None,
) -> str | None:
    doc_rows = parse_index_document_rows(index_html)
    links = extract_archive_links(index_html, index_url)

    url_to_row: dict[str, IndexDocumentRow] = {}
    for row in doc_rows:
        if row.href:
            resolved = urljoin(index_url, html.unescape(row.href))
            url_to_row[resolved] = row

    form_type_upper = (form_type or "").upper().strip()

    type_matched: list[str] = []
    for link in links:
        row = url_to_row.get(link)
        if row and row.doc_type and form_type_upper:
            if row.doc_type.upper().strip() == form_type_upper:
                type_matched.append(link)

    if type_matched:
        for link in type_matched:
            if "/xsl" not in link.lower():
                return link
        return type_matched[0]

    non_exhibit_links: list[str] = []
    for link in links:
        row = url_to_row.get(link)
        if row and row.doc_type:
            dt = row.doc_type.upper().strip()
            if dt.startswith("EX-") or dt == "GRAPHIC":
                continue
        lower = link.lower()
        if lower.endswith("-index.html"):
            continue
        non_exhibit_links.append(link)

    if form_type_upper and _OWNERSHIP_FORM_RE.fullmatch(form_type_upper):
        for link in non_exhibit_links:
            if link.lower().endswith(".xml"):
                return link

    for link in non_exhibit_links:
        if link.lower().endswith((".htm", ".html")):
            return link
    for link in non_exhibit_links:
        if link.lower().endswith(".xml"):
            return link
    for link in non_exhibit_links:
        if link.lower().endswith(".txt"):
            return link
    return non_exhibit_links[0] if non_exhibit_links else (links[0] if links else None)


def choose_primary_document_filename(
    index_html: str, index_url: str, form_type: str | None = None,
) -> str | None:
    url = choose_primary_document(index_html, index_url, form_type=form_type)
    if url:
        return url.rsplit("/", 1)[-1]
    return None


def choose_primary_document_from_header(
    header: SubmissionHeader, preferred_filename: str | None = None,
) -> SubmissionDocument | None:
    docs = [doc for doc in header.documents if doc.filename]
    if not docs:
        return None
    if preferred_filename:
        for doc in docs:
            if doc.filename and doc.filename.lower() == preferred_filename.lower():
                return doc
    form_type = (header.form_type or "").upper().strip()
    for doc in docs:
        doc_type = (doc.doc_type or "").upper().strip()
        if form_type and doc_type == form_type and is_textual_primary_filename(doc.filename):
            return doc
    for doc in docs:
        if (doc.sequence or "").strip().lstrip("0") in {"", "1"} and is_textual_primary_filename(doc.filename):
            return doc
    for doc in docs:
        if is_textual_primary_filename(doc.filename):
            return doc
    return docs[0]


def extract_primary_document_bytes(raw_submission: bytes, target_filename: str) -> bytes | None:
    if not is_textual_primary_filename(target_filename):
        return None
    target_lower = target_filename.lower().encode("ascii", errors="replace")
    pos = 0
    while pos < len(raw_submission):
        doc_start_m = _DOC_START_RE_B.search(raw_submission, pos)
        if not doc_start_m:
            break
        doc_end_m = _DOC_END_RE_B.search(raw_submission, doc_start_m.end())
        if not doc_end_m:
            break
        block = raw_submission[doc_start_m.end():doc_end_m.start()]
        pos = doc_end_m.end()
        fn_m = _FILENAME_RE_B.search(block)
        if not fn_m:
            continue
        fn = fn_m.group(1).strip()
        if fn.lower() != target_lower:
            continue
        text_start = _TEXT_START_RE_B.search(block)
        text_end = _TEXT_END_RE_B.search(block)
        if not text_start or not text_end:
            continue
        body = block[text_start.end():text_end.start()]
        body = _XML_WRAPPER_START_B.sub(b"", body, count=1)
        body = _XML_WRAPPER_END_B.sub(b"", body, count=1)
        return body.strip()
    return None


def _parse_sgml_party_section(role_tag: str, body: str) -> FilingParty:
    role = role_tag.lower().replace(" ", "-").strip()
    name_match = SECTION_NAME_RE.search(body)
    cik_match = SECTION_CIK_RE.search(body)
    name = name_match.group(1).strip() if name_match else None
    cik = cik_match.group(1).strip().zfill(10) if cik_match else None
    return FilingParty(role=role, cik=cik, name=name)


def parse_submission_text(text: str) -> SubmissionHeader:
    data: dict[str, str | None] = {}
    for key, pat in FIELD_PATTERNS.items():
        m = pat.search(text)
        data[key] = m.group(1).strip() if m else None

    parties: list[FilingParty] = []
    for m in _COLON_SECTION_RE.finditer(text):
        role_tag = m.group("role").strip()
        body = m.group("body")
        party = _parse_sgml_party_section(role_tag, body)
        if party.cik or party.name:
            parties.append(party)

    if not parties:
        for m in _ANGLE_SECTION_RE.finditer(text):
            role_tag = m.group("role").strip()
            body = m.group("body")
            party = _parse_sgml_party_section(role_tag, body)
            if party.cik or party.name:
                parties.append(party)

    if not parties:
        fb_name = FALLBACK_NAME_RE.search(text)
        fb_cik = FALLBACK_CIK_RE.search(text)
        if fb_name or fb_cik:
            parties.append(FilingParty(
                role="filer",
                cik=fb_cik.group(1).strip().zfill(10) if fb_cik else None,
                name=fb_name.group(1).strip() if fb_name else None,
            ))

    docs: list[SubmissionDocument] = []
    for raw in DOCUMENT_RE.findall(text):
        d: dict[str, str | None] = {}
        for fn, fp in DOC_FIELD_PATTERNS.items():
            m = fp.search(raw)
            d[fn] = m.group(1).strip() if m else None
        docs.append(SubmissionDocument(
            sequence=d.get("sequence"), filename=d.get("filename"),
            description=d.get("description"), doc_type=d.get("type"),
        ))

    items = [s.strip() for s in ITEM_INFO_RE.findall(text)]

    return SubmissionHeader(
        form_type=data.get("form_type"),
        acceptance_datetime=data.get("acceptance_datetime"),
        filed_as_of_date=data.get("filed_as_of_date"),
        parties=parties, documents=docs, item_information=items,
    )


def normalized_header_metadata(header: SubmissionHeader) -> dict[str, object]:
    return {
        "acceptance_datetime": (
            try_parse_datetime(header.acceptance_datetime).isoformat()
            if header.acceptance_datetime else None
        ),
        "form_type": header.form_type,
        "filed_as_of_date": (
            try_parse_date(header.filed_as_of_date).isoformat()
            if header.filed_as_of_date else None
        ),
        "item_information": header.item_information,
        "parties": [
            {"role": p.role, "cik": p.cik, "name": p.name,
             "name_normalized": p.name_normalized}
            for p in header.parties
        ],
        "documents": [
            {"sequence": d.sequence, "filename": d.filename,
             "description": d.description, "doc_type": d.doc_type}
            for d in header.documents
        ],
    }