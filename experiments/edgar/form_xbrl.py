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
from html.parser import HTMLParser
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

# iXBRL element local names that represent facts
_IXBRL_FACT_TAGS = frozenset({"nonfraction", "nonnumeric"})
# Tags that can continue a fact's text content
_IXBRL_CONTINUATION_TAG = "continuation"

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


def _parse_xbrl_contexts(root: ET.Element) -> dict[str, dict[str, str | None]]:
    """Parse XBRL context elements to extract period information.

    Returns a dict keyed by context ID, each containing:
      period_start, period_end, period_instant, segment
    """
    contexts: dict[str, dict[str, str | None]] = {}
    # Search for context elements with any namespace
    for ctx_el in root.iter():
        tag = ctx_el.tag
        local_name = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if local_name != "context":
            continue

        ctx_id = ctx_el.get("id")
        if not ctx_id:
            continue

        info: dict[str, str | None] = {
            "period_start": None,
            "period_end": None,
            "period_instant": None,
            "segment": None,
        }

        # Find period child — may be namespaced
        for child in ctx_el:
            child_local = child.tag.rsplit("}", 1)[-1] if "}" in child.tag else child.tag
            if child_local == "period":
                for period_child in child:
                    pc_local = period_child.tag.rsplit("}", 1)[-1] if "}" in period_child.tag else period_child.tag
                    if pc_local == "startDate" and period_child.text:
                        info["period_start"] = period_child.text.strip()
                    elif pc_local == "endDate" and period_child.text:
                        info["period_end"] = period_child.text.strip()
                    elif pc_local == "instant" and period_child.text:
                        info["period_instant"] = period_child.text.strip()
            elif child_local == "entity":
                for entity_child in child:
                    ec_local = entity_child.tag.rsplit("}", 1)[-1] if "}" in entity_child.tag else entity_child.tag
                    if ec_local == "segment":
                        # Extract dimension member text as a simple string
                        parts = []
                        for dim_el in entity_child:
                            dim_val = dim_el.text.strip() if dim_el.text else ""
                            dim_attr = dim_el.get("dimension", "")
                            if dim_attr and dim_val:
                                parts.append(f"{dim_attr}={dim_val}")
                            elif dim_val:
                                parts.append(dim_val)
                        if parts:
                            info["segment"] = "; ".join(parts)

        contexts[ctx_id] = info

    return contexts


def _resolve_fact_periods(
    facts: list[XBRLFact],
    contexts: dict[str, dict[str, str | None]],
) -> None:
    """Apply resolved context periods onto fact objects in-place."""
    for fact in facts:
        if not fact.context_id:
            continue
        ctx = contexts.get(fact.context_id)
        if not ctx:
            continue
        fact.period_start = ctx.get("period_start")
        fact.period_end = ctx.get("period_end")
        fact.period_instant = ctx.get("period_instant")
        if not fact.segment:
            fact.segment = ctx.get("segment")


def _extract_period_of_report(facts: list[XBRLFact]) -> str | None:
    """Find dei:DocumentPeriodEndDate in facts for the true reporting period.

    This is the correct source for period_of_report in a quant system —
    filing_date and report period are not interchangeable.  Backtests
    align fundamentals to report periods, and factor construction keys
    by fiscal quarter end, not submission date.
    """
    for fact in facts:
        if fact.concept in (
            "dei:DocumentPeriodEndDate",
            "dei:CurrentFiscalYearEndDate",
        ):
            if fact.value and len(fact.value) >= 10:
                return fact.value[:10]  # "YYYY-MM-DD"
    return None


def _parse_xbrl_instance(xml_bytes: bytes, accession_number: str) -> tuple[list[XBRLFact], dict[str, dict[str, str | None]]]:
    """Parse a traditional XBRL instance document (.xml).

    Returns (facts, contexts) tuple so the caller can resolve periods.
    """
    facts: list[XBRLFact] = []
    contexts: dict[str, dict[str, str | None]] = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.debug("XBRL instance XML parse failed for %s", accession_number)
        return facts, contexts

    # Parse contexts first
    contexts = _parse_xbrl_contexts(root)

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

    return facts, contexts


