from __future__ import annotations

"""Resolve SEC CIK values for companies listed in a YAML file.

This version is intentionally CIK-only. It removes the broader metadata-enrichment
logic from the original script and keeps the pipeline focused on one task:
producing a validated 10-digit CIK for each company.

Resolution strategy (highest confidence first):
1. Validate an already-present CIK against SEC submissions metadata.
2. Map explicit identifiers (CUSIP, then ISIN) to equity instruments via OpenFIGI,
   keeping the top 3–5 rows per identifier for better recall on foreign issuers.
3. Convert the resulting ticker/name hints into SEC CIK candidates using SEC's
   company_tickers_exchange index and cik-lookup-data.txt.
4. Fall back to exact/fuzzy company-name matches in local SEC reference data,
   using an inverted token index with recall variants for robust foreign-name
   and transliteration matching.
5. Fall back to SEC's public EDGAR company search by name, querying with
   multiple name variants (HTML-decoded, transliterated, abbreviation-expanded).
6. Validate every candidate against SEC submissions metadata before accepting it,
   with separate acceptance thresholds for identifier-backed vs name-only matches.

The script updates only the ``cik`` field and leaves every other field untouched.
"""

import argparse
import dataclasses
import difflib
import hashlib
import html as html_module
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
DEFAULT_SEC_USER_AGENT = "cik-only-enricher/2.0 (set SEC_USER_AGENT to 'Your Company contact@example.com')"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SEC_CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

_US_FIGI_EXCHANGE_CODES = {"US", "UA", "UN", "UQ", "UR", "UW", "UP", "UM"}
_GENERIC_NAME_WORDS = {
    "A", "ADR", "AG", "AND", "ASA", "B", "CLASS", "CO", "COM", "COMPANY",
    "CORP", "CORPORATION", "DE", "GROUP", "HOLDING", "HOLDINGS", "INC",
    "INCORPORATED", "INTL", "INTERNATIONAL", "LIMITED", "LTD", "LLC", "NV",
    "OF", "ORD", "ORDINARY", "PLC", "SA", "SE", "SPA", "THE",
}

# ---------------------------------------------------------------------------
# Recall-variant constants (used only for broadening search, never for final
# acceptance).  See §2 and §3 of the improvement spec.
# ---------------------------------------------------------------------------

_RECALL_SUFFIX_WORDS = {
    "REG", "ORD", "NEW", "EO", "DL", "SHS", "SHARES", "UNSP",
    "REGISTERED", "BEARER", "NOMINAL", "NAMENS", "INHABER",
    "VINKULIERT", "VINK", "NA", "BR",
}

_TRANSLITERATION_PAIRS: list[tuple[str, str]] = [
    ("UE", "U"),
    ("OE", "O"),
    ("AE", "A"),
]

_ABBREVIATION_MAP: dict[str, str] = {
    "RUECKVER": "RUCKVERSICHERUNGS",
    "RUCKVER": "RUCKVERSICHERUNGS",
    "INTL": "INTERNATIONAL",
    "MFG": "MANUFACTURING",
    "HLDGS": "HOLDINGS",
    "HLDG": "HOLDING",
    "GRP": "GROUP",
    "SVCS": "SERVICES",
    "SVC": "SERVICE",
    "TECH": "TECHNOLOGY",
    "TECHS": "TECHNOLOGIES",
    "FIN": "FINANCIAL",
    "FINL": "FINANCIAL",
    "PHARMA": "PHARMACEUTICAL",
    "CHEM": "CHEMICAL",
    "ELEC": "ELECTRIC",
    "ELECTR": "ELECTRONIC",
    "NAT": "NATIONAL",
    "NATL": "NATIONAL",
    "MGT": "MANAGEMENT",
    "MGMT": "MANAGEMENT",
    "DEV": "DEVELOPMENT",
    "PROP": "PROPERTIES",
    "RES": "RESOURCES",
    "COMM": "COMMUNICATIONS",
    "TELECOMM": "TELECOMMUNICATIONS",
    "BANCSHRS": "BANCSHARES",
    "SYS": "SYSTEMS",
    "SOLS": "SOLUTIONS",
    "INDS": "INDUSTRIES",
    "PRODS": "PRODUCTS",
    "PETRO": "PETROLEUM",
    "ENTMT": "ENTERTAINMENT",
    "ENGY": "ENERGY",
    "PWR": "POWER",
    "INS": "INSURANCE",
    "ASSUR": "ASSURANCE",
}

_RECALL_CLASS_RE = re.compile(r"\bCLASS\s+[A-Z0-9]\b")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_CIK_LOOKUP_LINE_RE = re.compile(r"^(?P<name>.+?):(?P<cik>\d{1,10}):$")
_BROWSE_EDGAR_LINK_RE = re.compile(
    r'href=["\'](?:https?://www\.sec\.gov)?/'
    r'(?:edgar/browse/\?CIK=|cgi-bin/browse-edgar\?[^"\']*CIK=)'
    r'(?P<cik>\d{1,10})[^"\']*["\'][^>]*>(?P<name>.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclasses.dataclass(slots=True)
class Config:
    input_path: Path
    output_path: Path
    cache_dir: Path
    log_level: str = "INFO"
    log_path: Optional[Path] = None
    sec_user_agent: str = DEFAULT_SEC_USER_AGENT
    openfigi_api_key: Optional[str] = None
    sec_min_interval_seconds: float = 0.15
    sec_reference_ttl_hours: int = 24
    submissions_ttl_hours: int = 24 * 7
    browse_fallback_enabled: bool = True
    browse_max_results: int = 30
    max_figi_hints: int = 5


@dataclasses.dataclass(slots=True)
class CompanyRecord:
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.raw.get("name") or "").strip()

    @property
    def ticker(self) -> str:
        return normalize_ticker(self.raw.get("ticker"))

    @property
    def cusip(self) -> str:
        return normalize_identifier(self.raw.get("cusip"))

    @property
    def isin(self) -> str:
        return normalize_identifier(self.raw.get("isin"))

    @property
    def existing_cik(self) -> str:
        return sec_zfill_cik(self.raw.get("cik"))


