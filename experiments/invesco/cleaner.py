"""
clean_watchlist.py

Reads a watchlist YAML file, validates each entry's CIK, normalises valid
CIKs to zero-padded 10-digit integers, drops invalid entries, and writes
the clean result to an output file.

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


def main(input_path: Path, output_path: Path) -> None:
    log.info("Reading %s", input_path)
    with input_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict) or "companies" not in data:
        sys.exit("ERROR: expected a YAML mapping with a top-level 'companies' key.")

    companies: list = data["companies"]
    total = len(companies)
    log.info("Loaded %d entries", total)

    valid_entries: list[dict] = []
    dropped_entries: list[dict] = []

    for entry in companies:
        cik = entry.get("cik")
        if is_valid_cik(cik):
            entry["cik"] = normalise_cik(cik)
            valid_entries.append(entry)
        else:
            dropped_entries.append(entry)
            ticker = entry.get("ticker") or "<unknown>"
            name = entry.get("name") or "<unknown>"
            log.warning(
                "[DROP] %-20s %-40s cik=%r (%s)",
                ticker, name, cik, invalid_reason(cik),
            )

    log.info("Valid: %d  |  Dropped: %d", len(valid_entries), len(dropped_entries))

    # ------------------------------------------------------------------
    # Deduplicate by CIK – keep the first occurrence, drop later dupes
    # ------------------------------------------------------------------
    seen_ciks: set[int] = set()
    unique_entries: list[dict] = []
    duplicate_entries: list[dict] = []

    for entry in valid_entries:
        cik = entry["cik"]
        if cik in seen_ciks:
            duplicate_entries.append(entry)
            ticker = entry.get("ticker") or "<unknown>"
            name = entry.get("name") or "<unknown>"
            log.warning(
                "[DUP]  %-20s %-40s cik=%d (duplicate – keeping first occurrence)",
                ticker, name, cik,
            )
        else:
            seen_ciks.add(cik)
            unique_entries.append(entry)

    if duplicate_entries:
        log.info(
            "Duplicates removed: %d  |  Unique: %d",
            len(duplicate_entries), len(unique_entries),
        )
    else:
        log.info("No duplicate CIKs found")

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

    log.info("Validating saved file")
    with output_path.open("r", encoding="utf-8") as fh:
        check = yaml.safe_load(fh)

    assert isinstance(check, dict), "Root is not a mapping."
    assert "companies" in check, "Missing 'companies' key."
    saved = check["companies"]
    assert len(saved) == len(unique_entries), (
        f"Count mismatch: wrote {len(unique_entries)} but read back {len(saved)}."
    )
    for i, entry in enumerate(saved):
        cik = entry.get("cik")
        assert is_valid_cik(cik), (
            f"Invalid CIK at index {i}: {cik!r} (ticker={entry.get('ticker')!r})"
        )

    # Verify uniqueness in the saved file
    saved_ciks = [e["cik"] for e in saved]
    assert len(saved_ciks) == len(set(saved_ciks)), (
        "Duplicate CIKs found in saved file!"
    )

    log.info("All %d entries have valid, unique CIKs ✓", len(saved))
    log.info(
        "Summary — Input: %d | Invalid: %d | Duplicate: %d | Output: %d",
        total, len(dropped_entries), len(duplicate_entries), len(unique_entries),
    )


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
        default="*__Die_10_groessten_Positionen-holdings.yaml",
        help="Glob pattern for input files inside the input directory",
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
    """Derive the output filename from an input filename.

    e.g. 2026-03-31__Die_10_groessten_Positionen-holdings.yaml
      -> 2026-03-31_watchlist.yaml
    """
    date_prefix = input_file.name.split("__")[0]          # "2026-03-31"
    return output_dir / f"{date_prefix}_watchlist.yaml"


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