"""Core types, parsing, storage, and HTTP infrastructure for the EDGAR ingestor.

This module owns ALL shared contracts AND implementations: data models, SEC SGML
parsing, Atom/submissions/index parsers, URL derivation, rate limiting, HTTP
client, SQLite storage, structured Form 4 / 8-K extraction, and the watchlist
index.  Orchestration logic belongs in edgar_daemon.py.

Carried forward from the working prototype (stream_sec_edgar.py) with:
  - RelevanceState / RetrievalStatus enums for the header-gate lifecycle
  - archive_cik vs issuer_cik distinction
  - FeedWatermark as a first-class type
  - WatchlistIndex with CIK and normalized-name lookup
  - HeaderResolver for conservative ambiguous-form matching
  - hdr.sgml URL derivation
  - Expanded default forms (20-F, 40-F families)
  - Checkpoint payloads as typed JSON blobs
  - Retry metadata (attempt_count, next_retry_at) on FilingRecord

Dependencies: Python >= 3.11, PyYAML.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import logging
import mimetypes
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import urljoin

import yaml


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s [%(funcName)s] %(message)s"
        ))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants and compiled patterns
# ---------------------------------------------------------------------------

ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
TITLE_RE = re.compile(r"^(?P<form>[A-Z0-9\-/]+)\s+-\s+(?P<rest>.+)$")
CIK_IN_TITLE_RE = re.compile(r"(?P<company>.*?)\s*\((?P<cik>\d{1,10})\)")
HREF_CIK_RE = re.compile(r"/data/(?P<cik>\d{1,10})/")
ARCHIVE_LINK_RE = re.compile(r'href=["\'](?P<href>[^"\']+)["\']', re.IGNORECASE)
DOC_EXT_RE = re.compile(r"\.(?:htm|html|txt|xml|xsd|xsl|pdf|jpg|jpeg|png)$", re.IGNORECASE)
DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.DOTALL | re.IGNORECASE)
ITEM_INFO_RE = re.compile(r"ITEM INFORMATION:\s*(.+)")
DOC_TEXT_BODY_RE = re.compile(r"<TEXT>(.*?)</TEXT>", re.DOTALL | re.IGNORECASE)

TEXTUAL_PRIMARY_EXTENSIONS = (".htm", ".html", ".txt", ".xml", ".xsd", ".xsl")
BINARY_PRIMARY_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")

_INDEX_TABLE_ROW_RE = re.compile(
    r"<tr[^>]*>\s*"
    r"<td[^>]*>\s*(?P<seq>[^<]*?)\s*</td>\s*"
    r"<td[^>]*>\s*(?P<desc>[^<]*?)\s*</td>\s*"
    r"<td[^>]*>\s*(?:<a[^>]+href=[\"'](?P<href>[^\"']+)[\"'][^>]*>)?\s*(?P<filename>[^<]*?)\s*(?:</a>)?\s*</td>\s*"
    r"<td[^>]*>\s*(?P<doc_type>[^<]*?)\s*</td>\s*"
    r"<td[^>]*>\s*(?P<size>[^<]*?)\s*</td>",
    re.DOTALL | re.IGNORECASE,
)

# --- SGML header patterns ---
_PARTY_ROLES_PATTERN = r"FILER|ISSUER|REPORTING-OWNER|SUBJECT\s+COMPANY|FILED-BY"
_COLON_SECTION_RE = re.compile(
    r"^(?P<role>" + _PARTY_ROLES_PATTERN + r"):\s*$"
    r"(?P<body>.*?)"
    r"(?=^(?:" + _PARTY_ROLES_PATTERN + r"):\s*$"
    r"|<(?:DOCUMENT|/SEC-HEADER)>"
    r"|\Z)",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)
_ANGLE_SECTION_RE = re.compile(
    r"<(?P<role>" + _PARTY_ROLES_PATTERN + r")>"
    r"(?P<body>.*?)"
    r"(?=<(?:" + _PARTY_ROLES_PATTERN + r"|DOCUMENT|/SEC-HEADER)>"
    r"|\Z)",
    re.DOTALL | re.IGNORECASE,
)
SECTION_NAME_RE = re.compile(r"COMPANY CONFORMED NAME:\s*(.+)")
SECTION_CIK_RE = re.compile(r"CENTRAL INDEX KEY:\s*(\d{1,10})")
FALLBACK_NAME_RE = re.compile(r"COMPANY CONFORMED NAME:\s*(.+)")
FALLBACK_CIK_RE = re.compile(r"CENTRAL INDEX KEY:\s*(\d{1,10})")

FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "acceptance_datetime": re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})", re.IGNORECASE),
    "form_type": re.compile(r"CONFORMED SUBMISSION TYPE:\s*([^\n]+)"),
    "filed_as_of_date": re.compile(r"FILED AS OF DATE:\s*(\d{8})"),
}

DOC_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "sequence": re.compile(r"<SEQUENCE>([^\n<]+)", re.IGNORECASE),
    "filename": re.compile(r"<FILENAME>([^\n<]+)", re.IGNORECASE),
    "description": re.compile(r"<DESCRIPTION>([^\n<]+)", re.IGNORECASE),
    "type": re.compile(r"<TYPE>([^\n<]+)", re.IGNORECASE),
}

# Byte-level patterns for document extraction.
_DOC_START_RE_B = re.compile(rb"<DOCUMENT>", re.IGNORECASE)
_DOC_END_RE_B = re.compile(rb"</DOCUMENT>", re.IGNORECASE)
_FILENAME_RE_B = re.compile(rb"<FILENAME>([^\n\r<]+)", re.IGNORECASE)
_TEXT_START_RE_B = re.compile(rb"<TEXT>", re.IGNORECASE)
_TEXT_END_RE_B = re.compile(rb"</TEXT>", re.IGNORECASE)
_XML_WRAPPER_START_B = re.compile(rb"^\s*<(?:XBRL|XML)[^>]*>", re.IGNORECASE)
_XML_WRAPPER_END_B = re.compile(rb"</(?:XBRL|XML)>\s*$", re.IGNORECASE)

OWNERSHIP_FORMS = frozenset({"3", "4", "5", "3/A", "4/A", "5/A"})
_OWNERSHIP_FORM_RE = re.compile(r"^(?:3|4|5)(?:/A)?$", re.IGNORECASE)
ACTIVIST_FORMS = frozenset({"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "13D", "13G"})

# --- Form taxonomy (expanded per migration plan) ---

DEFAULT_DIRECT_FORMS: tuple[str, ...] = (
    "8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A",
    "6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
)
DEFAULT_AMBIGUOUS_FORMS: tuple[str, ...] = (
    "3", "3/A", "4", "4/A", "5", "5/A",
    "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
)
DEFAULT_ALL_FORMS: tuple[str, ...] = DEFAULT_DIRECT_FORMS + DEFAULT_AMBIGUOUS_FORMS

_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BASE_SECONDS = 2.0

LATEST_FILINGS_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&company=&dateb=&owner=include&start={start}&count={count}&output=atom"
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RelevanceState(str, Enum):
    UNKNOWN = "unknown"
    DIRECT_MATCH = "direct_match"
    DIRECT_UNMATCHED = "direct_unmatched"
    HDR_PENDING = "hdr_pending"
    HDR_MATCH = "hdr_match"
    IRRELEVANT = "irrelevant"
    HDR_FAILED = "hdr_failed"              # Permanent: unparseable header
    HDR_TRANSIENT_FAIL = "hdr_transient_fail"
    UNRESOLVED = "unresolved"


class RetrievalStatus(str, Enum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    RETRIEVED_PARTIAL = "retrieved_partial"
    RETRIEVED = "retrieved"
    RETRIEVAL_FAILED = "retrieval_failed"


class FilingPriority(str, Enum):
    LIVE = "live"
    RETRY = "retry"
    HEADER_GATE = "header_gate"
    RETRIEVAL = "retrieval"
    AUDIT = "audit"
    BACKFILL = "backfill"
    REPAIR = "repair"


# ---------------------------------------------------------------------------
# Derived enum sets (must follow enum definitions)
# ---------------------------------------------------------------------------

# States that are considered terminal — no further discovery re-processing needed.
_TERMINAL_RELEVANCE_STATES = frozenset({
    RelevanceState.DIRECT_MATCH.value,
    RelevanceState.DIRECT_UNMATCHED.value,
    RelevanceState.HDR_MATCH.value,
    RelevanceState.IRRELEVANT.value,
    RelevanceState.HDR_FAILED.value,
    RelevanceState.UNRESOLVED.value,
})

# Retrieval states that are considered terminal — do not retry.
_TERMINAL_RETRIEVAL_STATUSES = frozenset({
    RetrievalStatus.RETRIEVED.value,
})

# Retrieval states that are actually retryable.
_RETRYABLE_RETRIEVAL_STATUSES = frozenset({
    RetrievalStatus.RETRIEVAL_FAILED.value,
    RetrievalStatus.RETRIEVED_PARTIAL.value,
})

# Maximum filing-level retry attempts before giving up.
MAX_FILING_RETRY_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

def _validate_accession(v: str) -> str:
    if not ACCESSION_RE.fullmatch(v):
        raise ValueError(f"Invalid accession number format: {v!r}")
    return v


def _validate_cik(v: str) -> str:
    stripped = v.lstrip("0") or "0"
    if not stripped.isdigit():
        raise ValueError(f"Invalid CIK: {v!r}")
    return v.zfill(10)


def normalize_name(name: str) -> str:
    """Stable comparison key: uppercase, collapsed whitespace, stripped punctuation."""
    s = name.upper().strip()
    s = re.sub(r"[.,;:!?'\"-]+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


@dataclass(slots=True)
class Settings:
    """Fully-resolved runtime configuration."""
    user_agent: str
    db_path: Path
    raw_dir: Path
    watchlist_file: Path | None
    max_rps: float
    latest_poll_seconds: int
    watchlist_audit_seconds: int
    backfill_lookback_days: int
    reconcile_poll_seconds: int
    weekly_repair_cron: str
    http_timeout_seconds: float
    direct_forms: tuple[str, ...] = DEFAULT_DIRECT_FORMS
    ambiguous_forms: tuple[str, ...] = DEFAULT_AMBIGUOUS_FORMS
    max_retries: int = _DEFAULT_MAX_RETRIES
    retry_base_seconds: float = _DEFAULT_RETRY_BASE_SECONDS
    retry_failed_poll_seconds: int = 300
    publish_dir: Path | None = None  # Dir for JSON-lines event publishing
    live_workers: int = 3  # Number of concurrent live-lane consumers
    live_rps_share: float = 0.6  # Fraction of max_rps reserved for live lane
    # --- Metrics (optional observability layer) ---
    metrics_enabled: bool = False
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9108

    def __post_init__(self) -> None:
        if not self.user_agent:
            raise ValueError("user_agent is required")
        if self.max_rps <= 0:
            raise ValueError("max_rps must be positive")
        if self.max_rps > 10:
            raise ValueError(
                f"max_rps={self.max_rps} exceeds SEC's documented 10 req/s ceiling"
            )
        if not (0.1 <= self.live_rps_share <= 0.9):
            raise ValueError(
                f"live_rps_share={self.live_rps_share} must be between 0.1 and 0.9"
            )

    @property
    def all_forms(self) -> tuple[str, ...]:
        return self.direct_forms + self.ambiguous_forms

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        if self.publish_dir:
            self.publish_dir.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class WatchlistCompany:
    cik: str
    ticker: str
    name: str
    name_normalized: str = ""
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cik = _validate_cik(str(self.cik))
        if not self.name_normalized:
            self.name_normalized = normalize_name(self.name)


@dataclass(slots=True)
class FilingDiscovery:
    """Candidate filing discovered from any SEC source, before resolution."""
    accession_number: str
    archive_cik: str
    form_type: str = "UNKNOWN"
    company_name: str = "UNKNOWN"
    filing_date: date | None = None
    accepted_at: datetime | None = None
    discovered_at: datetime | None = None
    source: str = "unknown"
    filing_href: str | None = None
    filing_index_url: str | None = None
    complete_txt_url: str | None = None
    hdr_sgml_url: str | None = None
    primary_document_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.accession_number = _validate_accession(self.accession_number)
        self.archive_cik = _validate_cik(self.archive_cik)


@dataclass(slots=True)
class FilingParty:
    role: str
    cik: str | None = None
    name: str | None = None
    name_normalized: str | None = None

    def __post_init__(self) -> None:
        if self.name and not self.name_normalized:
            self.name_normalized = normalize_name(self.name)


@dataclass(slots=True)
class SubmissionDocument:
    sequence: str | None = None
    filename: str | None = None
    description: str | None = None
    doc_type: str | None = None


@dataclass(slots=True)
class SubmissionHeader:
    form_type: str | None = None
    acceptance_datetime: str | None = None
    filed_as_of_date: str | None = None
    parties: list[FilingParty] = field(default_factory=list)
    documents: list[SubmissionDocument] = field(default_factory=list)
    item_information: list[str] = field(default_factory=list)

    @property
    def filer(self) -> FilingParty | None:
        return next((p for p in self.parties if p.role == "filer"), None)

    @property
    def issuer(self) -> FilingParty | None:
        return next((p for p in self.parties if p.role == "issuer"), None)

    @property
    def reporting_owners(self) -> list[FilingParty]:
        return [p for p in self.parties if p.role == "reporting-owner"]

    @property
    def subject_company(self) -> FilingParty | None:
        return next((p for p in self.parties if p.role == "subject-company"), None)

    def canonical_issuer(self) -> FilingParty | None:
        ft = (self.form_type or "").upper().strip()
        if _OWNERSHIP_FORM_RE.fullmatch(ft):
            return self.issuer or self.filer
        if ft in ACTIVIST_FORMS:
            return self.subject_company  # No filer fallback for 13D/G
        return self.filer

    # Legacy compat
    @property
    def company_name(self) -> str | None:
        p = self.canonical_issuer()
        return p.name if p else (self.parties[0].name if self.parties else None)

    @property
    def cik(self) -> str | None:
        p = self.canonical_issuer()
        return p.cik if p else (self.parties[0].cik if self.parties else None)


@dataclass(slots=True)
class FilingArtifact:
    accession_number: str
    artifact_type: str
    source_url: str
    local_path: Path
    sha256: str
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FilingRecord:
    accession_number: str
    archive_cik: str
    form_type: str
    relevance_state: str
    retrieval_status: str
    company_name: str = "UNKNOWN"
    source: str = "unknown"
    issuer_cik: str | None = None
    issuer_name: str | None = None
    accepted_at: datetime | None = None
    filing_date: date | None = None
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    next_retry_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    inactive_reason: str | None = None


@dataclass(slots=True)
class FeedWatermark:
    accepted_at: datetime | None
    accessions_at_boundary: frozenset[str] = field(default_factory=frozenset)

    def serialize(self) -> str:
        return json.dumps({
            "ts": self.accepted_at.isoformat() if self.accepted_at else None,
            "acc": sorted(self.accessions_at_boundary),
        })

    @classmethod
    def deserialize(cls, raw: str | None) -> FeedWatermark:
        if not raw:
            return cls(accepted_at=None)
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            ts = try_parse_datetime(raw)
            return cls(accepted_at=ts)
        ts = try_parse_datetime(blob.get("ts")) if isinstance(blob, dict) else None
        acc = frozenset(blob.get("acc", [])) if isinstance(blob, dict) else frozenset()
        return cls(accepted_at=ts, accessions_at_boundary=acc)


@dataclass(slots=True)
class IndexDocumentRow:
    sequence: str | None = None
    description: str | None = None
    href: str | None = None
    filename: str | None = None
    doc_type: str | None = None
    size: str | None = None


# --- Structured Form 4 / 8-K types ---

@dataclass(slots=True)
class Form4Transaction:
    issuer_cik: str | None = None
    issuer_name: str | None = None
    issuer_ticker: str | None = None
    reporting_owner_cik: str | None = None
    reporting_owner_name: str | None = None
    is_director: bool = False
    is_officer: bool = False
    officer_title: str | None = None
    is_ten_pct_owner: bool = False
    security_title: str | None = None
    transaction_date: str | None = None
    transaction_code: str | None = None
    shares: float | None = None
    price_per_share: float | None = None
    acquired_disposed: str | None = None
    shares_owned_after: float | None = None
    direct_indirect: str | None = None
    is_derivative: bool = False


@dataclass(slots=True)
class Form4Holding:
    issuer_cik: str | None = None
    issuer_name: str | None = None
    issuer_ticker: str | None = None
    reporting_owner_cik: str | None = None
    reporting_owner_name: str | None = None
    is_director: bool = False
    is_officer: bool = False
    officer_title: str | None = None
    is_ten_pct_owner: bool = False
    security_title: str | None = None
    shares_owned: float | None = None
    direct_indirect: str | None = None
    nature_of_ownership: str | None = None
    is_derivative: bool = False


@dataclass(slots=True)
class Form4Filing:
    accession_number: str
    issuer_cik: str | None = None
    issuer_name: str | None = None
    issuer_ticker: str | None = None
    reporting_owners: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[Form4Transaction] = field(default_factory=list)
    holdings: list[Form4Holding] = field(default_factory=list)
    footnotes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class EightKEvent:
    accession_number: str
    item_number: str
    item_description: str
    filing_date: str | None = None
    company_name: str | None = None
    cik: str | None = None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_SEC_TZ = ZoneInfo("America/New_York")


def sec_business_date() -> date:
    """Return today's date in the SEC's America/New_York timezone.

    The SEC publishes daily indices and operates on Eastern Time.  A daemon
    running in Europe (or any other timezone) must use this helper instead
    of ``date.today()`` to avoid misaligned reconciliation windows.
    """
    return datetime.now(_SEC_TZ).date()

def normalize_cik(cik: str | int) -> str:
    s = str(cik).strip()
    if not s.isdigit():
        raise ValueError(f"Invalid CIK: {cik!r}")
    return s.zfill(10)

def accession_nodashes(accession_number: str) -> str:
    return accession_number.replace("-", "")

def derive_archive_base(cik: str | int, accession_number: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(normalize_cik(cik))}/{accession_nodashes(accession_number)}"
    )

def derive_complete_txt_url(cik: str | int, accession_number: str) -> str:
    return f"{derive_archive_base(cik, accession_number)}/{accession_number}.txt"

def derive_index_url(cik: str | int, accession_number: str) -> str:
    return f"{derive_archive_base(cik, accession_number)}/{accession_number}-index.html"

def derive_hdr_sgml_url(cik: str | int, accession_number: str) -> str:
    base = derive_archive_base(cik, accession_number)
    return f"{base}/{accession_number}-hdr.sgml"

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def dump_json(data: dict[str, Any] | list[Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))

def extract_accession(text: str) -> str | None:
    m = ACCESSION_RE.search(text)
    return m.group(0) if m else None

def try_parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None

def try_parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None

def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)

def filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.rsplit("/", 1)[-1].strip()
    return tail or None

def is_textual_primary_filename(name: str | None) -> bool:
    if not name:
        return False
    return name.lower().endswith(TEXTUAL_PRIMARY_EXTENSIONS)

def guess_content_type_from_filename(name: str | None) -> str | None:
    if not name:
        return None
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    lower = name.lower()
    if lower.endswith((".htm", ".html")):
        return "text/html"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith((".xml", ".xsd", ".xsl")):
        return "application/xml"
    if lower.endswith(".pdf"):
        return "application/pdf"
    return None

def looks_like_json_payload(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0] in "[{"

def body_preview(data: bytes, limit: int = 160) -> str:
    return data[:limit].decode("utf-8", errors="replace").replace("\n", " ").strip()

def is_direct_form(form_type: str, direct_forms: tuple[str, ...] = DEFAULT_DIRECT_FORMS) -> bool:
    return form_type.upper().strip() in {f.upper() for f in direct_forms}

def is_ambiguous_form(form_type: str, ambiguous_forms: tuple[str, ...] = DEFAULT_AMBIGUOUS_FORMS) -> bool:
    return form_type.upper().strip() in {f.upper() for f in ambiguous_forms}


# ---------------------------------------------------------------------------
# Watchlist loading and index
# ---------------------------------------------------------------------------

_WATCHLIST_REQUIRED = {"cik", "ticker", "name"}

def load_watchlist_yaml(path: Path) -> list[WatchlistCompany]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "companies" not in raw:
        raise ValueError(f"Watchlist YAML must have a top-level 'companies' key: {path}")
    entries = raw["companies"]
    if not isinstance(entries, list):
        raise ValueError(f"'companies' must be a list: {path}")
    companies: list[WatchlistCompany] = []
    seen_ciks: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} is not a mapping: {entry!r}")
        missing = _WATCHLIST_REQUIRED - entry.keys()
        if missing:
            raise ValueError(f"Entry {i} missing required fields {missing}: {entry!r}")
        cik = _validate_cik(str(entry["cik"]))
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        extra = {k: v for k, v in entry.items() if k not in _WATCHLIST_REQUIRED and k != "isin" and k != "aliases"}
        # Parse aliases from watchlist entries.
        raw_aliases = entry.get("aliases", [])
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        aliases = tuple(str(a) for a in raw_aliases if a)
        companies.append(WatchlistCompany(
            cik=cik, ticker=str(entry["ticker"]),
            name=str(entry["name"]), aliases=aliases, metadata=extra,
        ))
    if not companies:
        raise ValueError(f"Watchlist is empty: {path}")
    logger.info("loaded %d unique companies from watchlist %s", len(companies), path)
    return companies


class WatchlistIndex:
    """O(1) lookup by CIK and normalized name (including aliases)."""

    def __init__(self, companies: list[WatchlistCompany]) -> None:
        self._by_cik: dict[str, WatchlistCompany] = {}
        self._by_name: dict[str, WatchlistCompany] = {}
        for c in companies:
            self._by_cik[c.cik] = c
            self._by_name[c.name_normalized] = c
            # Index all aliases alongside the canonical name.
            for alias in c.aliases:
                norm_alias = normalize_name(alias)
                if norm_alias and norm_alias not in self._by_name:
                    self._by_name[norm_alias] = c

    def __len__(self) -> int:
        return len(self._by_cik)

    def match_cik(self, cik: str | None) -> WatchlistCompany | None:
        if not cik:
            return None
        return self._by_cik.get(cik.zfill(10))

    def match_name(self, name: str | None) -> WatchlistCompany | None:
        if not name:
            return None
        return self._by_name.get(normalize_name(name))

    def contains_cik(self, cik: str | None) -> bool:
        if not cik:
            return False
        return cik.zfill(10) in self._by_cik

    @property
    def all_ciks(self) -> frozenset[str]:
        return frozenset(self._by_cik.keys())

    @property
    def companies(self) -> list[WatchlistCompany]:
        return list(self._by_cik.values())


# ---------------------------------------------------------------------------
# Header-gate resolver
# ---------------------------------------------------------------------------

class HeaderResolver:
    def __init__(self, watchlist: WatchlistIndex) -> None:
        self.watchlist = watchlist

    def resolve(
        self, header: SubmissionHeader,
    ) -> tuple[RelevanceState, WatchlistCompany | None, FilingParty | None]:
        canonical = header.canonical_issuer()
        form_type = (header.form_type or "").upper().strip()

        if form_type in ACTIVIST_FORMS and canonical is None:
            return RelevanceState.UNRESOLVED, None, None

        if canonical is None:
            return RelevanceState.HDR_FAILED, None, None

        if canonical.cik:
            match = self.watchlist.match_cik(canonical.cik)
            if match:
                return RelevanceState.HDR_MATCH, match, canonical
            return RelevanceState.IRRELEVANT, None, canonical

        if canonical.name:
            match = self.watchlist.match_name(canonical.name)
            if match:
                return RelevanceState.HDR_MATCH, match, canonical

        return RelevanceState.IRRELEVANT, None, canonical


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

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


class UrllibTransport:
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
        self._transport = transport or UrllibTransport(timeout=settings.http_timeout_seconds)
        self._headers = {
            "User-Agent": settings.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        }
        self._max_retries = settings.max_retries
        self._retry_base = settings.retry_base_seconds

    async def aclose(self) -> None:
        pass

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
# Parsers (carried forward from prototype)
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


# ---------------------------------------------------------------------------
# Structured Form 4 / 8-K parsers
# ---------------------------------------------------------------------------

def _safe_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip())
    except (ValueError, TypeError):
        return None


def _safe_bool(text: str | None) -> bool:
    if not text:
        return False
    return text.strip() in ("1", "true", "True", "yes")


def _el_text(parent: ET.Element | None, tag: str) -> str | None:
    if parent is None:
        return None
    # Try the tag as-is first (handles no-namespace and XPath-style tags)
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    # If the tag is a simple name (no namespace, no path operators),
    # try the wildcard namespace prefix so we match any namespace.
    if "{" not in tag and "/" not in tag and "." not in tag:
        el = parent.find(f"{{*}}{tag}")
        if el is not None and el.text:
            return el.text.strip()
    # For XPath-style tags like ".//securityTitle/value", try
    # replacing each path component with a wildcard-namespaced version.
    if "/" in tag and "{" not in tag:
        parts = tag.split("/")
        ns_parts = []
        for part in parts:
            if part == "" or part == ".":
                ns_parts.append(part)
            else:
                ns_parts.append(f"{{*}}{part}")
        ns_tag = "/".join(ns_parts)
        el = parent.find(ns_tag)
        if el is not None and el.text:
            return el.text.strip()
    return None


def parse_form4_xml(xml_bytes: bytes, accession_number: str) -> Form4Filing | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.warning("ownership form XML parse failed for %s", accession_number)
        return None

    filing = Form4Filing(accession_number=accession_number)

    issuer_el = root.find(".//issuer")
    if issuer_el is None:
        issuer_el = root.find(".//{*}issuer")
    if issuer_el is not None:
        filing.issuer_cik = _el_text(issuer_el, "issuerCik")
        filing.issuer_name = _el_text(issuer_el, "issuerName")
        filing.issuer_ticker = _el_text(issuer_el, "issuerTradingSymbol")

    owner_els = root.findall(".//reportingOwner")
    if not owner_els:
        owner_els = root.findall(".//{*}reportingOwner")
    for owner_el in owner_els:
        owner_id = owner_el.find("reportingOwnerId")
        if owner_id is None:
            owner_id = owner_el.find("{*}reportingOwnerId")
        owner_rel = owner_el.find("reportingOwnerRelationship")
        if owner_rel is None:
            owner_rel = owner_el.find("{*}reportingOwnerRelationship")
        owner_info: dict[str, Any] = {
            "cik": _el_text(owner_id, "rptOwnerCik"),
            "name": _el_text(owner_id, "rptOwnerName"),
            "is_director": _safe_bool(_el_text(owner_rel, "isDirector")),
            "is_officer": _safe_bool(_el_text(owner_rel, "isOfficer")),
            "officer_title": _el_text(owner_rel, "officerTitle"),
            "is_ten_pct_owner": _safe_bool(_el_text(owner_rel, "isTenPercentOwner")),
        }
        filing.reporting_owners.append(owner_info)

    table = root.find(".//nonDerivativeTable")
    if table is None:
        table = root.find(".//{*}nonDerivativeTable")
    if table is not None:
        txn_els = table.findall("nonDerivativeTransaction")
        if not txn_els:
            txn_els = table.findall("{*}nonDerivativeTransaction")
        for txn_el in txn_els:
            txns = _parse_form4_transaction(txn_el, filing, is_derivative=False)
            filing.transactions.extend(txns)
        hold_els = table.findall("nonDerivativeHolding")
        if not hold_els:
            hold_els = table.findall("{*}nonDerivativeHolding")
        for hold_el in hold_els:
            holdings = _parse_form4_holding(hold_el, filing, is_derivative=False)
            filing.holdings.extend(holdings)

    table = root.find(".//derivativeTable")
    if table is None:
        table = root.find(".//{*}derivativeTable")
    if table is not None:
        txn_els = table.findall("derivativeTransaction")
        if not txn_els:
            txn_els = table.findall("{*}derivativeTransaction")
        for txn_el in txn_els:
            txns = _parse_form4_transaction(txn_el, filing, is_derivative=True)
            filing.transactions.extend(txns)
        hold_els = table.findall("derivativeHolding")
        if not hold_els:
            hold_els = table.findall("{*}derivativeHolding")
        for hold_el in hold_els:
            holdings = _parse_form4_holding(hold_el, filing, is_derivative=True)
            filing.holdings.extend(holdings)

    fn_els = root.findall(".//footnote")
    if not fn_els:
        fn_els = root.findall(".//{*}footnote")
    for fn_el in fn_els:
        fn_id = fn_el.get("id", "")
        fn_text = fn_el.text or ""
        if fn_id:
            filing.footnotes[fn_id] = fn_text.strip()

    return filing


def _parse_form4_transaction(
    txn_el: ET.Element, filing: Form4Filing, *, is_derivative: bool,
) -> list[Form4Transaction]:
    """Parse a single transaction element into one row **per** reporting owner.

    SEC Form 3/4/5 filings can list multiple reporting owners (joint
    filings).  The prior implementation always attributed every
    transaction to ``reporting_owners[0]``, silently misattributing
    multi-owner filings.  We now emit one ``Form4Transaction`` per owner
    so downstream analytics correctly reflect each owner's activity.
    """
    sec_title = _el_text(txn_el, ".//securityTitle/value") or _el_text(txn_el, "securityTitle")
    txn_date = _el_text(txn_el, ".//transactionDate/value") or _el_text(txn_el, "transactionDate")
    amounts = txn_el.find(".//transactionAmounts")
    if amounts is None:
        amounts = txn_el.find(".//{*}transactionAmounts")
    coding = txn_el.find(".//transactionCoding")
    if coding is None:
        coding = txn_el.find(".//{*}transactionCoding")
    post = txn_el.find(".//postTransactionAmounts")
    if post is None:
        post = txn_el.find(".//{*}postTransactionAmounts")
    ownership = txn_el.find(".//ownershipNature")
    if ownership is None:
        ownership = txn_el.find(".//{*}ownershipNature")

    txn_code = _el_text(coding, "transactionCode") if coding is not None else None
    shares_raw = _el_text(amounts, ".//transactionShares/value") if amounts is not None else None
    price_raw = _el_text(amounts, ".//transactionPricePerShare/value") if amounts is not None else None
    acq_disp = _el_text(amounts, ".//transactionAcquiredDisposedCode/value") if amounts is not None else None
    shares_after = _el_text(post, ".//sharesOwnedFollowingTransaction/value") if post is not None else None
    direct_indirect = _el_text(ownership, ".//directOrIndirectOwnership/value") if ownership is not None else None

    owners = filing.reporting_owners if filing.reporting_owners else [{}]
    results: list[Form4Transaction] = []
    for owner in owners:
        results.append(Form4Transaction(
            issuer_cik=filing.issuer_cik, issuer_name=filing.issuer_name,
            issuer_ticker=filing.issuer_ticker,
            reporting_owner_cik=owner.get("cik"),
            reporting_owner_name=owner.get("name"),
            is_director=owner.get("is_director", False),
            is_officer=owner.get("is_officer", False),
            officer_title=owner.get("officer_title"),
            is_ten_pct_owner=owner.get("is_ten_pct_owner", False),
            security_title=sec_title, transaction_date=txn_date,
            transaction_code=txn_code, shares=_safe_float(shares_raw),
            price_per_share=_safe_float(price_raw),
            acquired_disposed=acq_disp,
            shares_owned_after=_safe_float(shares_after),
            direct_indirect=direct_indirect,
            is_derivative=is_derivative,
        ))
    return results


def _parse_form4_holding(
    hold_el: ET.Element, filing: Form4Filing, *, is_derivative: bool,
) -> list[Form4Holding]:
    """Parse a single holding element into one row **per** reporting owner.

    See ``_parse_form4_transaction`` for the multi-owner rationale.
    """
    sec_title = _el_text(hold_el, ".//securityTitle/value") or _el_text(hold_el, "securityTitle")
    post = hold_el.find(".//postTransactionAmounts")
    if post is None:
        post = hold_el.find(".//{*}postTransactionAmounts")
    shares_owned = _el_text(post, ".//sharesOwnedFollowingTransaction/value") if post is not None else None
    ownership = hold_el.find(".//ownershipNature")
    if ownership is None:
        ownership = hold_el.find(".//{*}ownershipNature")
    direct_indirect = _el_text(ownership, ".//directOrIndirectOwnership/value") if ownership is not None else None
    nature_text = _el_text(ownership, ".//natureOfOwnership/value") if ownership is not None else None

    owners = filing.reporting_owners if filing.reporting_owners else [{}]
    results: list[Form4Holding] = []
    for owner in owners:
        results.append(Form4Holding(
            issuer_cik=filing.issuer_cik, issuer_name=filing.issuer_name,
            issuer_ticker=filing.issuer_ticker,
            reporting_owner_cik=owner.get("cik"),
            reporting_owner_name=owner.get("name"),
            is_director=owner.get("is_director", False),
            is_officer=owner.get("is_officer", False),
            officer_title=owner.get("officer_title"),
            is_ten_pct_owner=owner.get("is_ten_pct_owner", False),
            security_title=sec_title, shares_owned=_safe_float(shares_owned),
            direct_indirect=direct_indirect, nature_of_ownership=nature_text,
            is_derivative=is_derivative,
        ))
    return results


# --- 8-K parsing ---

_8K_ITEM_DESCRIPTIONS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Principal Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Nominations",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

_8K_FULL_TITLES: dict[str, list[str]] = {
    "1.01": ["Entry into a Material Definitive Agreement"],
    "1.02": ["Termination of a Material Definitive Agreement"],
    "2.01": ["Completion of Acquisition or Disposition of Assets"],
    "2.02": ["Results of Operations and Financial Condition"],
    "5.02": [
        "Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; Compensatory Arrangements of Certain Officers",
        "Departure of Directors or Principal Officers",
        "Departure/Election of Directors or Principal Officers",
    ],
    "7.01": ["Regulation FD Disclosure", "Regulation FD", "Reg FD Disclosure", "Reg FD"],
    "8.01": ["Other Events"],
    "9.01": ["Financial Statements and Exhibits", "Financial Statements & Exhibits"],
}


def _normalize_8k_text(text: str) -> str:
    s = text.upper().strip()
    s = re.sub(r"^ITEM\s+(?!\d)", "", s)
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_8K_NORMALIZED_LOOKUP: dict[str, str] = {}
for _item_num, _titles in _8K_FULL_TITLES.items():
    for _title in _titles:
        _key = _normalize_8k_text(_title)
        if _key not in _8K_NORMALIZED_LOOKUP:
            _8K_NORMALIZED_LOOKUP[_key] = _item_num
for _item_num, _desc in _8K_ITEM_DESCRIPTIONS.items():
    _key = _normalize_8k_text(_desc)
    if _key not in _8K_NORMALIZED_LOOKUP:
        _8K_NORMALIZED_LOOKUP[_key] = _item_num

_8K_ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)", re.IGNORECASE)

# Regex for extracting Item references from the primary document HTML body.
# Matches patterns like "Item 1.01" or "ITEM 2.02" surrounded by HTML tags,
# whitespace, or punctuation — the typical rendering in 8-K primary docs.
_8K_BODY_ITEM_RE = re.compile(
    r"(?:^|\s|>)\s*Item\s+(\d+\.\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_8k_items_from_body(body_text: str) -> list[str]:
    """Extract unique 8-K item numbers from the primary document body.

    Strips HTML tags first, then scans for ``Item X.XX`` patterns.
    Only returns item numbers that appear in the known 8-K taxonomy
    to avoid false positives from boilerplate like "Item 10" references
    in other SEC filings.
    """
    cleaned = _HTML_TAG_RE.sub(" ", body_text)
    found: list[str] = []
    seen: set[str] = set()
    for m in _8K_BODY_ITEM_RE.finditer(cleaned):
        item_num = m.group(1)
        if item_num in _8K_ITEM_DESCRIPTIONS and item_num not in seen:
            seen.add(item_num)
            found.append(item_num)
    return found


def _resolve_8k_item_number(item_text: str) -> tuple[str, str]:
    m = _8K_ITEM_RE.search(item_text)
    if m:
        item_num = m.group(1)
        desc = _8K_ITEM_DESCRIPTIONS.get(item_num, item_text.strip())
        return item_num, desc
    normalized = _normalize_8k_text(item_text)
    if normalized in _8K_NORMALIZED_LOOKUP:
        item_num = _8K_NORMALIZED_LOOKUP[normalized]
        return item_num, _8K_ITEM_DESCRIPTIONS.get(item_num, item_text.strip())
    best_match: tuple[str, int] | None = None
    for known_text, item_num in _8K_NORMALIZED_LOOKUP.items():
        if known_text in normalized or normalized in known_text:
            match_len = len(known_text)
            if best_match is None or match_len > best_match[1]:
                best_match = (item_num, match_len)
    if best_match is not None:
        item_num = best_match[0]
        return item_num, _8K_ITEM_DESCRIPTIONS.get(item_num, item_text.strip())
    return "unknown", item_text.strip()


def parse_8k_items(
    header: SubmissionHeader, accession_number: str,
    company_name: str | None = None, cik: str | None = None,
    filing_date: str | None = None,
    primary_doc_body: str | None = None,
) -> list[EightKEvent]:
    """Extract 8-K item events from an SGML header, with body fallback.

    The SGML ``ITEM INFORMATION:`` header field is used as the fast path.
    When that field is absent or empty (which happens on a non-trivial
    fraction of real 8-K filings), we fall back to scanning the primary
    document HTML body for ``Item X.XX`` references.  Items discovered
    from the body that were already found via the header are deduplicated.
    """
    form_type = (header.form_type or "").upper().strip()
    if not form_type.startswith("8-K"):
        return []

    seen_items: set[str] = set()
    events: list[EightKEvent] = []

    # --- Fast path: SGML header items ---
    for item_text in header.item_information:
        item_num, desc = _resolve_8k_item_number(item_text)
        if item_num not in seen_items:
            seen_items.add(item_num)
            events.append(EightKEvent(
                accession_number=accession_number, item_number=item_num,
                item_description=desc, filing_date=filing_date,
                company_name=company_name or header.company_name,
                cik=cik or header.cik,
            ))

    # --- Fallback: parse item numbers from the primary document body ---
    if primary_doc_body:
        body_items = _extract_8k_items_from_body(primary_doc_body)
        for item_num in body_items:
            if item_num not in seen_items:
                seen_items.add(item_num)
                desc = _8K_ITEM_DESCRIPTIONS.get(item_num, item_num)
                events.append(EightKEvent(
                    accession_number=accession_number, item_number=item_num,
                    item_description=desc, filing_date=filing_date,
                    company_name=company_name or header.company_name,
                    cik=cik or header.cik,
                ))
        if body_items and not header.item_information:
            logger.info(
                "8-K body fallback found %d items for %s (header had none)",
                len(body_items), accession_number,
            )

    return events


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

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