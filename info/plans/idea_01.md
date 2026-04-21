Assume a role of a senior software engineer in quantitative finance and critically analyze the following personal project. 

# Event-driven algorithmic ETFs trading system

### Core Ideas:   

* event-driven ETF rotation/ranking system; start with 5-15 cleanest, most liquid EUR trading, accuumulating UCITS ETFs lines with stable exposure definitions selected out of available Xetra’s ETF, scale up later when system matured. 
* long daily or multi-day decision latency
* controlled execution latency
* prefers 
  * batched, low-frequency execution (compress the live strategy until order notional is large enough)
  * penalizes reactive multi-leg turnover, 
  * tax-aware accumulating ETF + hold longer behaviour. 
* Considers EUR-denominated, PRIIPs/KID-compliant UCITS ETFs; event-to-feature pipeline first; simple rules or supervised ranking first + ML later; 

* build the research engine first, paper-trade / micro-trade live, and scale only once position sizes are in the low-thousands of euros rather than low-hundreds.

### Non-negotiable (hard) constraints

* legal / location: Germany with German Tax ID, 
* compute: starting with laptop with Intel Core i7 + RTX 2070 GPU
* broker: Interactive Brokers (Cash, -- not Margin account) initial 2000 EUR for paper-trade + 1 test live; adding more if system if stable and live reflects backtest
* TWS API connected to IB Gateway for both market data and order execution
* budget: minimal with strong preference for free / scrapable / open data sources even if they require more processing / code 
* design: 
  * clear and accurate tax/fees/etc implementation and tracing for compliance / reporting purposes (e,g., early tax audits in Germany)

### Main Complexities

1. **economics**: smart-routed/direct-routed pricing; exchange/clearing/regulatory fees -- reactive ETF rotation uneconomic at the starting size; broker minimum commissions dominate spreads/market impact on liquid products.
2. **signal dilution**: single-name events dilute hard at ETF level (EDGAR/GDELT-style event signals should start as a secondary overlay, not the primary ranking engine)
3. **tax complexity**: German taxes: Investmentsteuergesetz, Vorabpauschale, etc -- tax engine first is mandatory.

### Data Sources / Options

* **Universe, venue, liquidity, and tradability**: use Deutsche Börse / Xetra (free/public) as the source of truth for listed ETFs, venue metadata, trading hours, trading statistics, and XLM/iXLM liquidity measures. 
* **Fund metadata, holdings, KID, factsheet, prospectus**: use the issuer websites directly — iShares, Xtrackers, Amundi, Vanguard, State Street/Invesco, etc. For UCITS ETFs, these pages typically expose holdings, KID/KIID, factsheets, and prospectus.
* **Prices**: 
  * starting idea: scrape Börse Frankfurt / Deutsche Börse instrument pages for historical prices/volumes on your tiny universe
* **Macro and rates**: 
  * ECB Data Portal API and Bundesbank SDMX API (official-source APIs with normal operational discipline) + FRED for convenience
* **Event overlay**: SEC EDGAR APIs for U.S. filings and GDELT for broad news/event coverage.
* **German tax inputs**
  * Investmentsteuergesetz text plus the BMF investment tax pages / annual basis-rate publication for the tax-engine reference data.
* **Identifier mapping / security master**: OpenFIGI to map ISINs and other identifiers into a cleaner internal contract master
* **Contract validation, live execution, fills, cash/settlement, and reconciliation**: IBKR real-time data (~17 EUR/Month; 100 lines max)

### Workflow / development stages

1. Universe curation: 5-15 UCITS ETFs, EUR trading line, KID available, accumulating preferred (helps behaviorally, but it does not eliminate tax mechanics), high AUM/liquidity, low spread/XLM, simple exposures.
2. Streaming (EDGAR) + scheduled (GDELT) + other sources data collection + preprocessing
  * EDGAR example: a filing/news event must be mapped to issuer -> sector/theme -> ETF holdings/weights -> expected multi-day ETF effect
3. Append-only event store, price store, holdings snapshot store, contract master, settlement ledger, and tax-lot ledger before any serious live trading.
4. Scheduled / alert-triggered event pipeline: EDGAR + macro/calendar/news sources, deduplicated and timestamped
5. Exposure graph: map events into ETF-level scores through holdings / sector / geography / style exposure.
6. ML strategy development with position-sizing + slippage + tax/fees/penalties awareness. 
  * Build ledger stack before the model stack. Contract master, calendar/settlement rules, append-only fills, settled-cash ledger, tax-lot ledger, and year-end tax adjustments should be production-grade before serious live trading.
  * account for cash-account turnover limits, trading frictions, and tax drag. 
  * prefers batched, low-frequency execution and penalizes reactive multi-leg turnover, hold longer behaviour. 
  * account for German stock settlement as two days (not hardcoded as expected to change in 2027)
  * account for annual tax drag in Germany
7. Automatic rank-and-rebalance: when new data is available; batch windowing, 1–2 order cycles max, hysteresis bands to suppress churn.
8. After-fee, after-tax, settled-cash-aware simulator / backtest.
9. Execution: cash-account-aware, limit orders, settled-cash checks, no intraday dependency.
10. Evaluation: after-fee, after-tax-aware metrics, turnover budget, realized-gain tracking.

Validate your research and return a concise, technical, accurate report. 