@dataclasses.dataclass(slots=True)
class SecEntity:
    cik: str
    name: str
    ticker: str = ""
    exchange: str = ""
    source: str = ""


@dataclasses.dataclass(slots=True)
class SubmissionProfile:
    cik: str
    name: str
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]
    former_names: tuple[str, ...]

    @classmethod
    def from_api(cls, cik: str, payload: dict[str, Any]) -> "SubmissionProfile":
        former_names: list[str] = []
        for item in payload.get("formerNames") or []:
            if isinstance(item, dict) and item.get("name"):
                former_names.append(str(item["name"]))
        return cls(
            cik=sec_zfill_cik(cik),
            name=str(payload.get("name") or "").strip(),
            tickers=tuple(normalize_ticker(t) for t in payload.get("tickers") or [] if t),
            exchanges=tuple(str(e).strip() for e in payload.get("exchanges") or [] if e),
            former_names=tuple(str(n).strip() for n in former_names if n),
        )

    @property
    def all_names(self) -> tuple[str, ...]:
        names = [self.name, *self.former_names]
        return tuple(n for n in names if n)


@dataclasses.dataclass(slots=True)
class FigiHint:
    id_type: str
    id_value: str
    ticker: str
    name: str
    exch_code: str
    market_sector: str
    security_type: str
    score: float


@dataclasses.dataclass(slots=True)
class Candidate:
    cik: str
    score: float
    evidence: list[str]
    identifier_backed: bool = False

    def add(self, score: float, evidence: str) -> None:
        self.score += score
        self.evidence.append(evidence)


