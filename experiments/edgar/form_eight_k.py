"""8-K item extraction and handler implementation."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from domain import (
    EightKEvent,
    FilingDiscovery,
    SubmissionHeader,
    get_logger,
)


logger = get_logger(__name__)

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
# 8-K handler for the registry
# ---------------------------------------------------------------------------

class EightKHandler:
    """FormHandler implementation for 8-K and 8-K/A filings."""

    def supports(self, form_type: str) -> bool:
        return form_type.upper().strip().startswith("8-K")

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> list[EightKEvent] | None:
        try:
            pdoc_body_text: str | None = None
            if primary_bytes is not None:
                pdoc_body_text = primary_bytes.decode("utf-8", errors="replace")

            effective_filing_date: str | None = None
            if discovery.filing_date:
                effective_filing_date = discovery.filing_date.isoformat()
            elif header.filed_as_of_date:
                effective_filing_date = header.filed_as_of_date

            events = parse_8k_items(
                header, accession_number,
                company_name=discovery.company_name,
                cik=discovery.archive_cik,
                filing_date=effective_filing_date,
                primary_doc_body=pdoc_body_text,
            )
            return events or None
        except Exception:
            logger.exception("8-K parse failed for %s (non-fatal)", accession_number)
            return None

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        events: list[EightKEvent] = parsed if isinstance(parsed, list) else []
        # Always delete existing 8-K events (idempotent reparse)
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
                 ev.filing_date, ev.company_name, ev.cik, now_iso),
            )

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        from event_builders import build_8k_events
        events: list[EightKEvent] = parsed if isinstance(parsed, list) else []
        if not events:
            return []
        return build_8k_events(accession_number, events)
