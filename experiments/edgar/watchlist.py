"""Watchlist loading, CIK/name index, and header-gate resolver.

Watchlist data and issuer-resolution logic are independent from storage
and transport — this is a natural home for future ETF/fund entity-matching.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from domain import (
    ACTIVIST_FORMS,
    FilingParty,
    RelevanceState,
    SubmissionHeader,
    WatchlistCompany,
    _OWNERSHIP_FORM_RE,
    _validate_cik,
    get_logger,
    normalize_name,
)


logger = get_logger(__name__)

_WATCHLIST_REQUIRED = {"cik", "ticker", "name"}

def load_watchlist_yaml(path: Path) -> list[WatchlistCompany]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "companies" not in raw:
        raise ValueError(
            f"Watchlist YAML must have a top-level 'companies' key: {path}"
        )
    entries = raw["companies"]
    if not isinstance(entries, list):
        raise ValueError(f"'companies' must be a list: {path}")

    companies: list[WatchlistCompany] = []
    seen_ciks: set[str] = set()
    skipped_missing = 0
    skipped_invalid_cik = 0
    skipped_duplicate = 0

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.warning(
                "watchlist entry %d is not a mapping, skipping: %r",
                i,
                entry,
            )
            skipped_missing += 1
            continue

        # --- Check for missing or empty mandatory fields -----------------
        entry_name = entry.get("name") or entry.get("raw_name") or ""
        entry_label = entry_name or f"<entry {i}>"

        missing = _WATCHLIST_REQUIRED - entry.keys()
        if missing:
            logger.warning(
                "watchlist entry %d (%s) missing required fields %s, skipping",
                i,
                entry_label,
                missing,
            )
            skipped_missing += 1
            continue

        raw_cik = entry.get("cik")
        if raw_cik is None or str(raw_cik).strip() in ("", "None", "null"):
            logger.warning(
                "watchlist entry %d (%s) has no CIK (non-SEC), skipping for EDGAR monitoring",
                i,
                entry_label,
            )
            skipped_invalid_cik += 1
            continue

        try:
            cik = _validate_cik(str(raw_cik))
        except (ValueError, TypeError) as exc:
            logger.warning(
                "watchlist entry %d (%s) has invalid CIK %r: %s, skipping",
                i,
                entry_label,
                raw_cik,
                exc,
            )
            skipped_invalid_cik += 1
            continue

        # Ticker may be None for entries enriched from a global ETF universe
        raw_ticker = entry.get("ticker")
        if not raw_ticker or str(raw_ticker).strip() in ("", "None", "null"):
            logger.warning(
                "watchlist entry %d (%s, CIK=%s) has no ticker, skipping",
                i,
                entry_label,
                cik,
            )
            skipped_missing += 1
            continue

        raw_name = str(entry.get("name") or "").strip()
        if not raw_name:
            logger.warning(
                "watchlist entry %d (CIK=%s) has empty name, skipping",
                i,
                cik,
            )
            skipped_missing += 1
            continue

        # Duplicate CIK
        if cik in seen_ciks:
            logger.warning(
                "watchlist entry %d (%s) has duplicate CIK %s, skipping",
                i,
                entry_label,
                cik,
            )
            skipped_duplicate += 1
            continue
        seen_ciks.add(cik)

        # Build valid entry
        extra = {
            k: v
            for k, v in entry.items()
            if k not in _WATCHLIST_REQUIRED and k != "isin" and k != "aliases"
        }
        raw_aliases = entry.get("aliases", [])
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        aliases = tuple(str(a) for a in raw_aliases if a)

        companies.append(
            WatchlistCompany(
                cik=cik,
                ticker=str(raw_ticker),
                name=raw_name,
                aliases=aliases,
                metadata=extra,
            )
        )

    if skipped_missing or skipped_invalid_cik or skipped_duplicate:
        logger.info(
            "watchlist filtering: skipped %d missing-fields, %d invalid/absent CIK, "
            "%d duplicate CIK out of %d total entries",
            skipped_missing,
            skipped_invalid_cik,
            skipped_duplicate,
            len(entries),
        )

    if not companies:
        raise ValueError(f"Watchlist has no valid entries for EDGAR monitoring: {path}")

    logger.info("loaded %d unique companies from watchlist %s", len(companies), path)
    return companies


class WatchlistIndex:
    """O(1) lookup by CIK and normalized name (including aliases)."""

    def __init__(self, companies: list[WatchlistCompany]) -> None:
        self._by_cik: dict[str, WatchlistCompany] = {}
        self._by_name: dict[str, WatchlistCompany] = {}
        for c in companies:
            self._by_cik[c.cik] = c
            self._by_name[c.name_normalized] = c
            # Index all aliases alongside the canonical name.
            for alias in c.aliases:
                norm_alias = normalize_name(alias)
                if norm_alias and norm_alias not in self._by_name:
                    self._by_name[norm_alias] = c

    def __len__(self) -> int:
        return len(self._by_cik)

    def match_cik(self, cik: str | None) -> WatchlistCompany | None:
        if not cik:
            return None
        return self._by_cik.get(cik.zfill(10))

    def match_name(self, name: str | None) -> WatchlistCompany | None:
        if not name:
            return None
        return self._by_name.get(normalize_name(name))

    def contains_cik(self, cik: str | None) -> bool:
        if not cik:
            return False
        return cik.zfill(10) in self._by_cik

    @property
    def all_ciks(self) -> frozenset[str]:
        return frozenset(self._by_cik.keys())

    @property
    def companies(self) -> list[WatchlistCompany]:
        return list(self._by_cik.values())


# ---------------------------------------------------------------------------
# Header-gate resolver
# ---------------------------------------------------------------------------

class HeaderResolver:
    def __init__(self, watchlist: WatchlistIndex) -> None:
        self.watchlist = watchlist

    def resolve(
        self, header: SubmissionHeader,
    ) -> tuple[RelevanceState, WatchlistCompany | None, FilingParty | None]:
        canonical = header.canonical_issuer()
        form_type = (header.form_type or "").upper().strip()

        if form_type in ACTIVIST_FORMS and canonical is None:
            return RelevanceState.UNRESOLVED, None, None

        if canonical is None:
            return RelevanceState.HDR_FAILED, None, None

        if canonical.cik:
            match = self.watchlist.match_cik(canonical.cik)
            if match:
                return RelevanceState.HDR_MATCH, match, canonical
            return RelevanceState.IRRELEVANT, None, canonical

        if canonical.name:
            match = self.watchlist.match_name(canonical.name)
            if match:
                return RelevanceState.HDR_MATCH, match, canonical

        return RelevanceState.IRRELEVANT, None, canonical

