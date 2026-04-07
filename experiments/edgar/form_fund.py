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
    _NPORT_FORM_RE,
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

        # Debt-instrument fields (maturity date and coupon rate)
        # These live under <debtSec> in the N-PORT schema but may also
        # appear directly on the investment element in some filings.
        holding.maturity_date = (
            _find_text(inv_el, "debtSec/maturityDt")
            or _find_text(inv_el, "maturityDt")
        )
        coupon_raw = (
            _find_text(inv_el, "debtSec/couponKind/couponRate")
            or _find_text(inv_el, "debtSec/couponRate")
            or _find_text(inv_el, "couponRate")
        )
        holding.coupon_rate = _safe_float(coupon_raw)

        filing.holdings.append(holding)

    filing.holding_count = len(filing.holdings)

    # Guard: if the XML parsed successfully but contained no recognisable
    # N-PORT structure (no genInfo, no fundInfo, no holdings), this is not
    # an N-PORT document — return None so the caller can try other paths.
    has_nport_structure = (
        gen_info is not None
        or fund_info is not None
        or filing.holdings
    )
    if not has_nport_structure:
        logger.info(
            "XML parsed for %s but contained no N-PORT structure "
            "(no genInfo/fundInfo/holdings); returning None",
            accession_number,
        )
        return None

    filing.parse_source = "xml"
    filing.parse_status = "complete"
    return filing


# ---------------------------------------------------------------------------
# HTML fallback for SEC-rendered N-PORT filings
# ---------------------------------------------------------------------------

# Regex patterns for extracting N-PORT data from SEC HTML renderings.
# SEC's EDGAR viewer renders N-PORT XML into HTML tables with
# recognisable label/value patterns that mirror the XML element names.

_HTML_LABEL_VALUE_RE = re.compile(
    r"<t[dh][^>]*>\s*(?:<[^>]+>\s*)*([^<]+?)(?:\s*<[^>]+>)*\s*</t[dh]>"
    r"\s*"
    r"<t[dh][^>]*>\s*(?:<[^>]+>\s*)*([^<]*?)(?:\s*<[^>]+>)*\s*</t[dh]>",
    re.IGNORECASE | re.DOTALL,
)

