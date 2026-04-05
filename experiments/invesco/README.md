# Process INVESCO FTSE ALL WORLD ETF composition

### Schema version: 2

Output YAML files use the `CompanyRef` reference-universe schema with file-level metadata:
```yaml
schema_version: 2
source_etf: "INVESCO FTSE ALL-WORLD ETF"
as_of_date: "YYYY-MM-DD"
companies:
  - isin: "..."
    cusip: "..."
    raw_name: "..."           # original spreadsheet name
    name: "..."               # normalised for matching [Req. for EDGER]
    weight: 0.0
    cik: "..."                # SEC filer ID (null if non-SEC) [Req. for EDGER]
    sec_eligible: false
    cik_confidence: null
    cik_rationale: null
    ticker: null              # from OpenFIGI [Req. for EDGER]
    exchange_code: null       # from OpenFIGI
    composite_figi: null      # from OpenFIGI
    share_class_figi: null    # from OpenFIGI
    security_type: null       # from OpenFIGI
```

Mutable snapshot data (market cap, revenue, ADV, sector/industry) is **not** stored
in the watchlist YAML — it belongs in a separate dated-snapshot pipeline.

### Pipeline:
1. `scraper.py` — scrape holdings spreadsheet (e.g. `2026-03-31__Die_10_groessten_Positionen-holdings.xlsx`) from Invesco
2. `parser.py` — convert `.xlsx` to `.yaml` with `CompanyRef` schema; populates `raw_name`/`name`, identifiers, and weight; enrichment fields default to null
3. `cik_enricher.py` — resolve CIK from `[ISIN, CUSIP, name]` via OpenFIGI + SEC; persists `ticker`, `exchange_code`, `composite_figi`, `share_class_figi`, `security_type`, `cik_confidence`, `cik_rationale`, `sec_eligible`
4. `cleaner.py` — normalise valid CIKs, deduplicate by `composite_figi` → `share_class_figi` → `isin` → `(cusip, name)`; retains entries without CIK
5. [TBD] `enricher.py` — separate snapshot pipeline for market cap, revenue, sector/industry, ADV