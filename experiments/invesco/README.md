# Process INVESCO FTSE ALL WORLD ETF composition

### Pipeline:
1. `scraper.py` scrape "2026-03-31__Die_10_groessten_Positionen-holdings.xlsx" like file from invesco
2. `parser.py` convert ".xlsx" to ".yaml" file and add new fields for subsequent enrichment
3. `cik_enricher.py` infer CIK from [ISIN, cusip, name] if possible
4. `cleaner.py` dedup CIK / drops those that were not identified
5. [TBD]
