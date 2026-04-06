"""Form 3/4/5 (ownership) parsing and handler implementation."""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from typing import Any

from domain import (
    EightKEvent,
    FilingDiscovery,
    Form4Filing,
    Form4Holding,
    Form4Transaction,
    SubmissionHeader,
    _OWNERSHIP_FORM_RE,
    get_logger,
)


logger = get_logger(__name__)

def _safe_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip())
    except (ValueError, TypeError):
        return None


def _safe_bool(text: str | None) -> bool:
    if not text:
        return False
    return text.strip() in ("1", "true", "True", "yes")


def _el_text(parent: ET.Element | None, tag: str) -> str | None:
    if parent is None:
        return None
    # Try the tag as-is first (handles no-namespace and XPath-style tags)
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    # If the tag is a simple name (no namespace, no path operators),
    # try the wildcard namespace prefix so we match any namespace.
    if "{" not in tag and "/" not in tag and "." not in tag:
        el = parent.find(f"{{*}}{tag}")
        if el is not None and el.text:
            return el.text.strip()
    # For XPath-style tags like ".//securityTitle/value", try
    # replacing each path component with a wildcard-namespaced version.
    if "/" in tag and "{" not in tag:
        parts = tag.split("/")
        ns_parts = []
        for part in parts:
            if part == "" or part == ".":
                ns_parts.append(part)
            else:
                ns_parts.append(f"{{*}}{part}")
        ns_tag = "/".join(ns_parts)
        el = parent.find(ns_tag)
        if el is not None and el.text:
            return el.text.strip()
    return None


def parse_form4_xml(xml_bytes: bytes, accession_number: str) -> Form4Filing | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.warning("ownership form XML parse failed for %s", accession_number)
        return None

    filing = Form4Filing(accession_number=accession_number)

    issuer_el = root.find(".//issuer")
    if issuer_el is None:
        issuer_el = root.find(".//{*}issuer")
    if issuer_el is not None:
        filing.issuer_cik = _el_text(issuer_el, "issuerCik")
        filing.issuer_name = _el_text(issuer_el, "issuerName")
        filing.issuer_ticker = _el_text(issuer_el, "issuerTradingSymbol")

    owner_els = root.findall(".//reportingOwner")
    if not owner_els:
        owner_els = root.findall(".//{*}reportingOwner")
    for owner_el in owner_els:
        owner_id = owner_el.find("reportingOwnerId")
        if owner_id is None:
            owner_id = owner_el.find("{*}reportingOwnerId")
        owner_rel = owner_el.find("reportingOwnerRelationship")
        if owner_rel is None:
            owner_rel = owner_el.find("{*}reportingOwnerRelationship")
        owner_info: dict[str, Any] = {
            "cik": _el_text(owner_id, "rptOwnerCik"),
            "name": _el_text(owner_id, "rptOwnerName"),
            "is_director": _safe_bool(_el_text(owner_rel, "isDirector")),
            "is_officer": _safe_bool(_el_text(owner_rel, "isOfficer")),
            "officer_title": _el_text(owner_rel, "officerTitle"),
            "is_ten_pct_owner": _safe_bool(_el_text(owner_rel, "isTenPercentOwner")),
        }
        filing.reporting_owners.append(owner_info)

    table = root.find(".//nonDerivativeTable")
    if table is None:
        table = root.find(".//{*}nonDerivativeTable")
    if table is not None:
        txn_els = table.findall("nonDerivativeTransaction")
        if not txn_els:
            txn_els = table.findall("{*}nonDerivativeTransaction")
        for txn_el in txn_els:
            txns = _parse_form4_transaction(txn_el, filing, is_derivative=False)
            filing.transactions.extend(txns)
        hold_els = table.findall("nonDerivativeHolding")
        if not hold_els:
            hold_els = table.findall("{*}nonDerivativeHolding")
        for hold_el in hold_els:
            holdings = _parse_form4_holding(hold_el, filing, is_derivative=False)
            filing.holdings.extend(holdings)

    table = root.find(".//derivativeTable")
    if table is None:
        table = root.find(".//{*}derivativeTable")
    if table is not None:
        txn_els = table.findall("derivativeTransaction")
        if not txn_els:
            txn_els = table.findall("{*}derivativeTransaction")
        for txn_el in txn_els:
            txns = _parse_form4_transaction(txn_el, filing, is_derivative=True)
            filing.transactions.extend(txns)
        hold_els = table.findall("derivativeHolding")
        if not hold_els:
            hold_els = table.findall("{*}derivativeHolding")
        for hold_el in hold_els:
            holdings = _parse_form4_holding(hold_el, filing, is_derivative=True)
            filing.holdings.extend(holdings)

    fn_els = root.findall(".//footnote")
    if not fn_els:
        fn_els = root.findall(".//{*}footnote")
    for fn_el in fn_els:
        fn_id = fn_el.get("id", "")
        fn_text = fn_el.text or ""
        if fn_id:
            filing.footnotes[fn_id] = fn_text.strip()

    return filing


