"""Shared domain types, enums, settings, and small generic helpers.

These are the contracts imported across the entire EDGAR ingestor codebase.
Nothing in this module touches SEC HTTP transport, parsers, SQLite, or
filesystem I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Module logger factory
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

# 13F form matching
_13F_FORM_RE = re.compile(r"^13F-(?:HR|NT)(?:/A)?$", re.IGNORECASE)
# 13D/G form matching (both "SC 13D" and bare "13D" forms)
_13DG_FORM_RE = re.compile(r"^(?:SC\s+)?13[DG](?:/A)?$", re.IGNORECASE)
# XBRL-bearing annual/quarterly forms
_XBRL_ANNUAL_QUARTERLY_RE = re.compile(
    r"^(?:10-[KQ]|20-F|40-F|6-K)(?:/A)?$", re.IGNORECASE,
)
# Fund/ETF forms
FUND_FORMS = frozenset({
    "N-PORT", "N-PORT/A", "N-PORT-EX", "N-PORT-EX/A",
    "N-CEN", "N-CEN/A",
    "497", "497K", "497J", "497AD",
    "485APOS", "485BPOS", "485BXT",
})
_FUND_FORM_RE = re.compile(
    r"^(?:N-PORT(?:-EX)?|N-CEN|497(?:K|J|AD)?|485[AB]POS|485BXT)(?:/A)?$",
    re.IGNORECASE,
)

# --- Form taxonomy ---

DEFAULT_DIRECT_FORMS: tuple[str, ...] = (
    "8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A",
    "6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
    # Institutional holdings
    "13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A",
    # Fund/ETF filings
    "N-PORT", "N-PORT/A", "N-PORT-EX", "N-PORT-EX/A",
    "N-CEN", "N-CEN/A",
    "497", "497K", "497J", "497AD",
    "485APOS", "485BPOS", "485BXT",
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
    HDR_FAILED = "hdr_failed"
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
# Derived enum sets
# ---------------------------------------------------------------------------

_TERMINAL_RELEVANCE_STATES = frozenset({
    RelevanceState.DIRECT_MATCH.value,
    RelevanceState.DIRECT_UNMATCHED.value,
    RelevanceState.HDR_MATCH.value,
    RelevanceState.IRRELEVANT.value,
    RelevanceState.HDR_FAILED.value,
    RelevanceState.UNRESOLVED.value,
})

_TERMINAL_RETRIEVAL_STATUSES = frozenset({
    RetrievalStatus.RETRIEVED.value,
})

_RETRYABLE_RETRIEVAL_STATUSES = frozenset({
    RetrievalStatus.RETRIEVAL_FAILED.value,
    RetrievalStatus.RETRIEVED_PARTIAL.value,
})

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
    publish_dir: Path | None = None
    live_workers: int = 3
    live_rps_share: float = 0.6
    out_form4_transactions_cap: int = 20
    out_form4_owners_cap: int = 10
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
            return self.subject_company
        return self.filer

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


@dataclass(slots=True)
class EightKExhibitFact:
    """Structured fact extracted from an 8-K exhibit or body text.

    Goes beyond item-number detection to capture quantitative and
    qualitative data from 2.02 earnings, deal economics, guidance, etc.
    """
    accession_number: str
    item_number: str
    fact_type: str  # e.g. "earnings_eps", "revenue", "guidance", "deal_value", "management_change"
    fact_key: str   # e.g. "eps_diluted", "revenue_q3", "new_ceo_name"
    fact_value: str | None = None
    fact_numeric: float | None = None
    currency: str | None = None
    period: str | None = None  # e.g. "Q3 2025", "FY2025"
    filing_date: str | None = None
    company_name: str | None = None
    cik: str | None = None


# --- 13F types ---

@dataclass(slots=True)
class ThirteenFHolding:
    """Single holding row from a 13F-HR information table."""
    issuer_name: str | None = None
    title_of_class: str | None = None
    cusip: str | None = None
    value_thousands: float | None = None
    shares_or_principal: float | None = None
    shares_or_principal_type: str | None = None  # "SH" or "PRN"
    investment_discretion: str | None = None     # "SOLE", "SHARED", "DFND"
    voting_sole: int | None = None
    voting_shared: int | None = None
    voting_none: int | None = None
    put_call: str | None = None  # "PUT", "CALL", or None


@dataclass(slots=True)
class ThirteenFFiling:
    """Parsed 13F-HR filing."""
    accession_number: str
    filer_cik: str | None = None
    filer_name: str | None = None
    report_period: str | None = None       # e.g. "2025-03-31"
    filing_type: str | None = None          # "13F-HR" or "13F-HR/A"
    total_value_thousands: float | None = None
    entry_count: int = 0
    holdings: list[ThirteenFHolding] = field(default_factory=list)


# --- 13D/G types ---

@dataclass(slots=True)
class ThirteenDGFiling:
    """Parsed SC 13D or SC 13G filing."""
    accession_number: str
    form_type: str | None = None       # "SC 13D", "SC 13G", etc.
    filer_cik: str | None = None
    filer_name: str | None = None
    subject_cik: str | None = None
    subject_name: str | None = None
    subject_cusip: str | None = None
    date_of_event: str | None = None
    ownership_percent: float | None = None
    shares_beneficially_owned: float | None = None
    is_amendment: bool = False
    amendment_number: int | None = None
    filing_date: str | None = None


# --- XBRL types ---

@dataclass(slots=True)
class XBRLFact:
    """Single XBRL fact extracted from an inline-XBRL or instance document."""
    concept: str               # e.g. "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    value: str | None = None
    numeric_value: float | None = None
    unit: str | None = None    # e.g. "USD", "shares"
    period_start: str | None = None
    period_end: str | None = None
    period_instant: str | None = None
    decimals: str | None = None
    context_id: str | None = None
    segment: str | None = None  # dimension member if any


@dataclass(slots=True)
class XBRLFiling:
    """Parsed XBRL data from a 10-K/10-Q/20-F/40-F filing."""
    accession_number: str
    form_type: str | None = None
    filer_cik: str | None = None
    filer_name: str | None = None
    period_of_report: str | None = None
    fiscal_year_end: str | None = None
    facts: list[XBRLFact] = field(default_factory=list)


# --- Fund/ETF types ---

@dataclass(slots=True)
class FundHolding:
    """Single holding from an N-PORT filing."""
    issuer_name: str | None = None
    title: str | None = None
    cusip: str | None = None
    isin: str | None = None
    lei: str | None = None
    balance: float | None = None
    units: str | None = None       # "NS" (shares), "PA" (principal), "NC" (contracts)
    value_usd: float | None = None
    pct_of_nav: float | None = None
    asset_category: str | None = None
    issuer_category: str | None = None
    country: str | None = None
    currency: str | None = None
    is_restricted: bool = False
    maturity_date: str | None = None
    coupon_rate: float | None = None


@dataclass(slots=True)
class FundFiling:
    """Parsed fund filing (N-PORT, N-CEN, 497, 485)."""
    accession_number: str
    form_type: str | None = None
    filer_cik: str | None = None
    filer_name: str | None = None
    series_id: str | None = None
    series_name: str | None = None
    class_id: str | None = None
    report_date: str | None = None
    total_assets: float | None = None
    net_assets: float | None = None
    holding_count: int = 0
    holdings: list[FundHolding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# RetrievedFilingBundle — the unified result object for retrieval
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RetrievedFilingBundle:
    """Bundle of all data produced by a single filing retrieval.

    Replaces the long argument list of ``commit_retrieved_filing()``.
    The retriever populates this once; the commit layer consumes it.
    Future form additions extend this dataclass without changing
    signatures.
    """
    accession_number: str
    archive_cik: str
    form_type: str
    company_name: str
    # Header data
    header: SubmissionHeader
    canonical_cik: str | None = None
    canonical_name: str | None = None
    canonical_name_normalized: str | None = None
    # Artifact paths + hashes (already written atomically)
    txt_path: str | None = None
    txt_sha256: str | None = None
    primary_doc_path: str | None = None
    primary_sha256: str | None = None
    primary_document_url: str | None = None
    # Artifact record
    artifact: FilingArtifact | None = None
    # Structured extracts keyed by handler name
    form_results: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_SEC_TZ = ZoneInfo("America/New_York")


def sec_business_date() -> date:
    """Return today's date in the SEC's America/New_York timezone."""
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
