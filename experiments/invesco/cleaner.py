"""
clean_watchlist.py

Reads a watchlist YAML file, normalises valid CIKs to zero-padded 10-digit
integers, and deduplicates entries using security-level identifiers.

Key behavioral changes vs. the original (report.md §3, §5):
  - Rows with missing/unresolved CIK are **retained** (CIK is an SEC filer
    identifier, not a security identifier — dropping non-SEC names would lose
    valid global ETF constituents).
  - Deduplication uses security-level keys in priority order:
      1. composite_figi
      2. share_class_figi
      3. isin
      4. (cusip, normalized name)  — fallback
  - CIK is normalised when present and valid, but never used as a dedup key.

A CIK is considered valid when it:
  - is not None, NaN, or missing
  - consists entirely of digits after stripping quotes/whitespace
  - represents a positive integer in the range [1, 9_999_999_999]
"""

import argparse
import logging
import math
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CIK_MAX = 9_999_999_999  # 10 digits
CIK_WIDTH = 10


# ---------------------------------------------------------------------------
# CIK validation & normalisation  (unchanged core logic)
# ---------------------------------------------------------------------------

def is_valid_cik(cik) -> bool:
    """Return True if *cik* is a valid SEC CIK value."""
    if cik is None:
        return False
    if isinstance(cik, float) and math.isnan(cik):
        return False
    cik_str = str(cik).strip().strip("'\"")
    if not cik_str.isdigit():
        return False
    cik_int = int(cik_str)
    return 1 <= cik_int <= CIK_MAX


def normalise_cik(cik) -> int:
    """Convert a raw CIK value to a plain integer.

    The YAML dumper will write it as an unquoted number (e.g. 1234).
    """
    return int(str(cik).strip().strip("'\""))


def invalid_reason(cik) -> str:
    """Human-readable reason why *cik* failed validation."""
    if cik is None:
        return "null / missing"
    if isinstance(cik, float) and math.isnan(cik):
        return "NaN"
    cik_str = str(cik).strip().strip("'\"")
    if not cik_str.isdigit():
        return f"non-numeric value: {cik!r}"
    cik_int = int(cik_str)
    if cik_int < 1:
        return "zero or negative"
    if cik_int > CIK_MAX:
        return f"exceeds 10-digit max ({cik_int})"
    return "unknown"


# ---------------------------------------------------------------------------
# Deduplication key  (report §5: composite_figi > share_class_figi > isin > (cusip, name))
# ---------------------------------------------------------------------------

def _normalize_name_for_dedup(name) -> str:
    """Minimal name normalisation for dedup fallback key."""
    return " ".join(str(name or "").upper().split())