# Broader table-row extraction for holdings tables.
_HTML_TR_RE = re.compile(
    r"<tr[^>]*>(.*?)</tr>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TD_RE = re.compile(
    r"<t[dh][^>]*>\s*(.*?)\s*</t[dh]>",
    re.IGNORECASE | re.DOTALL,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    return _HTML_TAG_RE.sub("", text).strip()


def _parse_nport_html_fallback(
    raw_bytes: bytes,
    accession_number: str,
) -> FundFiling | None:
    """Extract N-PORT holdings from SEC-rendered HTML when XML parse fails.

    This is a best-effort fallback for HTML/XML-like filings that
    ElementTree cannot parse.  It returns a FundFiling only when it
    extracts at least fund identity or report date AND at least one
    holdings row — otherwise returns None to preserve fail-closed
    semantics.
    """
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None

    filing = FundFiling(accession_number=accession_number)
    filing.parse_source = "html_fallback"

    # --- Extract metadata from label/value pairs ---
    _META_MAP = {
        "series id": "series_id",
        "seriesid": "series_id",
        "series name": "series_name",
        "seriesname": "series_name",
        "class id": "class_id",
        "classid": "class_id",
        "report date": "report_date",
        "reporting period": "report_date",
        "reppd": "report_date",
        "total assets": "_total_assets",
        "totassets": "_total_assets",
        "net assets": "_net_assets",
        "netassets": "_net_assets",
    }

    for m in _HTML_LABEL_VALUE_RE.finditer(text):
        label = _strip_html(m.group(1)).lower().strip().rstrip(":")
        value = _strip_html(m.group(2)).strip()
        if not value:
            continue
        attr = _META_MAP.get(label)
        if attr == "_total_assets":
            filing.total_assets = _safe_float(value)
        elif attr == "_net_assets":
            filing.net_assets = _safe_float(value)
        elif attr is not None:
            setattr(filing, attr, value)

    # --- Extract holdings from table rows ---
    # Strategy: find the table that has a header row containing N-PORT
    # holding column names (name, cusip, balance, value, etc.) and parse
    # subsequent rows as holdings.
    _HOLDING_HEADERS = {
        "name", "cusip", "isin", "balance", "value",
        "valussd", "valusd", "title", "lei",
    }

    header_indices: dict[str, int] = {}
    in_holdings_table = False
    holdings: list[FundHolding] = []

    for tr_match in _HTML_TR_RE.finditer(text):
        row_html = tr_match.group(1)
        cells = [_strip_html(c.group(1)) for c in _HTML_TD_RE.finditer(row_html)]
        if not cells:
            continue

        # Detect header row
        lower_cells = [c.lower().strip() for c in cells]
        header_hits = sum(1 for c in lower_cells if c in _HOLDING_HEADERS)
        if header_hits >= 3:
            header_indices = {c: i for i, c in enumerate(lower_cells)}
            in_holdings_table = True
            continue

        if not in_holdings_table:
            continue

        # Empty row or new section — stop collecting
        if not any(cells):
            if holdings:
                break
            continue

        def _cell(name: str) -> str | None:
            idx = header_indices.get(name)
            if idx is None or idx >= len(cells):
                return None
            v = cells[idx].strip()
            return v if v else None

        issuer_name = _cell("name")
        cusip = _cell("cusip")
        # Require at least one identifier per row
        if not issuer_name and not cusip:
            continue

        holding = FundHolding(
            issuer_name=issuer_name,
            title=_cell("title"),
            cusip=cusip,
            isin=_cell("isin"),
            lei=_cell("lei"),
            balance=_safe_float(_cell("balance")),
            units=_cell("units"),
            value_usd=_safe_float(
                _cell("valussd") or _cell("valusd") or _cell("value")
            ),
            pct_of_nav=_safe_float(_cell("pctval") or _cell("pct_of_nav")),
            asset_category=_cell("assetcat") or _cell("asset_category"),
            issuer_category=_cell("issuercat") or _cell("issuer_category"),
            country=_cell("invcountry") or _cell("country"),
            currency=_cell("curcd") or _cell("currency"),
        )

        is_restricted = _cell("isrestrictedsec") or _cell("is_restricted")
        holding.is_restricted = is_restricted in ("Y", "true", "1") if is_restricted else False

        holding.maturity_date = _cell("maturitydt") or _cell("maturity_date")
        holding.coupon_rate = _safe_float(
            _cell("couponrate") or _cell("coupon_rate")
        )

        holdings.append(holding)

    # --- Fail-closed gate: require identity/date AND holdings ---
    has_identity = bool(filing.series_id or filing.series_name or filing.report_date)
    if not has_identity or not holdings:
        logger.info(
            "N-PORT HTML fallback for %s did not meet minimum extraction "
            "threshold (identity=%s, holdings=%d); returning None",
            accession_number,
            has_identity,
            len(holdings),
        )
        return None

    filing.holdings = holdings
    filing.holding_count = len(holdings)
    # HTML fallback is inherently partial — table extraction may miss
    # fields that the XML path would capture.
    filing.parse_status = "partial"

    logger.info(
        "N-PORT HTML fallback extracted %d holdings for %s",
        len(holdings),
        accession_number,
    )
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
    """FormHandler implementation for N-PORT fund filings.

    Only N-PORT filings contain structured holdings data (CUSIP/ISIN,
    balance, value, asset category) that can be meaningfully parsed.
    Other fund form families (N-CEN, 497*, 485*) are text/HTML documents
    without structured content and are not routed through this handler.
    They are still discovered and streamed by the ingestor but not
    structurally parsed.
    """

    def supports(self, form_type: str) -> bool:
        return bool(_NPORT_FORM_RE.fullmatch(form_type.upper().strip()))

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
            # N-PORT filings are XML-structured — try XML first.
            filing = _parse_nport_xml(primary_bytes, accession_number)
            if filing is None:
                # XML parse failed or produced no recognisable structure.
                # Try the HTML fallback for SEC-rendered filings before
                # giving up.  The fallback enforces its own minimum-
                # extraction gate (identity + holdings) so it will not
                # silently produce a metadata-only FundFiling.
                logger.info(
                    "N-PORT XML parse returned None for %s; "
                    "attempting HTML fallback",
                    accession_number,
                )
                filing = _parse_nport_html_fallback(primary_bytes, accession_number)
                if filing is None:
                    logger.warning(
                        "N-PORT HTML fallback also failed for %s; "
                        "returning parse failure",
                        accession_number,
                    )
                    return None

            form_upper = (header.form_type or "").upper().strip()
            filing.form_type = form_upper
            canonical = header.canonical_issuer()
            if canonical:
                filing.filer_cik = canonical.cik
                filing.filer_name = canonical.name

            return filing
        except Exception:
            logger.exception("N-PORT parse failed for %s (non-fatal)", accession_number)
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