@dataclasses.dataclass(slots=True)
class ResolutionResult:
    cik: Optional[str]
    confidence: float
    rationale: str


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(level: str, log_path: Optional[Path]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


logger = logging.getLogger("cik_only_enricher")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def normalize_identifier(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def sec_zfill_cik(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits.zfill(10) if digits else ""


def normalize_ticker(value: Any) -> str:
    if value in (None, ""):
        return ""
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def normalize_name(value: Any) -> str:
    """Canonical name normalization for matching and validation.

    Applies HTML-entity decoding, uppercasing, punctuation collapse,
    and legal/generic-suffix removal.
    """
    raw = html_module.unescape(str(value or "")).upper()
    raw = raw.replace("&", " AND ").replace("/", " ").replace("-", " ")
    raw = _NON_ALNUM_RE.sub(" ", raw)
    words = [w for w in raw.split() if w and w not in _GENERIC_NAME_WORDS and not w.isdigit()]
    return " ".join(words)


def recall_name_variants(name: str) -> list[str]:
    """Generate additional normalized name forms for broadening search recall.

    These variants are used *only* to surface candidates from the inverted
    index and EDGAR browse.  They are never used for final acceptance scoring.
    """
    decoded = html_module.unescape(str(name or ""))
    original_norm = normalize_name(name)

    # Step 1: strip holding-label suffixes before normalizing
    cleaned = decoded.upper()
    for suffix in _RECALL_SUFFIX_WORDS:
        cleaned = re.sub(rf"\b{re.escape(suffix)}\b", " ", cleaned)
    cleaned = _RECALL_CLASS_RE.sub(" ", cleaned)
    base_norm = normalize_name(cleaned)

    variants: set[str] = set()
    if base_norm and base_norm != original_norm:
        variants.add(base_norm)

    # Step 2: transliteration  (UE→U, OE→O, AE→A)
    for source_text in [original_norm, base_norm]:
        if not source_text:
            continue
        trans = source_text
        for src, dst in _TRANSLITERATION_PAIRS:
            trans = trans.replace(src, dst)
        if trans != source_text:
            variants.add(trans)

    # Step 3: abbreviation expansion applied to every variant seen so far
    for variant in variants | {original_norm}:
        if not variant:
            continue
        tokens = variant.split()
        new_tokens = [_ABBREVIATION_MAP.get(t, t) for t in tokens]
        expanded = " ".join(new_tokens)
        if expanded != variant:
            variants.add(expanded)

    variants.discard(original_norm)
    return [v for v in variants if v]


def name_similarity(left: Any, right: Any) -> float:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    seq = difflib.SequenceMatcher(None, a, b).ratio()
    a_set = set(a.split())
    b_set = set(b.split())
    overlap = len(a_set & b_set) / max(1, min(len(a_set), len(b_set)))
    prefix_bonus = 0.0
    if a.startswith(b) or b.startswith(a):
        prefix_bonus = 0.08
    return min(1.0, max(seq, 0.60 * seq + 0.40 * overlap) + prefix_bonus)


def best_name_score(
    query_name: str,
    candidate_names: Iterable[str],
    *,
    use_recall_variants: bool = False,
) -> float:
    """Return the best name-similarity between *query_name* (optionally with
    recall variants) and any of *candidate_names*."""
    query_forms = [query_name]
    if use_recall_variants:
        query_forms.extend(recall_name_variants(query_name))
    best = 0.0
    for qf in query_forms:
        for cn in candidate_names:
            sim = name_similarity(qf, cn)
            if sim > best:
                best = sim
                if best >= 1.0:
                    return 1.0
    return best


def name_threshold(name: str) -> float:
    tokens = normalize_name(name).split()
    if len(tokens) <= 1:
        return 0.95
    if len(tokens) == 2:
        return 0.92
    return 0.88


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def html_strip(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value).split())


def choose_best_name_match(
    query: str,
    entities: Iterable[SecEntity],
    limit: int = 6,
    use_recall: bool = False,
) -> list[tuple[SecEntity, float]]:
    scored: list[tuple[SecEntity, float]] = []
    seen: set[str] = set()
    query_forms = [query]
    if use_recall:
        query_forms.extend(recall_name_variants(query))
    for entity in entities:
        if entity.cik in seen:
            continue
        seen.add(entity.cik)
        best = max(name_similarity(qf, entity.name) for qf in query_forms)
        scored.append((entity, best))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [item for item in scored[:limit] if item[1] > 0]


# ---------------------------------------------------------------------------
# HTTP and cache helpers
# ---------------------------------------------------------------------------


class JsonFileCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str, suffix: str = ".json") -> Path:
        hashed = sha1_text(key)
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{hashed}{suffix}"

    def get_json(self, namespace: str, key: str, max_age_seconds: Optional[int]) -> Optional[Any]:
        path = self._path(namespace, key, ".json")
        if not path.exists():
            return None
        if max_age_seconds is not None:
            age = time.time() - path.stat().st_mtime
            if age > max_age_seconds:
                return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        path = self._path(namespace, key, ".json")
        path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")

    def get_text(self, namespace: str, key: str, max_age_seconds: Optional[int]) -> Optional[str]:
        path = self._path(namespace, key, ".txt")
        if not path.exists():
            return None
        if max_age_seconds is not None:
            age = time.time() - path.stat().st_mtime
            if age > max_age_seconds:
                return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def set_text(self, namespace: str, key: str, value: str) -> None:
        path = self._path(namespace, key, ".txt")
        path.write_text(value, encoding="utf-8")


class HttpClient:
    def __init__(self, user_agent: str):
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": user_agent})

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.session.get(url, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        response = self.session.get(url, timeout=30, **kwargs)
        response.raise_for_status()
        return response.text

    def post_json(self, url: str, json_payload: Any, headers: Optional[dict[str, str]] = None) -> Any:
        response = self.session.post(url, json=json_payload, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()


class SecClientProtocol(Protocol):
    def get_tickers_exchange(self) -> dict[str, Any]: ...
    def get_cik_lookup_text(self) -> str: ...
    def get_submissions(self, cik: str) -> dict[str, Any]: ...
    def browse_company(self, query: str, count: int) -> str: ...


class SecClient:
    def __init__(self, http: HttpClient, cache: JsonFileCache, cfg: Config):
        self.http = http
        self.cache = cache
        self.cfg = cfg
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        delay = self.cfg.sec_min_interval_seconds - (time.monotonic() - self._last_request_at)
        if delay > 0:
            time.sleep(delay)
        self._last_request_at = time.monotonic()

    def get_tickers_exchange(self) -> dict[str, Any]:
        ttl = self.cfg.sec_reference_ttl_hours * 3600
        cached = self.cache.get_json("sec_reference", "company_tickers_exchange", ttl)
        if cached is not None:
            return cached
        self._throttle()
        payload = self.http.get_json(SEC_TICKERS_EXCHANGE_URL, headers={"Accept": "application/json"})
        self.cache.set_json("sec_reference", "company_tickers_exchange", payload)
        return payload

    def get_cik_lookup_text(self) -> str:
        ttl = self.cfg.sec_reference_ttl_hours * 3600
        cached = self.cache.get_text("sec_reference", "cik_lookup_data", ttl)
        if cached is not None:
            return cached
        self._throttle()
        text = self.http.get_text(SEC_CIK_LOOKUP_URL, headers={"Accept": "text/plain, text/html;q=0.9, */*;q=0.8"})
        self.cache.set_text("sec_reference", "cik_lookup_data", text)
        return text

    def get_submissions(self, cik: str) -> dict[str, Any]:
        cik10 = sec_zfill_cik(cik)
        ttl = self.cfg.submissions_ttl_hours * 3600
        cached = self.cache.get_json("sec_submissions", cik10, ttl)
        if cached is not None:
            return cached
        self._throttle()
        payload = self.http.get_json(
            SEC_SUBMISSIONS_URL.format(cik=cik10),
            headers={"Accept": "application/json"},
        )
        self.cache.set_json("sec_submissions", cik10, payload)
        return payload

    def browse_company(self, query: str, count: int) -> str:
        key = f"{query}|{count}"
        cached = self.cache.get_text("sec_browse", key, 24 * 3600)
        if cached is not None:
            return cached
        self._throttle()
        text = self.http.get_text(
            SEC_BROWSE_EDGAR_URL,
            params={"action": "getcompany", "company": query, "owner": "exclude", "count": count},
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        self.cache.set_text("sec_browse", key, text)
        return text


class OpenFigiClient:
    def __init__(self, http: HttpClient, cache: JsonFileCache, api_key: Optional[str]):
        self.http = http
        self.cache = cache
        self.api_key = api_key

    def map_identifier(self, id_type: str, id_value: str) -> list[dict[str, Any]]:
        key = f"{id_type}|{id_value}"
        cached = self.cache.get_json("openfigi", key, 30 * 24 * 3600)
        if cached is not None:
            return list(cached)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key

        payload = self.http.post_json(
            OPENFIGI_URL,
            json_payload=[{
                "idType": id_type,
                "idValue": id_value,
                "marketSecDes": "Equity",
                "includeUnlistedEquities": True,
            }],
            headers=headers,
        )
        result = payload[0].get("data") if payload and isinstance(payload, list) else []
        result = result if isinstance(result, list) else []
        self.cache.set_json("openfigi", key, result)
        return list(result)


# ---------------------------------------------------------------------------
# SEC reference data – now with an inverted token index (§3 of spec)
# ---------------------------------------------------------------------------


class SecReferenceData:
    def __init__(self, current_entities: list[SecEntity], lookup_entities: list[SecEntity]):
        self.current_entities = current_entities
        self.lookup_entities = lookup_entities
        self.by_ticker: dict[str, list[SecEntity]] = {}
        self.by_name: dict[str, list[SecEntity]] = {}
        # Inverted index: informative-token → [SecEntity, …]
        self.by_token: dict[str, list[SecEntity]] = {}

        for entity in current_entities:
            if entity.ticker:
                self.by_ticker.setdefault(entity.ticker, []).append(entity)

        all_entities = [*current_entities, *lookup_entities]
        for entity in all_entities:
            norm = normalize_name(entity.name)
            if norm:
                self.by_name.setdefault(norm, []).append(entity)
                for token in norm.split():
                    # _GENERIC_NAME_WORDS are already stripped by normalize_name,
                    # but guard against any leakage:
                    if token not in _GENERIC_NAME_WORDS:
                        self.by_token.setdefault(token, []).append(entity)

    @classmethod
    def load(cls, sec_client: SecClientProtocol) -> "SecReferenceData":
        payload = sec_client.get_tickers_exchange()
        fields = payload.get("fields") or []
        data_rows = payload.get("data") or []

        current_entities: list[SecEntity] = []
        for row in data_rows:
            obj = dict(zip(fields, row))
            current_entities.append(
                SecEntity(
                    cik=sec_zfill_cik(obj.get("cik")),
                    name=str(obj.get("name") or "").strip(),
                    ticker=normalize_ticker(obj.get("ticker")),
                    exchange=str(obj.get("exchange") or "").strip(),
                    source="sec_company_tickers_exchange",
                )
            )

        lookup_entities: list[SecEntity] = []
        try:
            raw_lookup = sec_client.get_cik_lookup_text()
            for line in raw_lookup.splitlines():
                match = _CIK_LOOKUP_LINE_RE.match(line.strip())
                if not match:
                    continue
                lookup_entities.append(
                    SecEntity(
                        cik=sec_zfill_cik(match.group("cik")),
                        name=" ".join(match.group("name").split()),
                        source="sec_cik_lookup",
                    )
                )
        except Exception as exc:  # pragma: no cover - soft fallback by design
            logger.warning("Unable to refresh SEC historical CIK lookup data: %s", exc)

        return cls(current_entities=current_entities, lookup_entities=lookup_entities)

    def exact_name_candidates(self, name: str) -> list[SecEntity]:
        return list(self.by_name.get(normalize_name(name), []))

    def fuzzy_name_candidates(
        self,
        name: str,
        limit: int = 20,
        extra_variants: Optional[list[str]] = None,
    ) -> list[tuple[SecEntity, float]]:
        """Find candidates via inverted token index with recall variants.

        This replaces the old first-token-only approach with a union of posting
        lists across *all* informative tokens from the original name plus its
        recall variants, ranked by token overlap then refined by string similarity.
        """
        normalized = normalize_name(name)
        if not normalized:
            return []

        # Collect query tokens from the original name + recall variants
        query_tokens: set[str] = set(normalized.split())
        for variant in recall_name_variants(name):
            query_tokens.update(variant.split())
        if extra_variants:
            for ev in extra_variants:
                query_tokens.update(normalize_name(ev).split())
        query_tokens -= _GENERIC_NAME_WORDS
        query_tokens.discard("")

        # Gather candidates from inverted index
        candidate_hits: dict[str, int] = {}       # cik → count of distinct token hits
        candidate_entity: dict[str, SecEntity] = {}  # cik → first entity seen
        for token in query_tokens:
            seen_for_token: set[str] = set()
            for entity in self.by_token.get(token, []):
                if entity.cik not in seen_for_token:
                    seen_for_token.add(entity.cik)
                    candidate_hits[entity.cik] = candidate_hits.get(entity.cik, 0) + 1
                    if entity.cik not in candidate_entity:
                        candidate_entity[entity.cik] = entity

        if not candidate_entity:
            # Ultimate fallback: scan current entities (expensive but rare)
            pool = self.current_entities
        else:
            # Prefer entities with more token overlap; cap pool size
            sorted_ciks = sorted(
                candidate_hits, key=lambda c: candidate_hits[c], reverse=True,
            )
            pool = [candidate_entity[c] for c in sorted_ciks[:300]]

        return choose_best_name_match(name, pool, limit=limit, use_recall=True)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class CIKResolver:
    def __init__(self, cfg: Config, sec_client: SecClientProtocol, figi_client: Optional[OpenFigiClient]):
        self.cfg = cfg
        self.sec_client = sec_client
        self.figi_client = figi_client
        self.reference = SecReferenceData.load(sec_client)
        self._submission_cache: dict[str, SubmissionProfile] = {}
        logger.info(
            "Reference data loaded: current_entities=%d historical_lookup=%d "
            "tickers=%d token_index_keys=%d",
            len(self.reference.current_entities),
            len(self.reference.lookup_entities),
            len(self.reference.by_ticker),
            len(self.reference.by_token),
        )

    # ----- public entry point -----

    def resolve(self, company: CompanyRecord) -> ResolutionResult:
        if not company.name:
            return ResolutionResult(cik=None, confidence=0.0, rationale="missing_name")

        # 1. Validate existing CIK
        if company.existing_cik:
            validated = self._validate_existing_cik(company)
            if validated is not None:
                return validated

        # 2 & 3. FIGI hints + local candidate collection
        figi_hints = self._get_figi_hints(company)
        has_identifier = bool(figi_hints)
        candidates = self._collect_candidates(company, figi_hints)

        # 4. EDGAR browse fallback (with multiple query variants)
        if not candidates and self.cfg.browse_fallback_enabled:
            self._add_browse_candidates(company, figi_hints, candidates)
        # Also try browse if we have candidates but none are identifier-backed
        # and we have no strong match yet
        if self.cfg.browse_fallback_enabled and not any(c.identifier_backed for c in candidates.values()):
            self._add_browse_candidates(company, figi_hints, candidates)

        if not candidates:
            return ResolutionResult(cik=None, confidence=0.0, rationale="no_candidates")

        # 5 & 6. Validate and rank
        ranked = self._validate_and_rank_candidates(company, figi_hints, candidates)
        if not ranked:
            return ResolutionResult(cik=None, confidence=0.0, rationale="validation_failed")

        best_cik, best_score, best_reason, best_id_backed = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        # Acceptance thresholds differ by evidence type
        if best_id_backed:
            # Identifier-backed: lower bar
            accept = best_score >= 0.95 and (best_score - second_score >= 0.06 or best_score >= 1.10)
        else:
            # Name-only: require stronger proof
            accept = best_score >= 1.05 and (best_score - second_score >= 0.08 or best_score >= 1.20)

        if accept:
            return ResolutionResult(
                cik=best_cik,
                confidence=min(1.0, best_score / 1.35),
                rationale=best_reason,
            )

        return ResolutionResult(
            cik=None,
            confidence=min(1.0, best_score / 1.35),
            rationale=f"ambiguous:{best_reason}",
        )

    # ----- step 1: existing-CIK validation -----

    def _validate_existing_cik(self, company: CompanyRecord) -> Optional[ResolutionResult]:
        try:
            submission = self._get_submission(company.existing_cik)
        except Exception as exc:
            logger.warning("Existing CIK could not be validated for %s: %s", company.name, exc)
            return None

        name_score = best_name_score(
            company.name, submission.all_names, use_recall_variants=True,
        )
        if name_score >= max(0.86, name_threshold(company.name) - 0.04):
            return ResolutionResult(
                cik=submission.cik,
                confidence=1.0,
                rationale="existing_cik_validated",
            )
        logger.warning(
            "Existing CIK rejected for %s: candidate=%s score=%.3f",
            company.name,
            submission.cik,
            name_score,
        )
        return None

    # ----- step 2: FIGI hints (keep top N, not just best) -----

    def _get_figi_hints(self, company: CompanyRecord) -> list[FigiHint]:
        """Return top *max_figi_hints* FIGI rows, preferring equity/common stock."""
        if self.figi_client is None:
            return []

        jobs: list[tuple[str, str]] = []
        if company.cusip:
            jobs.append(("ID_CUSIP", company.cusip))
            if len(company.cusip) == 8:
                jobs.append(("ID_CUSIP_8_CHR", company.cusip))
        if company.isin:
            jobs.append(("ID_ISIN", company.isin))

        all_hints: list[FigiHint] = []
        for id_type, id_value in jobs:
            try:
                rows = self.figi_client.map_identifier(id_type, id_value)
            except Exception as exc:
                logger.warning("OpenFIGI lookup failed for %s=%s: %s", id_type, id_value, exc)
                continue

            for row in rows:
                ticker = normalize_ticker(row.get("ticker"))
                name = str(row.get("name") or "").strip()
                exch_code = str(row.get("exchCode") or "").strip().upper()
                market_sector = str(row.get("marketSector") or "").strip()
                security_type = str(
                    row.get("securityType") or row.get("securityType2") or ""
                ).strip()

                score = 0.0
                if market_sector.upper() == "EQUITY":
                    score += 0.25
                if security_type.upper() in {
                    "COMMON STOCK", "ADR", "PREFERRED STOCK", "REIT",
                }:
                    score += 0.20
                if ticker:
                    score += 0.20
                if exch_code in _US_FIGI_EXCHANGE_CODES:
                    score += 0.10
                score += 0.35 * name_similarity(company.name, name)
                if id_type == "ID_CUSIP":
                    score += 0.08
                elif id_type == "ID_ISIN":
                    score += 0.05

                all_hints.append(FigiHint(
                    id_type=id_type,
                    id_value=id_value,
                    ticker=ticker,
                    name=name,
                    exch_code=exch_code,
                    market_sector=market_sector,
                    security_type=security_type,
                    score=score,
                ))

        # De-duplicate by (ticker, name) keeping highest score
        seen: dict[tuple[str, str], FigiHint] = {}
        for hint in all_hints:
            key = (hint.ticker, normalize_name(hint.name))
            if key not in seen or hint.score > seen[key].score:
                seen[key] = hint
        deduped = sorted(seen.values(), key=lambda h: h.score, reverse=True)

        kept = deduped[: self.cfg.max_figi_hints]
        for hint in kept:
            logger.debug(
                "FIGI hint for %s -> ticker=%s name=%s score=%.3f via %s",
                company.name, hint.ticker, hint.name, hint.score, hint.id_type,
            )
        return kept

    # ----- step 3: candidate collection (local reference data) -----

    def _collect_candidates(
        self,
        company: CompanyRecord,
        figi_hints: list[FigiHint],
    ) -> dict[str, Candidate]:
        candidates: dict[str, Candidate] = {}

        def add(
            entity: SecEntity,
            score: float,
            evidence: str,
            id_backed: bool = False,
        ) -> None:
            if not entity.cik:
                return
            item = candidates.get(entity.cik)
            if item is None:
                candidates[entity.cik] = Candidate(
                    cik=entity.cik,
                    score=score,
                    evidence=[evidence],
                    identifier_backed=id_backed,
                )
            else:
                item.add(score, evidence)
                if id_backed:
                    item.identifier_backed = True

        # Input ticker
        if company.ticker:
            for entity in self.reference.by_ticker.get(company.ticker, []):
                add(entity, 0.95, f"input_ticker:{company.ticker}")

        # FIGI-derived tickers and names (all kept hints, not just one)
        for hint in figi_hints:
            if hint.ticker:
                for entity in self.reference.by_ticker.get(hint.ticker, []):
                    add(entity, 0.98, f"figi_ticker:{hint.id_type}:{hint.ticker}", id_backed=True)
            if hint.name:
                for entity in self.reference.exact_name_candidates(hint.name):
                    add(entity, 0.82, f"figi_name_exact:{hint.id_type}", id_backed=True)
                for entity, sim in self.reference.fuzzy_name_candidates(hint.name):
                    if sim >= 0.85:
                        add(entity, 0.40 + 0.30 * sim, f"figi_name_fuzzy:{sim:.3f}", id_backed=True)

        # Direct company-name matching (exact)
        for entity in self.reference.exact_name_candidates(company.name):
            base = 0.92 if entity.source == "sec_company_tickers_exchange" else 0.84
            add(entity, base, f"name_exact:{entity.source}")

        # Direct company-name matching (fuzzy via inverted token index)
        extra_variants = [h.name for h in figi_hints if h.name]
        for entity, sim in self.reference.fuzzy_name_candidates(
            company.name, extra_variants=extra_variants,
        ):
            if sim >= max(0.78, name_threshold(company.name) - 0.12):
                add(entity, 0.42 + 0.28 * sim, f"name_fuzzy:{sim:.3f}:{entity.source}")

        return candidates

    # ----- step 4: browse-EDGAR fallback with multiple query variants -----

    def _add_browse_candidates(
        self,
        company: CompanyRecord,
        figi_hints: list[FigiHint],
        candidates: dict[str, Candidate],
    ) -> None:
        queries: list[str] = []
        seen_queries: set[str] = set()

        def _enqueue(q: str) -> None:
            norm = normalize_name(q)
            if norm and norm not in seen_queries:
                seen_queries.add(norm)
                queries.append(q)

        # Original name
        _enqueue(company.name)
        # HTML-decoded name
        _enqueue(html_module.unescape(company.name))
        # FIGI hint names
        for hint in figi_hints[:3]:
            if hint.name:
                _enqueue(hint.name)
        # Recall variants (limit to 2 to avoid excessive requests)
        for variant in recall_name_variants(company.name)[:2]:
            _enqueue(variant)

        for query in queries:
            try:
                html_text = self.sec_client.browse_company(query, self.cfg.browse_max_results)
            except Exception as exc:
                logger.warning("SEC browse fallback failed for %s (query=%s): %s", company.name, query, exc)
                continue

            seen_ciks: set[str] = set()
            for match in _BROWSE_EDGAR_LINK_RE.finditer(html_text):
                cik = sec_zfill_cik(match.group("cik"))
                name = html_strip(match.group("name"))
                if not cik or cik in seen_ciks or not name:
                    continue
                seen_ciks.add(cik)
                sim = best_name_score(company.name, [name], use_recall_variants=True)
                if sim < 0.72:
                    continue
                item = candidates.get(cik)
                evidence = f"browse_edgar:{sim:.3f}"
                score = 0.40 + 0.30 * sim
                id_backed = any(
                    h.ticker and normalize_ticker(name) == h.ticker
                    for h in figi_hints
                )
                if item is None:
                    candidates[cik] = Candidate(
                        cik=cik, score=score, evidence=[evidence],
                        identifier_backed=id_backed,
                    )
                else:
                    item.add(score, evidence)

    # ----- step 5/6: validate candidates against submissions -----

    def _validate_and_rank_candidates(
        self,
        company: CompanyRecord,
        figi_hints: list[FigiHint],
        candidates: dict[str, Candidate],
    ) -> list[tuple[str, float, str, bool]]:
        """Return (cik, total_score, rationale, identifier_backed) sorted desc."""
        ranked: list[tuple[str, float, str, bool]] = []
        preferred_tickers = {
            t for t in [company.ticker] + [h.ticker for h in figi_hints] if t
        }
        preferred_names = [company.name]
        for hint in figi_hints:
            if hint.name:
                preferred_names.append(hint.name)

        for cik, candidate in candidates.items():
            try:
                submission = self._get_submission(cik)
            except Exception as exc:
                logger.debug(
                    "Skipping CIK %s for %s after submissions error: %s",
                    cik, company.name, exc,
                )
                continue

            # Compute best name similarity including recall variants of the
            # input name against all submission names (current + former).
            name_score = 0.0
            for query_name in preferred_names:
                score = best_name_score(
                    query_name, submission.all_names, use_recall_variants=True,
                )
                name_score = max(name_score, score)

            ticker_match = bool(
                preferred_tickers and preferred_tickers.intersection(submission.tickers)
            )
            current_name_exact = (
                normalize_name(company.name) == normalize_name(submission.name)
            )

            total = candidate.score
            total += 0.40 * name_score
            if ticker_match:
                total += 0.22
            if current_name_exact:
                total += 0.10

            # Gate: identifier-backed candidates get a lower name-similarity bar
            if candidate.identifier_backed:
                gate_threshold = max(0.72, name_threshold(company.name) - 0.18)
                gate = name_score >= gate_threshold or ticker_match
            else:
                gate_threshold = max(0.84, name_threshold(company.name) - 0.06)
                gate = name_score >= gate_threshold or ticker_match

            if not gate:
                logger.debug(
                    "Gate rejected CIK %s for %s: name_score=%.3f threshold=%.3f "
                    "ticker_match=%s id_backed=%s",
                    cik, company.name, name_score, gate_threshold,
                    ticker_match, candidate.identifier_backed,
                )
                continue

            rationale = ";".join(candidate.evidence)
            ranked.append((cik, total, rationale, candidate.identifier_backed))

        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked

    def _get_submission(self, cik: str) -> SubmissionProfile:
        cik10 = sec_zfill_cik(cik)
        cached = self._submission_cache.get(cik10)
        if cached is not None:
            return cached
        payload = self.sec_client.get_submissions(cik10)
        profile = SubmissionProfile.from_api(cik10, payload)
        self._submission_cache[cik10] = profile
        return profile


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------


class _QuotedStr(str):
    pass


class _CompanyDumper(yaml.SafeDumper):
    pass


_CompanyDumper.add_representer(
    _QuotedStr,
    lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'"),
)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), list):
        raise ValueError("Expected a YAML document with a top-level 'companies' list")
    return payload


def _prepare_yaml(value: Any, *, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {k: _prepare_yaml(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_prepare_yaml(item, key=key) for item in value]
    if key == "cik" and value not in (None, ""):
        return _QuotedStr(sec_zfill_cik(value))
    return value


def dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    prepared = _prepare_yaml(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(prepared, handle, Dumper=_CompanyDumper, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class RunSummary:
    total: int = 0
    resolved: int = 0
    unresolved: int = 0


def enrich_file(cfg: Config) -> RunSummary:
    payload = load_yaml(cfg.input_path)
    companies_raw = payload["companies"]

    cache = JsonFileCache(cfg.cache_dir)
    http = HttpClient(cfg.sec_user_agent)
    sec_client = SecClient(http=http, cache=cache, cfg=cfg)
    figi_client = OpenFigiClient(http=http, cache=cache, api_key=cfg.openfigi_api_key)
    resolver = CIKResolver(cfg=cfg, sec_client=sec_client, figi_client=figi_client)

    summary = RunSummary(total=len(companies_raw))
    for index, company_dict in enumerate(companies_raw, start=1):
        company = CompanyRecord(raw=company_dict)
        result = resolver.resolve(company)

        company_dict["cik"] = result.cik
        if result.cik:
            summary.resolved += 1
            logger.info(
                "[%d/%d] Resolved %-40s -> %s (confidence=%.2f, %s)",
                index,
                len(companies_raw),
                company.name[:40],
                result.cik,
                result.confidence,
                result.rationale,
            )
        else:
            summary.unresolved += 1
            logger.warning(
                "[%d/%d] Unresolved %-40s (%s)",
                index,
                len(companies_raw),
                company.name[:40],
                result.rationale,
            )

    validate_output(payload)
    dump_yaml(cfg.output_path, payload)
    logger.info(
        "Completed CIK enrichment: resolved=%d unresolved=%d total=%d output=%s",
        summary.resolved,
        summary.unresolved,
        summary.total,
        cfg.output_path,
    )
    return summary


def validate_output(payload: dict[str, Any]) -> None:
    companies = payload.get("companies") or []
    for idx, item in enumerate(companies, start=1):
        cik = item.get("cik")
        if cik in (None, ""):
            continue
        cik_str = sec_zfill_cik(cik)
        if not re.fullmatch(r"\d{10}", cik_str):
            raise ValueError(f"Invalid CIK at companies[{idx - 1}]: {cik!r}")
        item["cik"] = cik_str


# ---------------------------------------------------------------------------
# Self-test (offline validation)
# ---------------------------------------------------------------------------


class FakeSecClient:
    def __init__(self) -> None:
        self._tickers = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
                [320193, "Apple Inc.", "AAPL", "Nasdaq"],
                [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
                [1018724, "AMAZON COM INC", "AMZN", "Nasdaq"],
                [1781933, "MUENCHENER RUCKVERSICHERUNGS-GESELLSCHAFT AG IN MUENCHEN", "MURGY", "OTC"],
                [1076930, "MAHINDRA & MAHINDRA LTD", "MAHMF", "OTC"],
            ],
        }
        self._lookup = "".join([
            "NVIDIA CORP:1045810:\n",
            "APPLE INC:320193:\n",
            "MICROSOFT CORP:789019:\n",
            "AMAZON COM INC:1018724:\n",
            "MUENCHENER RUCKVERSICHERUNGS-GESELLSCHAFT AG IN MUENCHEN:1781933:\n",
            "MAHINDRA & MAHINDRA LTD:1076930:\n",
        ])
        self._submissions = {
            "0001045810": {"name": "NVIDIA CORP", "tickers": ["NVDA"], "exchanges": ["Nasdaq"], "formerNames": []},
            "0000320193": {"name": "Apple Inc.", "tickers": ["AAPL"], "exchanges": ["Nasdaq"], "formerNames": []},
            "0000789019": {"name": "MICROSOFT CORP", "tickers": ["MSFT"], "exchanges": ["Nasdaq"], "formerNames": []},
            "0001018724": {"name": "AMAZON COM INC", "tickers": ["AMZN"], "exchanges": ["Nasdaq"], "formerNames": []},
            "0001781933": {
                "name": "MUENCHENER RUCKVERSICHERUNGS-GESELLSCHAFT AG IN MUENCHEN",
                "tickers": ["MURGY"], "exchanges": ["OTC"],
                "formerNames": [{"name": "MUNICH REINSURANCE CO"}],
            },
            "0001076930": {
                "name": "MAHINDRA & MAHINDRA LTD",
                "tickers": ["MAHMF"], "exchanges": ["OTC"],
                "formerNames": [],
            },
        }

    def get_tickers_exchange(self) -> dict[str, Any]:
        return self._tickers

    def get_cik_lookup_text(self) -> str:
        return self._lookup

    def get_submissions(self, cik: str) -> dict[str, Any]:
        return self._submissions[sec_zfill_cik(cik)]

    def browse_company(self, query: str, count: int) -> str:
        return ""


class FakeFigiClient:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {
            ("ID_CUSIP", "67066G104"): [{"ticker": "NVDA", "name": "NVIDIA CORP", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_ISIN", "US67066G1040"): [{"ticker": "NVDA", "name": "NVIDIA CORP", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_CUSIP", "037833100"): [{"ticker": "AAPL", "name": "APPLE INC", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_ISIN", "US0378331005"): [{"ticker": "AAPL", "name": "APPLE INC", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_CUSIP", "594918104"): [{"ticker": "MSFT", "name": "MICROSOFT CORP", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_ISIN", "US5949181045"): [{"ticker": "MSFT", "name": "MICROSOFT CORP", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_CUSIP", "023135106"): [{"ticker": "AMZN", "name": "AMAZON COM INC", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_ISIN", "US0231351067"): [{"ticker": "AMZN", "name": "AMAZON COM INC", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"}],
            ("ID_ISIN", "DE0008430026"): [
                {"ticker": "MUV2", "name": "MUENCHENER RUECKVER AG-REG", "exchCode": "GY", "marketSector": "Equity", "securityType": "Common Stock"},
                {"ticker": "MURGY", "name": "MUENCHENER RUCK MUNICH RE REG", "exchCode": "US", "marketSector": "Equity", "securityType": "ADR"},
            ],
            ("ID_ISIN", "INE101A01026"): [
                {"ticker": "MM", "name": "MAHINDRA & MAHINDRA LTD", "exchCode": "IB", "marketSector": "Equity", "securityType": "Common Stock"},
                {"ticker": "MAHMF", "name": "MAHINDRA & MAHINDRA", "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"},
            ],
        }

    def map_identifier(self, id_type: str, id_value: str) -> list[dict[str, Any]]:
        return list(self.rows.get((id_type, id_value), []))


def run_self_test() -> None:
    cfg = Config(
        input_path=Path("input.yaml"),
        output_path=Path("output.yaml"),
        cache_dir=Path(".cache"),
        browse_fallback_enabled=False,
    )
    resolver = CIKResolver(cfg=cfg, sec_client=FakeSecClient(), figi_client=FakeFigiClient())

    # --- Core US-equity cases (unchanged from original) ---
    cases = [
        ({"name": "NVIDIA CORP", "cusip": "67066G104", "isin": "US67066G1040", "cik": None}, "0001045810"),
        ({"name": "APPLE INC", "cusip": "037833100", "isin": "US0378331005", "cik": None}, "0000320193"),
        ({"name": "MICROSOFT CORP", "cusip": "594918104", "isin": "US5949181045", "cik": None}, "0000789019"),
        ({"name": "AMAZON.COM INC", "cusip": "023135106", "isin": "US0231351067", "cik": None}, "0001018724"),
    ]
    for payload, expected_cik in cases:
        result = resolver.resolve(CompanyRecord(raw=payload))
        assert result.cik == expected_cik, f"FAIL {payload['name']}: expected={expected_cik} got={result.cik} ({result.rationale})"
        assert result.confidence > 0.75, f"FAIL {payload['name']}: low confidence {result.confidence}"

    # --- New cases that previously failed ---

    # HTML-encoded ampersand + foreign issuer
    result = resolver.resolve(CompanyRecord(raw={
        "name": "MAHINDRA &amp; MAHINDRA",
        "isin": "INE101A01026",
        "cusip": None,
        "cik": None,
    }))
    assert result.cik == "0001076930", f"FAIL Mahindra: expected=0001076930 got={result.cik} ({result.rationale})"

    # Abbreviated/transliterated German name
    result = resolver.resolve(CompanyRecord(raw={
        "name": "MUENCHENER RUECKVER AG-REG",
        "isin": "DE0008430026",
        "cusip": None,
        "cik": None,
    }))
    assert result.cik == "0001781933", f"FAIL Munich Re: expected=0001781933 got={result.cik} ({result.rationale})"

    # --- Verify normalization helpers ---
    assert normalize_name("MAHINDRA &amp; MAHINDRA") == "MAHINDRA MAHINDRA"
    assert normalize_name("MUENCHENER RUECKVER AG-REG") == "MUENCHENER RUECKVER REG"

    variants = recall_name_variants("MUENCHENER RUECKVER AG-REG")
    variant_set = set(variants)
    # Should contain transliterated form
    assert any("MUNCHENER" in v for v in variant_set), f"Missing transliteration in {variant_set}"
    # Should contain abbreviation-expanded form
    assert any("RUCKVERSICHERUNGS" in v for v in variant_set), f"Missing expansion in {variant_set}"

    variants_m = recall_name_variants("MAHINDRA &amp; MAHINDRA")
    # The HTML decode should produce a different normalized form only if
    # the base normalization is different (here it's the same after AND removal)
    # Main point: it shouldn't crash
    assert isinstance(variants_m, list)

    logger.info("All self-test assertions passed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve SEC CIKs for companies in a YAML file")
    parser.add_argument("--input", dest="input_path", type=Path, default=Path("../data/parsed_invesco-ftse-all-world/2026-03-31__Die_10_groessten_Positionen-holdings.yaml"), help="Input YAML path")
    parser.add_argument("--output", dest="output_path", type=Path, default=Path("../data/enriched_invesco-ftse-all-world/2026-03-31__Die_10_groessten_Positionen-holdings.yaml"), help="Output YAML path")
    parser.add_argument("--cache-dir", dest="cache_dir", type=Path, default=Path("../data/.cik_cache"))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--sec-user-agent", default=os.getenv("SEC_USER_AGENT", DEFAULT_SEC_USER_AGENT))
    parser.add_argument("--openfigi-api-key", default=os.getenv("OPENFIGI_APIKEY"))
    parser.add_argument("--disable-browse-fallback", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level, args.log_path)

    if args.self_test:
        run_self_test()
        logger.info("Self-test completed successfully")
        return 0

    if args.input_path is None or args.output_path is None:
        raise SystemExit("--input and --output are required unless --self-test is used")

    cfg = Config(
        input_path=args.input_path,
        output_path=args.output_path,
        cache_dir=args.cache_dir,
        log_level=args.log_level,
        log_path=args.log_path,
        sec_user_agent=args.sec_user_agent,
        openfigi_api_key=args.openfigi_api_key,
        browse_fallback_enabled=not args.disable_browse_fallback,
    )
    enrich_file(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))