"""Convert an ETF constituents spreadsheet to a structured YAML file."""
import logging
import os
import re
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List

import pandas as pd
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants / regexes
# ---------------------------------------------------------------------------

CURRENCIES = (
    "USD|EUR|GBP|JPY|CHF|CAD|AUD|HKD|TWD|KRW|SEK|DKK|NOK|SGD|INR|BRL|ZAR"
    "|MXN|CNY|NZD|CLP|THB|IDR|MYR|PHP|PLN|CZK|HUF|ILS|TRY|ARS|COP|PEN"
    "|QAR|SAR|AED|KWd|RON|ISK|NPV"
)
_PAR_VALUE_RE = re.compile(rf"\s+(?:{CURRENCIES})\S*(?:\s.*)?$", re.I)
_ADR_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:SP(?:ON(?:S)?)?\s+)?(?:ADR|GDR)(?:\s+.*)?$|\s*-\s*REG\s+S$", re.I
)
_PRF_SUFFIX_RE = re.compile(r"\s+NON-CUM\s+PRF\s+SHS$", re.I)

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")

HEADER_SKIP_ROWS = 4          # data starts at row index 4 (0-based)
COLUMN_NAMES = ["name", "cusip", "isin", "weight"]

SCHEMA_VERSION = 2
SOURCE_ETF = "INVESCO FTSE ALL-WORLD ETF"

# ---------------------------------------------------------------------------
# Models – input
# ---------------------------------------------------------------------------

@dataclass
class RawConstituent:
    """Validated row from the ETF spreadsheet."""
    name: str
    cusip: Optional[str]
    isin: str
    weight: float

    def __post_init__(self):
        # coerce cusip
        if self.cusip is None or (isinstance(self.cusip, float) and pd.isna(self.cusip)):
            self.cusip = None
        else:
            self.cusip = str(self.cusip).strip() or None

        # validate ISIN
        if not _ISIN_RE.fullmatch(str(self.isin)):
            raise ValueError(f"Invalid ISIN format: {self.isin}")

        # validate weight
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"Weight out of range [0, 1]: {self.weight}")


# ---------------------------------------------------------------------------
# Models – output  (report.md §2: CompanyRef reference-universe schema)
# ---------------------------------------------------------------------------

@dataclass
class CompanyRef:
    """Output schema for a single ETF constituent.

    Reference-universe schema (report §2).  Mutable snapshot fields
    (market_cap, revenue, ADV, sector/industry) are deliberately excluded
    and belong in a separate dated-snapshot file.
    """
    # --- core identifiers ---
    isin: str = ""
    cusip: Optional[str] = None
    raw_name: str = ""              # original spreadsheet name
    name: str = ""                  # normalized name for matching
    weight: float = 0.0

    # --- CIK / SEC fields (populated by cik_enricher) ---
    cik: Optional[str] = None
    sec_eligible: bool = False
    cik_confidence: Optional[float] = None
    cik_rationale: Optional[str] = None

    # --- FIGI identity fields (populated by cik_enricher) ---
    ticker: Optional[str] = None        # not unique by itself
    exchange_code: Optional[str] = None
    composite_figi: Optional[str] = None
    share_class_figi: Optional[str] = None
    security_type: Optional[str] = None

# ---------------------------------------------------------------------------
# Name cleaning
# ---------------------------------------------------------------------------

def clean_name(raw: str) -> str:
    """Strip par-value suffixes and share-class markers from raw constituent names.

    This is the normalisation step used for matching; the original name
    is preserved as ``raw_name``.
    """
    name = _PAR_VALUE_RE.sub("", raw)
    name = _ADR_SUFFIX_RE.sub("", name)
    name = _PRF_SUFFIX_RE.sub("", name)
    return name.strip()

# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_constituents(path: Path) -> List[RawConstituent]:
    logger.info("Reading constituents from %s", path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    df = pd.read_excel(path, skiprows=HEADER_SKIP_ROWS, header=None, names=COLUMN_NAMES)
    df = df.dropna(subset=["isin"])
    logger.info("Found %d rows with ISIN values", len(df))

    rows: List[RawConstituent] = []
    for i, record in enumerate(df.to_dict(orient="records")):
        try:
            rows.append(RawConstituent(**record))
        except Exception as exc:
            logger.warning("Skipping row %d: %s — %s", i, record, exc)
    logger.info("Validated %d / %d rows", len(rows), len(df))
    return rows

# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def check_duplicates(rows: List[RawConstituent]) -> None:
    """Warn when the same (CUSIP, ISIN) pair appears more than once."""
    pairs = [(r.cusip, r.isin) for r in rows]
    counts = Counter(pairs)
    dupes = {pair: n for pair, n in counts.items() if n > 1}
    if dupes:
        for (cusip, isin), n in dupes.items():
            msg = f"Duplicate entry detected {n}x — CUSIP={cusip}, ISIN={isin}"
            logger.warning(msg)
            warnings.warn(msg, stacklevel=2)
    else:
        logger.info("No duplicate (CUSIP, ISIN) pairs found")

# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

def to_companies(rows: List[RawConstituent]) -> List[CompanyRef]:
    """Build CompanyRef entries from raw constituents.

    Populates ``raw_name`` (original) and ``name`` (cleaned/normalised).
    All enrichment fields are left at their defaults (None / False).
    """
    logger.info("Building company entries for %d constituents", len(rows))
    return [
        CompanyRef(
            cusip=r.cusip,
            isin=r.isin,
            raw_name=r.name,
            name=clean_name(r.name),
            weight=r.weight,
        )
        for r in rows
    ]

# ---------------------------------------------------------------------------
# as_of_date extraction
# ---------------------------------------------------------------------------

def _extract_as_of_date(path: Path) -> str:
    """Extract the date prefix from a filename like '2026-03-31__...xlsx'.

    Returns the date string or an empty string if not found.
    """
    match = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
    return match.group(1) if match else ""

# ---------------------------------------------------------------------------
# Write YAML (with blank lines between entries)
# ---------------------------------------------------------------------------

def _none_representer(dumper: yaml.Dumper, _data: None) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "null")


