"""EDGAR ingestor package.

Re-exports the most commonly used symbols for convenience.
Downstream code can import from ``edgar.domain``, ``edgar.sec_io``,
etc. directly, or use this package-level namespace.
"""

from domain import *  # noqa: F401,F403
from sec_io import (  # noqa: F401
    AsyncTokenBucket,
    HTTPTransport,
    MockTransport,
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
from watchlist import (  # noqa: F401
    HeaderResolver,
    WatchlistIndex,
    load_watchlist_yaml,
)
from edgar.storage import SQLiteStorage  # noqa: F401
from edgar.forms.registry import FormHandler, FormRegistry  # noqa: F401
from edgar.forms.form4 import Form4Handler, parse_form4_xml  # noqa: F401
from edgar.forms.eight_k import EightKHandler, parse_8k_items  # noqa: F401