class _IXBRLParser(HTMLParser):
    """HTMLParser subclass that extracts iXBRL facts from inline XBRL HTML.

    Handles:
    - Attributes in any order (contextRef, name, unitRef, decimals, etc.)
    - Nested markup inside fact elements (collects all inner text recursively)
    - ``ix:continuation`` elements linked via ``continuedAt`` / ``id`` attrs
    - ``scale`` and ``sign`` attributes on ix:nonFraction
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Completed raw facts: list of dicts with attrs + collected text
        self.raw_facts: list[dict[str, str | None]] = []
        # Continuation blocks keyed by their id
        self.continuations: dict[str, str] = {}

        # --- parser state ---
        # Stack depth for the current fact element (handles nested ix tags)
        self._fact_depth: int = 0
        self._fact_attrs: dict[str, str | None] = {}
        self._fact_text_parts: list[str] = []
        self._in_fact: bool = False
        # ix:continuation tracking
        self._in_continuation: bool = False
        self._continuation_depth: int = 0
        self._continuation_id: str | None = None
        self._continuation_parts: list[str] = []
        # General nesting depth for any ix: tag (to track close tags)
        self._ix_tag_stack: list[str] = []

    @staticmethod
    def _is_ix_tag(tag: str) -> tuple[bool, str]:
        """Return (is_ix_namespace, local_name_lower) for a tag."""
        if ":" in tag:
            prefix, local = tag.split(":", 1)
            if prefix.lower() in ("ix", "ixt"):
                return True, local.lower()
        return False, tag.lower()

    # --- HTMLParser callbacks ---

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        is_ix, local = self._is_ix_tag(tag)

        if self._in_fact:
            self._fact_depth += 1
            return

        if self._in_continuation:
            self._continuation_depth += 1
            return

        if not is_ix:
            return

        if local in _IXBRL_FACT_TAGS:
            attr_dict = {k.lower(): v for k, v in attrs}
            self._in_fact = True
            self._fact_depth = 1
            self._fact_attrs = {
                "tag_type": local,
                "name": attr_dict.get("name"),
                "contextref": attr_dict.get("contextref"),
                "unitref": attr_dict.get("unitref"),
                "decimals": attr_dict.get("decimals"),
                "scale": attr_dict.get("scale"),
                "sign": attr_dict.get("sign"),
                "format": attr_dict.get("format"),
                "continuedat": attr_dict.get("continuedat"),
                "id": attr_dict.get("id"),
            }
            self._fact_text_parts = []
        elif local == _IXBRL_CONTINUATION_TAG:
            attr_dict = {k.lower(): v for k, v in attrs}
            self._in_continuation = True
            self._continuation_depth = 1
            self._continuation_id = attr_dict.get("id")
            self._continuation_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_fact:
            self._fact_depth -= 1
            if self._fact_depth <= 0:
                self._in_fact = False
                self._fact_attrs["_text"] = "".join(self._fact_text_parts)
                self.raw_facts.append(self._fact_attrs)
                self._fact_attrs = {}
                self._fact_text_parts = []
            return

        if self._in_continuation:
            self._continuation_depth -= 1
            if self._continuation_depth <= 0:
                self._in_continuation = False
                cid = self._continuation_id
                if cid:
                    self.continuations[cid] = "".join(self._continuation_parts)
                self._continuation_id = None
                self._continuation_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_fact:
            self._fact_text_parts.append(data)
        elif self._in_continuation:
            self._continuation_parts.append(data)


def _apply_ixbrl_scaling(value_str: str, scale: str | None, sign: str | None) -> str:
    """Apply iXBRL scale and sign transformations to a numeric value string.

    ``scale`` is an integer exponent: the raw value is multiplied by 10**scale.
    ``sign`` of ``"-"`` negates the value.
    """
    if not scale and not sign:
        return value_str
    numeric = _safe_float(value_str)
    if numeric is None:
        return value_str
    if scale:
        try:
            numeric = numeric * (10 ** int(scale))
        except (ValueError, TypeError):
            pass
    if sign == "-":
        numeric = -abs(numeric)
    # Format without unnecessary trailing zeros
    if numeric == int(numeric):
        return str(int(numeric))
    return str(numeric)


def _resolve_continuations(raw_facts: list[dict[str, str | None]], continuations: dict[str, str]) -> None:
    """Follow ``continuedAt`` chains and append continuation text to facts in-place."""
    for fact in raw_facts:
        cont_id = fact.get("continuedat")
        parts: list[str] = []
        seen: set[str] = set()
        while cont_id and cont_id not in seen:
            seen.add(cont_id)
            text = continuations.get(cont_id, "")
            if text:
                parts.append(text)
            # continuations themselves can chain — not common, but spec-valid
            # (we don't track chained continuedAt here; would need richer data)
            break
        if parts:
            existing = fact.get("_text") or ""
            fact["_text"] = existing + " ".join(parts)


def _parse_ixbrl(html_bytes: bytes, accession_number: str) -> list[XBRLFact]:
    """Parse inline XBRL facts from an HTML document using structured HTML parsing."""
    facts: list[XBRLFact] = []
    text = html_bytes.decode("utf-8", errors="replace")

    parser = _IXBRLParser()
    try:
        parser.feed(text)
    except Exception:
        logger.debug("iXBRL HTML parse failed for %s", accession_number)
        return facts

    # Resolve continuations
    _resolve_continuations(parser.raw_facts, parser.continuations)

    for raw in parser.raw_facts:
        concept = raw.get("name")
        if not concept:
            continue

        # Filter to interesting prefixes
        if not any(concept.startswith(p) for p in _INTERESTING_PREFIXES):
            continue

        value = (raw.get("_text") or "").strip()
        if not value:
            continue

        # Apply iXBRL scale/sign for numeric facts
        tag_type = raw.get("tag_type")
        if tag_type == "nonfraction":
            value = _apply_ixbrl_scaling(value, raw.get("scale"), raw.get("sign"))

        fact = XBRLFact(
            concept=concept,
            value=value,
            numeric_value=_safe_float(value),
            unit=raw.get("unitref"),
            decimals=raw.get("decimals"),
            context_id=raw.get("contextref"),
        )
        facts.append(fact)

    return facts


def parse_xbrl_filing(
    primary_bytes: bytes,
    accession_number: str,
    header: SubmissionHeader,
    discovery: FilingDiscovery,
) -> XBRLFiling | None:
    """Parse XBRL facts from the primary document (XML instance or iXBRL HTML).

    Period resolution:
      - Parses XBRL context elements to resolve true reporting periods
      - Uses dei:DocumentPeriodEndDate for period_of_report (fiscal period end)
      - Falls back to discovery.filing_date only as a last resort
      - Populates fact-level period_start, period_end, period_instant from contexts
    """
    filing = XBRLFiling(
        accession_number=accession_number,
        form_type=(header.form_type or "").strip(),
    )

    canonical = header.canonical_issuer()
    if canonical:
        filing.filer_cik = canonical.cik
        filing.filer_name = canonical.name

    # Try XML instance parse first (returns facts + contexts)
    facts, contexts = _parse_xbrl_instance(primary_bytes, accession_number)

    # If no facts found, try iXBRL (HTML)
    if not facts:
        facts = _parse_ixbrl(primary_bytes, accession_number)
        # For iXBRL, attempt to parse contexts from the HTML as well
        # (iXBRL contexts use the same xbrli:context XML elements embedded in HTML)
        if facts and not contexts:
            try:
                # Extract context elements from iXBRL HTML by finding XML-like sections
                html_text = primary_bytes.decode("utf-8", errors="replace")
                # Try parsing contexts from any embedded XML-like content
                import re as _re
                # Find xbrli:context blocks in the HTML
                ctx_pattern = _re.compile(
                    r'<(?:xbrli:)?context\s+id=["\']([^"\']+)["\'][^>]*>.*?</(?:xbrli:)?context>',
                    _re.DOTALL | _re.IGNORECASE,
                )
                ctx_xml = "<root>" + "".join(ctx_pattern.findall(primary_bytes.decode("utf-8", errors="replace"))) + "</root>"
                # Actually, let's try a different approach - wrap the whole HTML
                # and parse contexts from it
                for ctx_m in ctx_pattern.finditer(html_text):
                    ctx_block = ctx_m.group(0)
                    try:
                        ctx_root = ET.fromstring(f"<root xmlns:xbrli='http://www.xbrl.org/2003/instance'>{ctx_block}</root>")
                        sub_contexts = _parse_xbrl_contexts(ctx_root)
                        contexts.update(sub_contexts)
                    except ET.ParseError:
                        pass
            except Exception:
                logger.debug("iXBRL context parsing failed for %s (non-fatal)", accession_number)

    if not facts:
        logger.debug("no XBRL facts found for %s", accession_number)
        return None

    # Resolve fact-level period fields from contexts
    if contexts:
        _resolve_fact_periods(facts, contexts)

    # Derive period_of_report from dei:DocumentPeriodEndDate (the true
    # fiscal period end), NOT from discovery.filing_date which is the
    # SEC submission date.  These are semantically different: backtests
    # align fundamentals to report periods, and factor construction
    # keys by fiscal quarter end, not submission date.
    period = _extract_period_of_report(facts)
    if period:
        filing.period_of_report = period
    elif discovery.filing_date:
        # Last resort fallback — clearly label this as filing_date-derived
        filing.period_of_report = discovery.filing_date.isoformat()
        logger.debug(
            "XBRL period_of_report fell back to filing_date for %s "
            "(dei:DocumentPeriodEndDate not found)",
            accession_number,
        )

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