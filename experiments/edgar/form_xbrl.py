"""XBRL fact extraction for annual/quarterly filings (10-K, 10-Q, 20-F, 40-F, 6-K).

Extracts structured XBRL facts from inline XBRL (iXBRL) HTML documents and
traditional XBRL instance documents (.xml).  Focuses on key financial concepts
commonly used in quantitative research: revenue, net income, EPS, total assets,
total liabilities, operating cash flow, etc.

The parser is intentionally broad — it extracts all us-gaap and ifrs-full facts
rather than cherry-picking, so downstream RL feature pipelines can select what
they need.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Any

from domain import (
    FilingDiscovery,
    SubmissionHeader,
    XBRLFact,
    XBRLFiling,
    _XBRL_ANNUAL_QUARTERLY_RE,
    get_logger,
)


logger = get_logger(__name__)

# Inline XBRL tag patterns (found in HTML documents)
_IXBRL_TAG_RE = re.compile(
    r"<(?:ix|ixt?):(\w+)"
    r"(?:\s[^>]*)?"
    r"(?:contextRef=[\"']([^\"']*)[\"'])?"
    r"(?:\s[^>]*)?"
    r"(?:name=[\"']([^\"']*)[\"'])?"
    r"(?:\s[^>]*)?"
    r"(?:unitRef=[\"']([^\"']*)[\"'])?"
    r"(?:\s[^>]*)?"
    r"(?:decimals=[\"']([^\"']*)[\"'])?"
    r"[^>]*>"
    r"([^<]*)"
    r"</(?:ix|ixt?):\1>",
    re.IGNORECASE | re.DOTALL,
)

# More targeted pattern for ix:nonFraction and ix:nonNumeric
_IXBRL_NONFRAC_RE = re.compile(
    r"<ix:(?:nonFraction|nonNumeric)\s+"
    r"[^>]*?"
    r"name=[\"']([^\"']+)[\"']"
    r"[^>]*?"
    r"(?:contextRef=[\"']([^\"']*)[\"'])?"
    r"[^>]*?"
    r"(?:unitRef=[\"']([^\"']*)[\"'])?"
    r"[^>]*?"
    r"(?:decimals=[\"']([^\"']*)[\"'])?"
    r"[^>]*?>"
    r"([^<]*)"
    r"</ix:(?:nonFraction|nonNumeric)>",
    re.IGNORECASE | re.DOTALL,
)

# Key financial concepts we especially want to capture
_KEY_CONCEPTS = frozenset({
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:NetIncomeLoss",
    "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
    "us-gaap:EarningsPerShareBasic",
    "us-gaap:EarningsPerShareDiluted",
    "us-gaap:Assets",
    "us-gaap:Liabilities",
    "us-gaap:StockholdersEquity",
    "us-gaap:LiabilitiesAndStockholdersEquity",
    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:NetCashProvidedByUsedInInvestingActivities",
    "us-gaap:NetCashProvidedByUsedInFinancingActivities",
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:GrossProfit",
    "us-gaap:CostOfGoodsAndServicesSold",
    "us-gaap:ResearchAndDevelopmentExpense",
    "us-gaap:CommonStockSharesOutstanding",
    "us-gaap:WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
    "us-gaap:LongTermDebt",
    "us-gaap:ShortTermBorrowings",
    "ifrs-full:Revenue",
    "ifrs-full:ProfitLoss",
    "ifrs-full:BasicEarningsLossPerShare",
    "ifrs-full:DilutedEarningsLossPerShare",
    "ifrs-full:Assets",
    "ifrs-full:Liabilities",
    "ifrs-full:Equity",
})

# Prefixes we care about for general extraction
_INTERESTING_PREFIXES = ("us-gaap:", "ifrs-full:", "dei:")


def _safe_float(text: str | None) -> float | None:
    if not text:
        return None
    # Remove common iXBRL formatting
    cleaned = text.strip().replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
    if cleaned in ("", "-", "—", "–"):
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_xbrl_instance(xml_bytes: bytes, accession_number: str) -> list[XBRLFact]:
    """Parse a traditional XBRL instance document (.xml)."""
    facts: list[XBRLFact] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.debug("XBRL instance XML parse failed for %s", accession_number)
        return facts

    for el in root.iter():
        tag = el.tag
        # Strip namespace URI but keep prefix mapping
        if "}" in tag:
            ns_uri, local = tag.split("}", 1)
            ns_uri = ns_uri.lstrip("{")
            # Map common namespace URIs to prefixes
            prefix = ""
            if "us-gaap" in ns_uri:
                prefix = "us-gaap:"
            elif "ifrs-full" in ns_uri:
                prefix = "ifrs-full:"
            elif "dei" in ns_uri:
                prefix = "dei:"
            else:
                continue  # Skip non-interesting namespaces
            concept = f"{prefix}{local}"
        else:
            continue

        if not el.text or not el.text.strip():
            continue

        context_ref = el.get("contextRef")
        unit_ref = el.get("unitRef")
        decimals = el.get("decimals")
        value = el.text.strip()

        fact = XBRLFact(
            concept=concept,
            value=value,
            numeric_value=_safe_float(value),
            unit=unit_ref,
            decimals=decimals,
            context_id=context_ref,
        )
        facts.append(fact)

    return facts


def _parse_ixbrl(html_bytes: bytes, accession_number: str) -> list[XBRLFact]:
    """Parse inline XBRL facts from an HTML document."""
    facts: list[XBRLFact] = []
    text = html_bytes.decode("utf-8", errors="replace")

    for m in _IXBRL_NONFRAC_RE.finditer(text):
        concept = m.group(1)
        context_ref = m.group(2)
        unit_ref = m.group(3)
        decimals = m.group(4)
        value = m.group(5).strip()

        if not value:
            continue

        # Filter to interesting prefixes
        if not any(concept.startswith(p) for p in _INTERESTING_PREFIXES):
            continue

        fact = XBRLFact(
            concept=concept,
            value=value,
            numeric_value=_safe_float(value),
            unit=unit_ref,
            decimals=decimals,
            context_id=context_ref,
        )
        facts.append(fact)

    return facts


def parse_xbrl_filing(
    primary_bytes: bytes,
    accession_number: str,
    header: SubmissionHeader,
    discovery: FilingDiscovery,
) -> XBRLFiling | None:
    """Parse XBRL facts from the primary document (XML instance or iXBRL HTML)."""
    filing = XBRLFiling(
        accession_number=accession_number,
        form_type=(header.form_type or "").strip(),
    )

    canonical = header.canonical_issuer()
    if canonical:
        filing.filer_cik = canonical.cik
        filing.filer_name = canonical.name

    if discovery.filing_date:
        filing.period_of_report = discovery.filing_date.isoformat()

    # Try XML instance parse first
    facts = _parse_xbrl_instance(primary_bytes, accession_number)

    # If no facts found, try iXBRL (HTML)
    if not facts:
        facts = _parse_ixbrl(primary_bytes, accession_number)

    if not facts:
        logger.debug("no XBRL facts found for %s", accession_number)
        return None

    filing.facts = facts
    return filing


class XBRLHandler:
    """FormHandler for XBRL-bearing annual/quarterly filings."""

    def supports(self, form_type: str) -> bool:
        return bool(_XBRL_ANNUAL_QUARTERLY_RE.fullmatch(form_type.upper().strip()))

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> XBRLFiling | None:
        if primary_bytes is None:
            return None
        try:
            return parse_xbrl_filing(primary_bytes, accession_number, header, discovery)
        except Exception:
            logger.exception("XBRL parse failed for %s (non-fatal)", accession_number)
            return None

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        if not isinstance(parsed, XBRLFiling):
            return
        filing = parsed
        conn.execute(
            "DELETE FROM xbrl_facts WHERE accession_number=?",
            (accession_number,),
        )
        for fact in filing.facts:
            conn.execute(
                """INSERT INTO xbrl_facts (
                    accession_number, filer_cik, form_type,
                    period_of_report, concept, value, numeric_value,
                    unit, decimals, context_id, period_start,
                    period_end, period_instant, segment, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    accession_number, filing.filer_cik, filing.form_type,
                    filing.period_of_report, fact.concept, fact.value,
                    fact.numeric_value, fact.unit, fact.decimals,
                    fact.context_id, fact.period_start, fact.period_end,
                    fact.period_instant, fact.segment, now_iso,
                ),
            )

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        from event_builders import build_xbrl_event
        if not isinstance(parsed, XBRLFiling):
            return []
        if not parsed.facts:
            return []
        return [build_xbrl_event(accession_number, parsed)]
