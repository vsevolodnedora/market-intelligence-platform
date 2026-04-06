"""Compatibility façade — re-exports all public symbols from the edgar package.

This file exists so that existing import statements like
``from edgar_core import Settings, SECClient, ...`` continue to work
without changes.  New code should import directly from ``edgar.*``
sub-modules.

This façade will be removed once all internal imports have been
migrated to the package.
"""

# --- Domain types, enums, constants, and helpers ---
from domain import (  # noqa: F401
    ACCESSION_RE,
    ACTIVIST_FORMS,
    ARCHIVE_LINK_RE,
    ATOM_NS,
    BINARY_PRIMARY_EXTENSIONS,
    CIK_IN_TITLE_RE,
    DEFAULT_ALL_FORMS,
    DEFAULT_AMBIGUOUS_FORMS,
    DEFAULT_DIRECT_FORMS,
    DOC_EXT_RE,
    DOC_FIELD_PATTERNS,
    DOC_TEXT_BODY_RE,
    DOCUMENT_RE,
    EightKEvent,
    EightKExhibitFact,
    FALLBACK_CIK_RE,
    FALLBACK_NAME_RE,
    FIELD_PATTERNS,
    FUND_FORMS,
    FeedWatermark,
    FilingArtifact,
    FilingDiscovery,
    FilingParty,
    FilingPriority,
    FilingRecord,
    Form4Filing,
    Form4Holding,
    Form4Transaction,
    FundFiling,
    FundHolding,
    HREF_CIK_RE,
    ITEM_INFO_RE,
    IndexDocumentRow,
    LATEST_FILINGS_ATOM_URL,
    MAX_FILING_RETRY_ATTEMPTS,
    OWNERSHIP_FORMS,
    RelevanceState,
    RetrievalStatus,
    RetrievedFilingBundle,
    SECTION_CIK_RE,
    SECTION_NAME_RE,
    Settings,
    SubmissionDocument,
    SubmissionHeader,
    TEXTUAL_PRIMARY_EXTENSIONS,
    TITLE_RE,
    ThirteenDGFiling,
    ThirteenFFiling,
    ThirteenFHolding,
    WatchlistCompany,
    XBRLFact,
    XBRLFiling,
    _13DG_FORM_RE,
    _13F_FORM_RE,
    _ANGLE_SECTION_RE,
    _COLON_SECTION_RE,
    _DEFAULT_MAX_RETRIES,
    _DEFAULT_RETRY_BASE_SECONDS,
    _DOC_END_RE_B,
    _DOC_START_RE_B,
    _FILENAME_RE_B,
    _FUND_FORM_RE,
    _INDEX_TABLE_ROW_RE,
    _OWNERSHIP_FORM_RE,
    _RETRYABLE_HTTP_CODES,
    _RETRYABLE_RETRIEVAL_STATUSES,
    _TERMINAL_RELEVANCE_STATES,
    _TERMINAL_RETRIEVAL_STATUSES,
    _TEXT_END_RE_B,
    _TEXT_START_RE_B,
    _XBRL_ANNUAL_QUARTERLY_RE,
    _XML_WRAPPER_END_B,
    _XML_WRAPPER_START_B,
    _validate_accession,
    _validate_cik,
    accession_nodashes,
    body_preview,
    derive_archive_base,
    derive_complete_txt_url,
    derive_hdr_sgml_url,
    derive_index_url,
    dump_json,
    extract_accession,
    filename_from_url,
    get_logger,
    guess_content_type_from_filename,
    is_ambiguous_form,
    is_direct_form,
    is_textual_primary_filename,
    looks_like_json_payload,
    normalize_cik,
    normalize_name,
    safe_filename,
    sec_business_date,
    sha256_hex,
    try_parse_date,
    try_parse_datetime,
    utcnow,
)

# --- SEC I/O: transport, rate limiting, parsers ---
from sec_io import (  # noqa: F401
    AsyncTokenBucket,
    HTTPTransport,
    MockTransport,
    PooledTransport,
    SECClient,
    SECHTTPError,
    SECResponseFormatError,
    UrllibTransport,
    choose_primary_document,
    choose_primary_document_filename,
    choose_primary_document_from_header,
    extract_archive_links,
    extract_primary_document_bytes,
    extract_submissions_rollover_urls,
    filter_by_forms,
    normalized_header_metadata,
    parse_company_idx,
    parse_index_document_rows,
    parse_latest_filings_atom,
    parse_submission_text,
    parse_submissions_json,
    parse_submissions_rollover_json,
)

# --- Watchlist ---
from watchlist import (  # noqa: F401
    HeaderResolver,
    WatchlistIndex,
    load_watchlist_yaml,
)

# --- Storage ---
from storage import SQLiteStorage  # noqa: F401

# --- Form handlers ---
from form_registry import FormHandler, FormRegistry  # noqa: F401
from form_form4 import Form4Handler, parse_form4_xml  # noqa: F401
from form_eight_k import EightKHandler, parse_8k_items  # noqa: F401
from form_13f import ThirteenFHandler, parse_13f_xml  # noqa: F401
from form_13dg import ThirteenDGHandler, parse_13dg_text  # noqa: F401
from form_xbrl import XBRLHandler, parse_xbrl_filing  # noqa: F401
from form_fund import FundHandler  # noqa: F401
