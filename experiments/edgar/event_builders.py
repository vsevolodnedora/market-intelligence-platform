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
    EightKExhibitFact,
    Form4Filing,
    ThirteenFFiling,
    ThirteenFNoticeFiling,
    ThirteenDGFiling,
    XBRLFiling,
    FundFiling,
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


def build_8k_facts_event(
    accession_number: str,
    facts: list[EightKExhibitFact],
) -> EventEnvelope:
    """Build an event for deep-extracted 8-K exhibit/body facts."""
    fact_summaries = []
    for f in facts[:30]:  # Cap payload size
        fact_summaries.append({
            "item_number": f.item_number,
            "fact_type": f.fact_type,
            "fact_key": f.fact_key,
            "fact_value": f.fact_value,
            "fact_numeric": f.fact_numeric,
            "currency": f.currency,
        })
    return EventEnvelope.new(
        subject=EventSubjects.EIGHT_K_FACTS,
        accession_number=accession_number,
        payload={
            "fact_count": len(facts),
            "facts": fact_summaries,
            "company_name": facts[0].company_name if facts else None,
            "cik": facts[0].cik if facts else None,
            "filing_date": facts[0].filing_date if facts else None,
        },
        business_key=accession_number,
    )


def build_13f_event(
    accession_number: str,
    filing: ThirteenFFiling,
) -> EventEnvelope:
    """Build an event for a parsed 13F-HR institutional holdings filing."""
    # Top holdings by value for the event payload
    top_holdings = sorted(
        filing.holdings,
        key=lambda h: h.value_thousands or 0,
        reverse=True,
    )[:20]
    return EventEnvelope.new(
        subject=EventSubjects.THIRTEEN_F_PARSED,
        accession_number=accession_number,
        payload={
            "filer_cik": filing.filer_cik,
            "filer_name": filing.filer_name,
            "report_period": filing.report_period,
            "total_value_thousands": filing.total_value_thousands,
            "entry_count": filing.entry_count,
            "top_holdings": [
                {
                    "issuer_name": h.issuer_name,
                    "cusip": h.cusip,
                    "value_thousands": h.value_thousands,
                    "shares": h.shares_or_principal,
                }
                for h in top_holdings
            ],
        },
        business_key=accession_number,
    )


def build_13f_notice_event(
    accession_number: str,
    filing: ThirteenFNoticeFiling,
) -> EventEnvelope:
    """Build an event for a 13F-NT notice filing.

    Unlike the holdings event, the notice payload contains only filer
    identity and report period — there are no holdings to include.
    """
    return EventEnvelope.new(
        subject=EventSubjects.THIRTEEN_F_NT_PARSED,
        accession_number=accession_number,
        payload={
            "filer_cik": filing.filer_cik,
            "filer_name": filing.filer_name,
            "report_period": filing.report_period,
            "filing_type": filing.filing_type,
            "filing_date": filing.filing_date,
            "is_amendment": filing.is_amendment,
        },
        business_key=accession_number,
    )


def build_13dg_event(
    accession_number: str,
    filing: ThirteenDGFiling,
) -> EventEnvelope:
    """Build an event for a parsed SC 13D/G activist filing."""
    return EventEnvelope.new(
        subject=EventSubjects.THIRTEEN_DG_PARSED,
        accession_number=accession_number,
        payload={
            "form_type": filing.form_type,
            "filer_cik": filing.filer_cik,
            "filer_name": filing.filer_name,
            "subject_cik": filing.subject_cik,
            "subject_name": filing.subject_name,
            "subject_cusip": filing.subject_cusip,
            "ownership_percent": filing.ownership_percent,
            "shares_beneficially_owned": filing.shares_beneficially_owned,
            "is_amendment": filing.is_amendment,
            "amendment_number": filing.amendment_number,
            "date_of_event": filing.date_of_event,
            "filing_date": filing.filing_date,
        },
        business_key=accession_number,
    )


def build_xbrl_event(
    accession_number: str,
    filing: XBRLFiling,
) -> EventEnvelope:
    """Build an event for parsed XBRL facts from annual/quarterly filings.

    The payload includes only key financial concepts to keep event size
    bounded.  Full fact data is available in the xbrl_facts table.
    """
    # Key concepts for the event payload
    _KEY_PREFIXES = (
        "us-gaap:Revenue", "us-gaap:NetIncomeLoss",
        "us-gaap:EarningsPerShare", "us-gaap:Assets",
        "us-gaap:Liabilities", "us-gaap:StockholdersEquity",
        "us-gaap:OperatingIncomeLoss", "us-gaap:GrossProfit",
        "us-gaap:CashAndCashEquivalents",
        "us-gaap:NetCashProvided",
        "ifrs-full:Revenue", "ifrs-full:ProfitLoss",
        "ifrs-full:Assets", "ifrs-full:Equity",
    )
    key_facts = []
    for fact in filing.facts:
        if any(fact.concept.startswith(p) for p in _KEY_PREFIXES):
            key_facts.append({
                "concept": fact.concept,
                "value": fact.value,
                "numeric_value": fact.numeric_value,
                "unit": fact.unit,
                "context_id": fact.context_id,
            })
        if len(key_facts) >= 50:
            break

    return EventEnvelope.new(
        subject=EventSubjects.XBRL_PARSED,
        accession_number=accession_number,
        payload={
            "form_type": filing.form_type,
            "filer_cik": filing.filer_cik,
            "filer_name": filing.filer_name,
            "period_of_report": filing.period_of_report,
            "total_fact_count": len(filing.facts),
            "key_facts": key_facts,
        },
        business_key=accession_number,
    )


def build_fund_event(
    accession_number: str,
    filing: FundFiling,
) -> EventEnvelope:
    """Build an event for a parsed fund/ETF filing."""
    # Top holdings by value for N-PORT
    top_holdings = sorted(
        filing.holdings,
        key=lambda h: h.value_usd or 0,
        reverse=True,
    )[:20]

    # Distinguish full holdings parse (N-PORT) from metadata-only
    # (497, 485, N-CEN) so downstream consumers never confuse
    # "no holdings extracted" with "holdings not applicable".
    form_upper = (filing.form_type or "").upper()
    if form_upper.startswith("N-PORT"):
        parse_status = "complete"
    else:
        parse_status = "metadata_only"

    return EventEnvelope.new(
        subject=EventSubjects.FUND_FILING_PARSED,
        accession_number=accession_number,
        payload={
            "form_type": filing.form_type,
            "filer_cik": filing.filer_cik,
            "filer_name": filing.filer_name,
            "series_id": filing.series_id,
            "series_name": filing.series_name,
            "report_date": filing.report_date,
            "total_assets": filing.total_assets,
            "net_assets": filing.net_assets,
            "holding_count": filing.holding_count,
            "parse_status": parse_status,
            "top_holdings": [
                {
                    "issuer_name": h.issuer_name,
                    "cusip": h.cusip,
                    "isin": h.isin,
                    "value_usd": h.value_usd,
                    "pct_of_nav": h.pct_of_nav,
                    "asset_category": h.asset_category,
                }
                for h in top_holdings
            ],
        },
        business_key=accession_number,
    )