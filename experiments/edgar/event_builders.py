"""Domain event construction helpers.

These functions build normalised ``EventEnvelope`` objects from parsed
filing data.  They contain **no** persistence or outbox logic — that
stays in ``event_outbox.py``.
"""

from __future__ import annotations

from typing import Any

from event_outbox import EventEnvelope, EventSubjects
from domain import (
    EightKEvent,
    Form4Filing,
)


def build_filing_retrieved_event(
    accession_number: str,
    archive_cik: str,
    form_type: str,
    company_name: str,
    *,
    issuer_cik: str | None = None,
    issuer_name: str | None = None,
    status: str = "retrieved",
    txt_sha256: str | None = None,
    primary_sha256: str | None = None,
    primary_document_url: str | None = None,
    acceptance_datetime: str | None = None,
    filing_date: str | None = None,
    header_form_type: str | None = None,
) -> EventEnvelope:
    subject = (
        EventSubjects.FILING_RETRIEVED
        if status == "retrieved"
        else EventSubjects.FILING_PARTIAL
    )
    return EventEnvelope.new(
        subject=subject,
        accession_number=accession_number,
        payload={
            "archive_cik": archive_cik,
            "form_type": form_type,
            "company_name": company_name,
            "issuer_cik": issuer_cik,
            "issuer_name": issuer_name,
            "status": status,
            "txt_sha256": txt_sha256,
            "primary_sha256": primary_sha256,
            "primary_document_url": primary_document_url,
            "acceptance_datetime": acceptance_datetime,
            "filing_date": filing_date,
            "header_form_type": header_form_type,
        },
        business_key=accession_number,
    )


def build_form4_event(
    accession_number: str,
    form4: Form4Filing,
    out_form4_transactions_cap:int = 20,
    out_form4_owners_cap:int = 10,
) -> EventEnvelope:
    # Build compact transaction summaries for downstream consumers
    txn_summaries = []
    for txn in form4.transactions[:out_form4_transactions_cap]:  # Cap at N to bound payload size
        txn_summaries.append({
            "security_title": txn.security_title,
            "transaction_date": txn.transaction_date,
            "transaction_code": txn.transaction_code,
            "shares": txn.shares,
            "price_per_share": txn.price_per_share,
            "acquired_disposed": txn.acquired_disposed,
            "shares_owned_after": txn.shares_owned_after,
            "is_derivative": txn.is_derivative,
        })
    owner_summaries = []
    for owner in form4.reporting_owners[:out_form4_owners_cap]:
        owner_summaries.append({
            "cik": owner.get("cik") if isinstance(owner, dict) else getattr(owner, "cik", None),
            "name": owner.get("name") if isinstance(owner, dict) else getattr(owner, "name", None),
            "is_director": owner.get("is_director", False) if isinstance(owner, dict) else getattr(owner, "is_director", False),
            "is_officer": owner.get("is_officer", False) if isinstance(owner, dict) else getattr(owner, "is_officer", False),
            "officer_title": owner.get("officer_title") if isinstance(owner, dict) else getattr(owner, "officer_title", None),
            "is_ten_pct_owner": owner.get("is_ten_pct_owner", False) if isinstance(owner, dict) else getattr(owner, "is_ten_pct_owner", False),
        })
    return EventEnvelope.new(
        subject=EventSubjects.FORM4_PARSED,
        accession_number=accession_number,
        payload={
            "issuer_cik": form4.issuer_cik,
            "issuer_name": form4.issuer_name,
            "issuer_ticker": form4.issuer_ticker,
            "transaction_count": len(form4.transactions),
            "holding_count": len(form4.holdings),
            "owner_count": len(form4.reporting_owners),
            "transactions": txn_summaries,
            "reporting_owners": owner_summaries,
        },
        business_key=accession_number,
    )


def build_8k_events(
    accession_number: str,
    events: list[EightKEvent],
) -> list[EventEnvelope]:
    return [
        EventEnvelope.new(
            subject=EventSubjects.EIGHT_K_ITEM,
            accession_number=accession_number,
            payload={
                "item_number": ev.item_number,
                "item_description": ev.item_description,
                "company_name": ev.company_name,
                "cik": ev.cik,
                "filing_date": ev.filing_date,
            },
            business_key=f"{accession_number}:{ev.item_number}",
        )
        for ev in events
    ]


def build_filing_failed_event(
    accession_number: str,
    archive_cik: str,
    form_type: str,
    error: str,
    attempt_no: int = 1,
) -> EventEnvelope:
    return EventEnvelope.new(
        subject=EventSubjects.FILING_FAILED,
        accession_number=accession_number,
        payload={
            "archive_cik": archive_cik,
            "form_type": form_type,
            "error": error[:500],
            "attempt_no": attempt_no,
        },
        business_key=f"{accession_number}:{attempt_no}",
    )


def build_feed_gap_event(
    watermark_ts: str,
    pages_checked: int,
) -> EventEnvelope:
    return EventEnvelope.new(
        subject=EventSubjects.FEED_GAP,
        accession_number="N/A",
        payload={
            "watermark_ts": watermark_ts,
            "pages_checked": pages_checked,
        },
    )
