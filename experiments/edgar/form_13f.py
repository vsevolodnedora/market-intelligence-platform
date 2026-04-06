"""13F-HR institutional holdings parsing and handler implementation.

Parses the 13F information table XML to extract individual holdings
with CUSIP, value, share count, and voting authority data.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Any

from domain import (
    FilingDiscovery,
    SubmissionHeader,
    ThirteenFFiling,
    ThirteenFHolding,
    _13F_FORM_RE,
    get_logger,
)


logger = get_logger(__name__)

# Namespace used in 13F information table XML
_13F_NS = "http://www.sec.gov/document/thirteenf"
_13F_TABLE_NS = "http://www.sec.gov/document/thirteenftable"

# Try multiple known namespace patterns
_NS_PATTERNS = [
    {"ns": _13F_NS, "tbl": _13F_TABLE_NS},
    {"ns": "http://www.sec.gov/document/thirteenf-2005", "tbl": "http://www.sec.gov/document/thirteenftable-2005"},
]


def _safe_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def _safe_int(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text.strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def _find_text(el: ET.Element | None, tag: str) -> str | None:
    """Find text in element, trying with and without namespace."""
    if el is None:
        return None
    # Try bare tag
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    # Try wildcard namespace
    child = el.find(f"{{*}}{tag}")
    if child is not None and child.text:
        return child.text.strip()
    return None


def parse_13f_xml(xml_bytes: bytes, accession_number: str) -> ThirteenFFiling | None:
    """Parse a 13F information table XML document."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.warning("13F XML parse failed for %s", accession_number)
        return None

    filing = ThirteenFFiling(accession_number=accession_number)

    # Try to find info table entries
    entries: list[ET.Element] = []
    for tag_variant in ["infoTable", "informationTable"]:
        entries = root.findall(f".//{{{_13F_TABLE_NS}}}{tag_variant}")
        if entries:
            break
        entries = root.findall(f".//{{*}}{tag_variant}")
        if entries:
            break
        entries = root.findall(f".//{tag_variant}")
        if entries:
            break

    # If no wrapper infoTable found, look directly for individual entries
    if not entries:
        for tag_variant in ["infoTable", "informationTable"]:
            # The root itself might be the table
            if root.tag.endswith(tag_variant) or root.tag == tag_variant:
                entries = [root]
                break

    # Find all info table entries (each represents one holding)
    holding_els: list[ET.Element] = []
    for tag_variant in ["infoTable", "informationTable"]:
        holding_els = root.findall(f".//{{{_13F_TABLE_NS}}}{tag_variant}")
        if holding_els:
            break
        holding_els = root.findall(f".//{{*}}{tag_variant}")
        if holding_els:
            break
        holding_els = root.findall(f".//{tag_variant}")
        if holding_els:
            break

    for hel in holding_els:
        holding = ThirteenFHolding(
            issuer_name=_find_text(hel, "nameOfIssuer"),
            title_of_class=_find_text(hel, "titleOfClass"),
            cusip=_find_text(hel, "cusip"),
            value_thousands=_safe_float(_find_text(hel, "value")),
            put_call=_find_text(hel, "putCall"),
            investment_discretion=_find_text(hel, "investmentDiscretion"),
        )

        # Shares or principal amount
        shares_el = hel.find(f"{{*}}shrsOrPrnAmt")
        if shares_el is None:
            shares_el = hel.find("shrsOrPrnAmt")
        if shares_el is not None:
            holding.shares_or_principal = _safe_float(_find_text(shares_el, "sshPrnamt"))
            holding.shares_or_principal_type = _find_text(shares_el, "sshPrnamtType")

        # Voting authority
        voting_el = hel.find(f"{{*}}votingAuthority")
        if voting_el is None:
            voting_el = hel.find("votingAuthority")
        if voting_el is not None:
            holding.voting_sole = _safe_int(_find_text(voting_el, "Sole"))
            holding.voting_shared = _safe_int(_find_text(voting_el, "Shared"))
            holding.voting_none = _safe_int(_find_text(voting_el, "None"))

        filing.holdings.append(holding)

    filing.entry_count = len(filing.holdings)
    if filing.holdings:
        filing.total_value_thousands = sum(
            h.value_thousands for h in filing.holdings
            if h.value_thousands is not None
        )

    return filing


class ThirteenFHandler:
    """FormHandler implementation for 13F-HR and 13F-HR/A filings."""

    def supports(self, form_type: str) -> bool:
        return bool(_13F_FORM_RE.fullmatch(form_type.upper().strip()))

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> ThirteenFFiling | None:
        if primary_bytes is None:
            return None
        try:
            filing = parse_13f_xml(primary_bytes, accession_number)
            if filing is not None:
                # Enrich from header
                filing.filing_type = (header.form_type or "").strip()
                canonical = header.canonical_issuer()
                if canonical:
                    filing.filer_cik = canonical.cik
                    filing.filer_name = canonical.name
            return filing
        except Exception:
            logger.exception("13F parse failed for %s (non-fatal)", accession_number)
            return None

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        if not isinstance(parsed, ThirteenFFiling):
            return
        filing = parsed
        # Idempotent: delete existing rows
        conn.execute(
            "DELETE FROM thirteenf_holdings WHERE accession_number=?",
            (accession_number,),
        )
        for h in filing.holdings:
            conn.execute(
                """INSERT INTO thirteenf_holdings (
                    accession_number, filer_cik, filer_name,
                    report_period, issuer_name, title_of_class, cusip,
                    value_thousands, shares_or_principal,
                    shares_or_principal_type, investment_discretion,
                    voting_sole, voting_shared, voting_none,
                    put_call, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    accession_number, filing.filer_cik, filing.filer_name,
                    filing.report_period, h.issuer_name, h.title_of_class,
                    h.cusip, h.value_thousands, h.shares_or_principal,
                    h.shares_or_principal_type, h.investment_discretion,
                    h.voting_sole, h.voting_shared, h.voting_none,
                    h.put_call, now_iso,
                ),
            )

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        from event_builders import build_13f_event
        if not isinstance(parsed, ThirteenFFiling):
            return []
        if not parsed.holdings:
            return []
        return [build_13f_event(accession_number, parsed)]