def _dedup_key(entry: dict) -> str:
    """Return a deduplication key for *entry* using the priority order
    from the report.

    Priority:
      1. composite_figi  (best global instrument identifier)
      2. share_class_figi
      3. isin             (always present from parser)
      4. (cusip, normalised name) — last-resort fallback
    """
    composite_figi = (entry.get("composite_figi") or "").strip()
    if composite_figi:
        return f"composite_figi:{composite_figi}"

    share_class_figi = (entry.get("share_class_figi") or "").strip()
    if share_class_figi:
        return f"share_class_figi:{share_class_figi}"

    isin = (entry.get("isin") or "").strip()
    if isin:
        return f"isin:{isin}"

    cusip = (entry.get("cusip") or "").strip()
    name = _normalize_name_for_dedup(entry.get("name"))
    return f"cusip_name:{cusip}|{name}"


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def main(input_path: Path, output_path: Path) -> None:
    log.info("Reading %s", input_path)
    with input_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict) or "companies" not in data:
        sys.exit("ERROR: expected a YAML mapping with a top-level 'companies' key.")

    companies: list = data["companies"]
    total = len(companies)
    log.info("Loaded %d entries", total)

    # ------------------------------------------------------------------
    # Step 1: Normalise valid CIKs (but do NOT drop entries with missing CIK)
    # ------------------------------------------------------------------
    cik_normalised = 0
    cik_invalid = 0

    for entry in companies:
        cik = entry.get("cik")
        if is_valid_cik(cik):
            entry["cik"] = normalise_cik(cik)
            cik_normalised += 1
        elif cik is not None and not (isinstance(cik, float) and math.isnan(cik)):
            # CIK is present but malformed — log and set to None
            cik_str_val = str(cik).strip().strip("'\"")
            if cik_str_val and cik_str_val != "None" and cik_str_val != "null":
                ticker = entry.get("ticker") or "<unknown>"
                name = entry.get("name") or entry.get("raw_name") or "<unknown>"
                log.warning(
                    "[CIK-INVALID] %-20s %-40s cik=%r (%s)",
                    ticker, name, cik, invalid_reason(cik),
                )
                cik_invalid += 1
            entry["cik"] = None

    log.info(
        "CIK normalisation: valid=%d  invalid-cleared=%d  missing=%d",
        cik_normalised, cik_invalid, total - cik_normalised - cik_invalid,
    )

    # ------------------------------------------------------------------
    # Step 2: Deduplicate by security-level identifier (report §5)
    # ------------------------------------------------------------------
    seen_keys: set[str] = set()
    unique_entries: list[dict] = []
    duplicate_entries: list[dict] = []

    for entry in companies:
        key = _dedup_key(entry)
        if key in seen_keys:
            duplicate_entries.append(entry)
            ticker = entry.get("ticker") or "<unknown>"
            name = entry.get("name") or entry.get("raw_name") or "<unknown>"
            log.warning(
                "[DUP]  %-20s %-40s key=%s (duplicate – keeping first occurrence)",
                ticker, name, key,
            )
        else:
            seen_keys.add(key)
            unique_entries.append(entry)

    if duplicate_entries:
        log.info(
            "Duplicates removed: %d  |  Unique: %d",
            len(duplicate_entries), len(unique_entries),
        )
    else:
        log.info("No duplicate entries found")

    data["companies"] = unique_entries

    log.info("Writing %s", output_path)
    with output_path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            data,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    log.info("Write complete")

    # ------------------------------------------------------------------
    # Validate the saved file
    # ------------------------------------------------------------------
    log.info("Validating saved file")
    with output_path.open("r", encoding="utf-8") as fh:
        check = yaml.safe_load(fh)

    assert isinstance(check, dict), "Root is not a mapping."
    assert "companies" in check, "Missing 'companies' key."
    saved = check["companies"]
    assert len(saved) == len(unique_entries), (
        f"Count mismatch: wrote {len(unique_entries)} but read back {len(saved)}."
    )

    # Validate CIKs that are present
    for i, entry in enumerate(saved):
        cik = entry.get("cik")
        if cik is not None:
            assert is_valid_cik(cik), (
                f"Invalid CIK at index {i}: {cik!r} (ticker={entry.get('ticker')!r})"
            )

    # Verify dedup-key uniqueness in the saved file
    saved_keys = [_dedup_key(e) for e in saved]
    assert len(saved_keys) == len(set(saved_keys)), (
        "Duplicate dedup keys found in saved file!"
    )

    cik_count = sum(1 for e in saved if e.get("cik") is not None)
    log.info(
        "All %d entries validated (with CIK: %d, without CIK: %d)",
        len(saved), cik_count, len(saved) - cik_count,
    )
    log.info(
        "Summary — Input: %d | Duplicate: %d | Output: %d",
        total, len(duplicate_entries), len(unique_entries),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and clean CIK values in a watchlist YAML file.",
    )
    parser.add_argument(
        "-i", "--input",
        type=Path,
        default=Path("../data/enriched_invesco-ftse-all-world"),
    )
    parser.add_argument(
        "-g", "--glob-pattern",
        default="*.yaml",
        help="Glob pattern for input files inside the input directory"
             " (widened from original to process full holdings)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("../data/cleaned_invesco-ftse-all-world"),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return parser.parse_args(argv)


def _output_path_for(input_file: Path, output_dir: Path) -> Path:
    """Derive the output filename from an input filename."""
    return output_dir / input_file.name


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    input_files = sorted(args.input.glob(args.glob_pattern))
    if not input_files:
        logging.warning("No files matched %s in %s", args.glob_pattern, args.input)
        raise SystemExit(1)

    for input_file in input_files:
        output_file = _output_path_for(input_file, args.output_dir)

        if output_file.exists():
            logging.info("Skipping %s — output already exists: %s", input_file.name, output_file.name)
            continue

        logging.info("Processing %s -> %s", input_file.name, output_file.name)
        main(input_file, output_file)