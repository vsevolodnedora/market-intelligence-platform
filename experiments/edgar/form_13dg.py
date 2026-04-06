"""SC 13D/G activist/beneficial ownership parsing and handler implementation.

Extracts structured ownership data from SC 13D and SC 13G filings including
ownership percentage, shares beneficially owned, filer identity, and subject
company identity.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from domain import (
    FilingDiscovery,
    SubmissionHeader,
    ThirteenDGFiling,
    _13DG_FORM_RE,
    get_logger,
)


logger = get_logger(__name__)

# Patterns for extracting key fields from 13D/G text bodies
_PERCENT_RE = re.compile(
    r"(?:percent(?:age)?\s+(?:of\s+)?(?:class|shares|securities|outstanding))"
    r"[^0-9]*?(\d{1,3}(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_PERCENT_SIMPLE_RE = re.compile(
    r"(\d{1,3}\.\d+)\s*%\s*(?:of\s+(?:the\s+)?(?:outstanding|class|shares))",
    re.IGNORECASE,
)
_SHARES_RE = re.compile(
    r"(?:aggregate\s+number|number\s+of\s+shares|shares?\s+beneficially\s+owned)"
    r"[^0-9]*?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_CUSIP_RE = re.compile(r"CUSIP\s*(?:No\.?|Number)?\s*[:\s]*([A-Z0-9]{6,9})", re.IGNORECASE)
_DATE_OF_EVENT_RE = re.compile(
    r"DATE\s+OF\s+EVENT[^:]*:\s*(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_AMENDMENT_RE = re.compile(r"(?:AMENDMENT\s+NO\.?\s*:?\s*(\d+))", re.IGNORECASE)


def _safe_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def parse_13dg_text(
    text: str,
    accession_number: str,
    header: SubmissionHeader,
    discovery: FilingDiscovery,
) -> ThirteenDGFiling | None:
    """Extract structured data from a 13D/G filing body text."""
    form_type = (header.form_type or "").upper().strip()
    is_amendment = "/A" in form_type

    filing = ThirteenDGFiling(
        accession_number=accession_number,
        form_type=form_type,
        is_amendment=is_amendment,
        filing_date=discovery.filing_date.isoformat() if discovery.filing_date else None,
    )

    # Filer and subject from header parties
    if header.parties:
        subject = header.subject_company
        # For SC 13D/G, the activist may be tagged as "filer" or "filed-by"
        filer = header.filer
        if filer is None:
            filer = next((p for p in header.parties if p.role == "filed-by"), None)
        if filer is None and header.parties:
            # Last resort: first party that isn't the subject
            for p in header.parties:
                if p.role != "subject-company":
                    filer = p
                    break
        if subject:
            filing.subject_cik = subject.cik
            filing.subject_name = subject.name
        if filer:
            filing.filer_cik = filer.cik
            filing.filer_name = filer.name

    # CUSIP
    m = _CUSIP_RE.search(text)
    if m:
        filing.subject_cusip = m.group(1).strip()

    # Ownership percentage - try structured pattern first, then simpler
    m = _PERCENT_RE.search(text)
    if m:
        filing.ownership_percent = _safe_float(m.group(1))
    else:
        m = _PERCENT_SIMPLE_RE.search(text)
        if m:
            filing.ownership_percent = _safe_float(m.group(1))

    # Shares beneficially owned
    m = _SHARES_RE.search(text)
    if m:
        filing.shares_beneficially_owned = _safe_float(m.group(1))

    # Date of event
    m = _DATE_OF_EVENT_RE.search(text)
    if m:
        filing.date_of_event = m.group(1).strip()

    # Amendment number
    if is_amendment:
        m = _AMENDMENT_RE.search(text)
        if m:
            try:
                filing.amendment_number = int(m.group(1))
            except ValueError:
                pass

    return filing


class ThirteenDGHandler:
    """FormHandler implementation for SC 13D and SC 13G filings."""

    def supports(self, form_type: str) -> bool:
        return bool(_13DG_FORM_RE.fullmatch(form_type.upper().strip()))

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> ThirteenDGFiling | None:
        if primary_bytes is None:
            return None
        try:
            text = primary_bytes.decode("utf-8", errors="replace")
            return parse_13dg_text(text, accession_number, header, discovery)
        except Exception:
            logger.exception("13D/G parse failed for %s (non-fatal)", accession_number)
            return None

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        if not isinstance(parsed, ThirteenDGFiling):
            return
        f = parsed
        conn.execute(
            "DELETE FROM thirteendg_filings WHERE accession_number=?",
            (accession_number,),
        )
        conn.execute(
            """INSERT INTO thirteendg_filings (
                accession_number, form_type, filer_cik, filer_name,
                subject_cik, subject_name, subject_cusip,
                date_of_event, ownership_percent,
                shares_beneficially_owned, is_amendment,
                amendment_number, filing_date, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                accession_number, f.form_type, f.filer_cik, f.filer_name,
                f.subject_cik, f.subject_name, f.subject_cusip,
                f.date_of_event, f.ownership_percent,
                f.shares_beneficially_owned, int(f.is_amendment),
                f.amendment_number, f.filing_date, now_iso,
            ),
        )

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        from event_builders import build_13dg_event
        if not isinstance(parsed, ThirteenDGFiling):
            return []
        return [build_13dg_event(accession_number, parsed)]