def _parse_form4_transaction(
    txn_el: ET.Element, filing: Form4Filing, *, is_derivative: bool,
) -> list[Form4Transaction]:
    """Parse a single transaction element into one row **per** reporting owner.

    SEC Form 3/4/5 filings can list multiple reporting owners (joint
    filings).  The prior implementation always attributed every
    transaction to ``reporting_owners[0]``, silently misattributing
    multi-owner filings.  We now emit one ``Form4Transaction`` per owner
    so downstream analytics correctly reflect each owner's activity.
    """
    sec_title = _el_text(txn_el, ".//securityTitle/value") or _el_text(txn_el, "securityTitle")
    txn_date = _el_text(txn_el, ".//transactionDate/value") or _el_text(txn_el, "transactionDate")
    amounts = txn_el.find(".//transactionAmounts")
    if amounts is None:
        amounts = txn_el.find(".//{*}transactionAmounts")
    coding = txn_el.find(".//transactionCoding")
    if coding is None:
        coding = txn_el.find(".//{*}transactionCoding")
    post = txn_el.find(".//postTransactionAmounts")
    if post is None:
        post = txn_el.find(".//{*}postTransactionAmounts")
    ownership = txn_el.find(".//ownershipNature")
    if ownership is None:
        ownership = txn_el.find(".//{*}ownershipNature")

    txn_code = _el_text(coding, "transactionCode") if coding is not None else None
    shares_raw = _el_text(amounts, ".//transactionShares/value") if amounts is not None else None
    price_raw = _el_text(amounts, ".//transactionPricePerShare/value") if amounts is not None else None
    acq_disp = _el_text(amounts, ".//transactionAcquiredDisposedCode/value") if amounts is not None else None
    shares_after = _el_text(post, ".//sharesOwnedFollowingTransaction/value") if post is not None else None
    direct_indirect = _el_text(ownership, ".//directOrIndirectOwnership/value") if ownership is not None else None

    owners = filing.reporting_owners if filing.reporting_owners else [{}]
    results: list[Form4Transaction] = []
    for owner in owners:
        results.append(Form4Transaction(
            issuer_cik=filing.issuer_cik, issuer_name=filing.issuer_name,
            issuer_ticker=filing.issuer_ticker,
            reporting_owner_cik=owner.get("cik"),
            reporting_owner_name=owner.get("name"),
            is_director=owner.get("is_director", False),
            is_officer=owner.get("is_officer", False),
            officer_title=owner.get("officer_title"),
            is_ten_pct_owner=owner.get("is_ten_pct_owner", False),
            security_title=sec_title, transaction_date=txn_date,
            transaction_code=txn_code, shares=_safe_float(shares_raw),
            price_per_share=_safe_float(price_raw),
            acquired_disposed=acq_disp,
            shares_owned_after=_safe_float(shares_after),
            direct_indirect=direct_indirect,
            is_derivative=is_derivative,
        ))
    return results