def write_yaml(companies: List[CompanyRef], path: Path, *, as_of_date: str = "") -> None:
    """Write company list to YAML with file-level metadata (report §2)."""
    logger.info("Writing %d companies to %s", len(companies), path)
    dumper = yaml.Dumper
    dumper.add_representer(type(None), _none_representer)

    # File-level metadata (report §2)
    header_lines = [
        f"schema_version: {SCHEMA_VERSION}",
        f'source_etf: "{SOURCE_ETF}"',
        f'as_of_date: "{as_of_date}"',
        "companies:",
    ]
    header = "\n".join(header_lines) + "\n"

    blocks: List[str] = []
    for company in companies:
        d = asdict(company)
        entry = yaml.dump(
            [d], Dumper=dumper, default_flow_style=False, sort_keys=False
        )
        # yaml.dump of a list produces "- key: val\n  key: val\n..."
        # Indent everything to sit under `companies:`
        indented = "  " + entry.replace("\n", "\n  ").rstrip() + "\n"
        blocks.append(indented)

    path.write_text(header + "\n".join(blocks))
    logger.info("YAML written successfully")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_output(path: Path, expected_count: int) -> None:
    """Re-read the YAML and check it round-trips correctly."""
    logger.info("Validating output file %s", path)
    with open(path) as f:
        loaded = yaml.safe_load(f)

    # Validate file-level metadata
    assert loaded.get("schema_version") == SCHEMA_VERSION, (
        f"Expected schema_version={SCHEMA_VERSION}, got {loaded.get('schema_version')}"
    )
    assert "source_etf" in loaded, "Missing 'source_etf' metadata"
    assert "as_of_date" in loaded, "Missing 'as_of_date' metadata"

    companies = loaded.get("companies", [])
    assert len(companies) == expected_count, (
        f"Expected {expected_count} companies, got {len(companies)}"
    )
    for i, c in enumerate(companies):
        assert "isin" in c and c["isin"], f"Company #{i} missing ISIN"
        assert "cusip" in c, f"Company #{i} missing CUSIP field"
        assert "raw_name" in c and c["raw_name"], f"Company #{i} missing raw_name"
        assert "name" in c and c["name"], f"Company #{i} missing name"
        assert "weight" in c and isinstance(c["weight"], (int, float)), (
            f"Company #{i} has invalid weight: {c.get('weight')}"
        )
    logger.info("Validation passed — %d companies verified", len(companies))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

INPUT_FNAME = "Die_10_groessten_Positionen-holdings.xlsx"
OUTPUT_FNAME = "{scrape_date}_watchlist.yaml" # TODO fix below to use this output name

def main() -> None:
    input_base = Path("../data/scrape_invesco-ftse-all-world/")
    output_dir = Path("../data/parsed_invesco-ftse-all-world/")
    output_dir.mkdir(parents=True, exist_ok=True)

    for date_dir in sorted(input_base.glob("[0-9][0-9][0-9][0-9]_[0-9][0-9]_[0-9][0-9]")): # for all scrape days
        for input_path in sorted(date_dir.glob(INPUT_FNAME)):
            output_path = output_dir / OUTPUT_FNAME.format(scrape_date=date_dir.name)

            if output_path.exists():
                logger.info("Skipping %s — output already exists", input_path.name)
                continue

            logger.info("Processing %s/%s", date_dir.name, input_path.name)
            rows = read_constituents(input_path)
            check_duplicates(rows)
            companies = to_companies(rows)
            as_of_date = _extract_as_of_date(input_path)
            write_yaml(companies, output_path, as_of_date=as_of_date)
            validate_output(output_path, expected_count=len(companies))
            logger.info("Done — %d companies written to %s", len(companies), output_path.name)

if __name__ == "__main__":
    main()