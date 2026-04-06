"""Fund/ETF filing parsing and handler implementation.

Handles N-PORT (portfolio holdings), N-CEN (census), 497 (prospectus supplements),
and 485 (post-effective amendments) form families.

N-PORT is the highest-value form here — it contains fund-level portfolio
holdings with CUSIP/ISIN, balance, value, asset category, and more.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Any

from domain import (
    FilingDiscovery,
    FundFiling,
    FundHolding,
    SubmissionHeader,
    _FUND_FORM_RE,
    get_logger,
)


logger = get_logger(__name__)


def _safe_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def _find_text(el: ET.Element | None, tag: str) -> str | None:
    """Find element text, trying bare and wildcard-namespaced."""
    if el is None:
        return None
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    child = el.find(f"{{*}}{tag}")
    if child is not None and child.text:
        return child.text.strip()
    # Try nested path with wildcard
    if "/" in tag and "{" not in tag:
        parts = tag.split("/")
        ns_parts = [f"{{*}}{p}" if p not in ("", ".") else p for p in parts]
        child = el.find("/".join(ns_parts))
        if child is not None and child.text:
            return child.text.strip()
    return None


def _parse_nport_xml(xml_bytes: bytes, accession_number: str) -> FundFiling | None:
    """Parse an N-PORT XML filing to extract fund holdings."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.warning("N-PORT XML parse failed for %s", accession_number)
        return None

    filing = FundFiling(accession_number=accession_number)

    # General info
    gen_info = root.find(".//{*}genInfo")
    if gen_info is not None:
        filing.series_id = _find_text(gen_info, "seriesId")
        filing.series_name = _find_text(gen_info, "seriesName")
        filing.class_id = _find_text(gen_info, "classId")
        filing.report_date = _find_text(gen_info, "repPd")

    # Fund info — total/net assets
    fund_info = root.find(".//{*}fundInfo")
    if fund_info is not None:
        filing.total_assets = _safe_float(_find_text(fund_info, "totAssets"))
        filing.net_assets = _safe_float(_find_text(fund_info, "netAssets"))

    # Holdings (invstOrSecs)
    inv_els = root.findall(".//{*}invstOrSec")
    if not inv_els:
        inv_els = root.findall(".//invstOrSec")

    for inv_el in inv_els:
        holding = FundHolding(
            issuer_name=_find_text(inv_el, "name"),
            title=_find_text(inv_el, "title"),
            cusip=_find_text(inv_el, "cusip"),
            isin=_find_text(inv_el, "isin"),
            lei=_find_text(inv_el, "lei"),
            balance=_safe_float(_find_text(inv_el, "balance")),
            units=_find_text(inv_el, "units"),
            value_usd=_safe_float(_find_text(inv_el, "valUSD")),
            pct_of_nav=_safe_float(_find_text(inv_el, "pctVal")),
            asset_category=_find_text(inv_el, "assetCat"),
            issuer_category=_find_text(inv_el, "issuerCat"),
            country=_find_text(inv_el, "invCountry"),
            currency=_find_text(inv_el, "curCd"),
        )

        # Restricted flag
        is_restricted = _find_text(inv_el, "isRestrictedSec")
        holding.is_restricted = is_restricted in ("Y", "true", "1") if is_restricted else False

        filing.holdings.append(holding)

    filing.holding_count = len(filing.holdings)
    return filing


def _parse_fund_text(
    text: str,
    accession_number: str,
    header: SubmissionHeader,
    discovery: FilingDiscovery,
) -> FundFiling:
    """Minimal parse for non-N-PORT fund filings (497, 485, N-CEN).

    These filings are primarily text/HTML; we capture metadata but not
    full structured data.  The value for the event pipeline is the filing
    detection and metadata, not deep content extraction.
    """
    filing = FundFiling(
        accession_number=accession_number,
        form_type=(header.form_type or "").strip(),
    )
    canonical = header.canonical_issuer()
    if canonical:
        filing.filer_cik = canonical.cik
        filing.filer_name = canonical.name
    if discovery.filing_date:
        filing.report_date = discovery.filing_date.isoformat()
    return filing


class FundHandler:
    """FormHandler implementation for fund/ETF filings (N-PORT, N-CEN, 497, 485)."""

    def supports(self, form_type: str) -> bool:
        return bool(_FUND_FORM_RE.fullmatch(form_type.upper().strip()))

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> FundFiling | None:
        if primary_bytes is None:
            return None
        try:
            form_upper = (header.form_type or "").upper().strip()
            filing: FundFiling | None = None

            # N-PORT filings are XML-structured — do NOT fall back to
            # the generic text parser on failure, because that would
            # silently produce a metadata-only FundFiling that is
            # indistinguishable from a successful parse with zero
            # holdings, contaminating downstream datasets.
            if form_upper.startswith("N-PORT"):
                filing = _parse_nport_xml(primary_bytes, accession_number)
                if filing is None:
                    logger.warning(
                        "N-PORT XML parse returned None for %s; "
                        "returning parse failure (no text fallback)",
                        accession_number,
                    )
                    return None
            else:
                # Non-N-PORT fund forms (497, 485, N-CEN) use text parse
                text = primary_bytes.decode("utf-8", errors="replace")
                filing = _parse_fund_text(text, accession_number, header, discovery)

            if filing is not None:
                filing.form_type = form_upper
                canonical = header.canonical_issuer()
                if canonical:
                    filing.filer_cik = canonical.cik
                    filing.filer_name = canonical.name

            return filing
        except Exception:
            logger.exception("Fund form parse failed for %s (non-fatal)", accession_number)
            return None

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        if not isinstance(parsed, FundFiling):
            return
        f = parsed

        # Fund filing metadata
        conn.execute(
            "DELETE FROM fund_filings WHERE accession_number=?",
            (accession_number,),
        )
        conn.execute(
            """INSERT INTO fund_filings (
                accession_number, form_type, filer_cik, filer_name,
                series_id, series_name, class_id, report_date,
                total_assets, net_assets, holding_count, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                accession_number, f.form_type, f.filer_cik, f.filer_name,
                f.series_id, f.series_name, f.class_id, f.report_date,
                f.total_assets, f.net_assets, f.holding_count, now_iso,
            ),
        )

        # Fund holdings (N-PORT)
        conn.execute(
            "DELETE FROM fund_holdings WHERE accession_number=?",
            (accession_number,),
        )
        for h in f.holdings:
            conn.execute(
                """INSERT INTO fund_holdings (
                    accession_number, issuer_name, title, cusip, isin,
                    lei, balance, units, value_usd, pct_of_nav,
                    asset_category, issuer_category, country, currency,
                    is_restricted, maturity_date, coupon_rate, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    accession_number, h.issuer_name, h.title, h.cusip,
                    h.isin, h.lei, h.balance, h.units, h.value_usd,
                    h.pct_of_nav, h.asset_category, h.issuer_category,
                    h.country, h.currency, int(h.is_restricted),
                    h.maturity_date, h.coupon_rate, now_iso,
                ),
            )

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        from event_builders import build_fund_event
        if not isinstance(parsed, FundFiling):
            return []
        return [build_fund_event(accession_number, parsed)]