def _parse_form4_holding(
    hold_el: ET.Element, filing: Form4Filing, *, is_derivative: bool,
) -> list[Form4Holding]:
    """Parse a single holding element into one row **per** reporting owner.

    See ``_parse_form4_transaction`` for the multi-owner rationale.
    """
    sec_title = _el_text(hold_el, ".//securityTitle/value") or _el_text(hold_el, "securityTitle")
    post = hold_el.find(".//postTransactionAmounts")
    if post is None:
        post = hold_el.find(".//{*}postTransactionAmounts")
    shares_owned = _el_text(post, ".//sharesOwnedFollowingTransaction/value") if post is not None else None
    ownership = hold_el.find(".//ownershipNature")
    if ownership is None:
        ownership = hold_el.find(".//{*}ownershipNature")
    direct_indirect = _el_text(ownership, ".//directOrIndirectOwnership/value") if ownership is not None else None
    nature_text = _el_text(ownership, ".//natureOfOwnership/value") if ownership is not None else None

    owners = filing.reporting_owners if filing.reporting_owners else [{}]
    results: list[Form4Holding] = []
    for owner in owners:
        results.append(Form4Holding(
            issuer_cik=filing.issuer_cik, issuer_name=filing.issuer_name,
            issuer_ticker=filing.issuer_ticker,
            reporting_owner_cik=owner.get("cik"),
            reporting_owner_name=owner.get("name"),
            is_director=owner.get("is_director", False),
            is_officer=owner.get("is_officer", False),
            officer_title=owner.get("officer_title"),
            is_ten_pct_owner=owner.get("is_ten_pct_owner", False),
            security_title=sec_title, shares_owned=_safe_float(shares_owned),
            direct_indirect=direct_indirect, nature_of_ownership=nature_text,
            is_derivative=is_derivative,
        ))
    return results



# ---------------------------------------------------------------------------
# Form 4 handler for the registry
# ---------------------------------------------------------------------------

class Form4Handler:
    """FormHandler implementation for SEC ownership forms (3, 4, 5 and amendments)."""

    def supports(self, form_type: str) -> bool:
        return bool(_OWNERSHIP_FORM_RE.fullmatch(form_type.upper().strip()))

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> Form4Filing | None:
        if primary_bytes is None:
            return None
        try:
            return parse_form4_xml(primary_bytes, accession_number)
        except Exception:
            logger.exception("Form 4 parse failed for %s (non-fatal)", accession_number)
            return None

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        if not isinstance(parsed, Form4Filing):
            return
        filing = parsed
        # Transactions — always delete existing for idempotent reparse
        conn.execute(
            "DELETE FROM form4_transactions WHERE accession_number=?",
            (accession_number,),
        )
        for txn in filing.transactions:
            conn.execute(
                """INSERT INTO form4_transactions (
                    accession_number, issuer_cik, issuer_name, issuer_ticker,
                    reporting_owner_cik, reporting_owner_name,
                    is_director, is_officer, officer_title, is_ten_pct_owner,
                    security_title, transaction_date, transaction_code,
                    shares, price_per_share, acquired_disposed,
                    shares_owned_after, direct_indirect, is_derivative, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    accession_number, txn.issuer_cik, txn.issuer_name,
                    txn.issuer_ticker, txn.reporting_owner_cik,
                    txn.reporting_owner_name, int(txn.is_director),
                    int(txn.is_officer), txn.officer_title,
                    int(txn.is_ten_pct_owner), txn.security_title,
                    txn.transaction_date, txn.transaction_code,
                    txn.shares, txn.price_per_share, txn.acquired_disposed,
                    txn.shares_owned_after, txn.direct_indirect,
                    int(txn.is_derivative), now_iso,
                ),
            )
        # Holdings — unconditional delete
        conn.execute(
            "DELETE FROM form4_holdings WHERE accession_number=?",
            (accession_number,),
        )
        for h in filing.holdings:
            conn.execute(
                """INSERT INTO form4_holdings (
                    accession_number, issuer_cik, issuer_name, issuer_ticker,
                    reporting_owner_cik, reporting_owner_name,
                    is_director, is_officer, officer_title, is_ten_pct_owner,
                    security_title, shares_owned, direct_indirect,
                    nature_of_ownership, is_derivative, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    accession_number, h.issuer_cik, h.issuer_name,
                    h.issuer_ticker, h.reporting_owner_cik,
                    h.reporting_owner_name, int(h.is_director),
                    int(h.is_officer), h.officer_title,
                    int(h.is_ten_pct_owner), h.security_title,
                    h.shares_owned, h.direct_indirect,
                    h.nature_of_ownership, int(h.is_derivative), now_iso,
                ),
            )

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        from event_builders import build_form4_event
        if not isinstance(parsed, Form4Filing):
            return []
        form4 = parsed
        if not (form4.transactions or form4.holdings):
            return []
        cap_txn = kwargs.get("out_form4_transactions_cap", 20)
        cap_own = kwargs.get("out_form4_owners_cap", 10)
        return [build_form4_event(accession_number, form4, cap_txn, cap_own)]
