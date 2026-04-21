Assume a role of a senior software engineer in quantitative finance and thoroughly and critically analyze the following personal project.


## Scope

* German-tax-aware, settled-cash-aware, low-turnover UCITS ETF allocation/algo trading engine 
* Event-aware overlay explicitly deferred to post-reconciliation phase (end goal is to have an event-driven system)
* v1 builds core infrastructure / research tools
* v1 success criterion = accounting, settlement, fee, and tax correctness under live broker statements
* v1 alpha criterion = simple, slow ETF-native ranking only
* v2 [out of scope here] will move execution to be more event-driven, introduce ML and NLP for news processing, scale up positions

---

## Core Ideas:

* ETF-native slow ranking is the core engine; event-to-ETF mapping is a later overlay
  * research universe -- may remain 5-10 ETFs
  * live universe -- starts at 1-2 funded positions max until economics and reconciliation are proven
  * scale only after the live system is stable and the accounting/execution stack reconciles cleanly against broker records.
* **ETF-native slow ranking first; event-to-ETF mapping second**. 
* long daily or multi-day decision latency
* config sets default: one batch window per day max; monthly rebalance by default; threshold rebalance only if score change exceeds churn band and minimum notional is met
* prefers tax-aware accumulating ETF + hold-longer behaviour
* considers EUR-denominated, PRIIPs/KID-compliant UCITS ETFs
* build the **research and accounting engine first**, paper-trade / micro-trade live, and scale only once live fills, fees, settlement, and tax handling match simulation closely
* IBKR TWS API / IB Gateway for order execution and operational validation
* **open / free / scrapable data sources first, low-cost subscriptions later**

---

## Non-negotiable (hard) constraints

* legal / location: Germany with German Tax ID; employee of a finance firm that is forbidden to trade:
  * commodity derivatives, options on futures, swaps, ETFs that hold positions in Commodities or Commodity Derivatives (excluding gold and silver ETFs), physical commodities, single stock ETFs
* compute: starting with laptop with Intel Core i7 + RTX 2070 GPU running during the trading hours
* broker: Interactive Brokers, **Cash account only**, initial 3000 EUR for paper-trade + 1 test live; will add more cash only if live behaviour reflects backtest and system works as expected 
* TWS API connected to IB Gateway for order execution and broker reconciliation
* budget: minimal with strong preference for free / scrapable / open data sources even if they require more processing / code
* treat small-account commission floors as a hard system constraint, not just a cost assumption
* all order generation must be constrained by settled cash, minimum notional, venue/routing fees, and expected spread cost
* universe expansion and capital scaling are blocked until live-vs-sim reconciliation passes


---

## Architecture / data model

* replace multiple competing ledgers with one canonical append-only accounting journal
* represent fills, fees, taxes, cash movements, FX, corrections, distributions, tax accruals/payments, trade-date obligations, settlement releases, and year-end adjustments as journal entries
* derive positions, trade-date cash, settled cash, tax lots, accrued tax state, and PnL as materialized views from the journal
* retain raw broker events and raw broker statements as immutable external records
* venue, routing, commission schedule, settlement cycle, tax classification, **Teilfreistellung regime**, **Basiszins inputs for Vorabpauschale**, and corporate-action rules must be effective-dated reference data. 
* routing is a first-class dimension: SMART, Xetra/direct, GETTEX/direct, SWB/direct, etc.
* every backtest and live decision must bind to a reference-data version snapshot
* execution policy v1 = default to one explicitly configured venue/session profile
  * start with: **Xetra-only, core continuous session** for model/backtest/live consistency
  * treat **extended retail trading windows** as a separate optional session profile, not an implicit default
  * later expand as venue-aware execution with routing-specific fee/slippage models. Deutsche Börse currently describes core Xetra trading at **09:00–17:30 CET**, while also operating an Extended Xetra Retail Service outside that core session.

* forbid mixed "single-venue simulation / multi-venue live routing" semantics
* separate trading-calendar logic from settlement-calendar logic
* model settlement regime as effective-dated by venue and instrument
* current regime remains versioned reference data; calendar edges such as **settlement-open / trading-closed** days must be modeled explicitly. The 2026 Xetra/Frankfurt calendar explicitly marks **24 Dec** and **31 Dec** as settlement days while trading is closed.
* future T+1 migration is a scheduled but not hardcoded regime change; ESMA currently recommends **11 October 2027** as the EU transition date.

---

## Main Complexities [revised where needed]

### **economics**:

   * smart-routed / direct-routed pricing;
   * exchange / clearing / regulatory fees;
   * fixed minimum commissions dominate at small ticket sizes.
   * On Germany venues at IBKR, fixed pricing is currently **0.05% smart-routed with EUR 3 minimum**; direct-routed pricing is **not uniform** and currently breaks out as **0.10% with EUR 4 minimum** for “all other direct-routed”, **0.12% with EUR 6 minimum** for **SWB**, and **0.05% with EUR 3 minimum** for **GETTEX**, before third-party venue fees.
   * reactive ETF rotation is therefore uneconomic at the starting size.

### **signal dilution**:

   * single-name EDGAR / news signals dilute materially at ETF level;
   * holdings-based event propagation is useful later, but should begin as an overlay rather than the core ranking engine

### **tax complexity**:

   * German Investmentsteuergesetz treatment is first-order;
   * accumulating ETFs still fall under **Vorabpauschale** under **§18**;
   * applicable **Teilfreistellung** follows **§20**, and changes in the applicable Teilfreistellung rate or loss of eligibility must be handled explicitly under **§22** rather than treated as timeless static metadata;
   * the annual **Basiszins** is published separately by the **BMF** and must be loaded as reference data, not hardcoded. ([Gesetze im Internet][2])

### **time integrity**:

   * event, holdings, price, contract, and tax data must be timestamped and replayable;
   * without strict timestamp discipline the backtest will leak information

---
---

## Workflow / development stages (scaffold) [revised]

### **Task 1** Build Reference data pipeline

Start with effective-dated reference data for venue, routing, commission schedule, settlement cycle, session profile, tax classification, Teilfreistellung regime, Basiszins inputs, and corporate-action rules.  
Germany venue fees must stay route-specific, and tax inputs must stay dated. 

### **Task 2** Build raw broker ingest + immutable archives

Ingest IBKR raw broker events, contract metadata, and broker statements as immutable external records.

### **Task 3** Build canonical accounting journal

Implement the append-only journal first; no parallel portfolio ledger should be allowed to become a competing source of truth.

### **Task 4** Build reconciliation views and harness

Derive positions, settled cash, tax lots, fees, and PnL from the journal and reconcile them against broker statements before strategy work is considered “live-ready”.

### **Task 5** Create ETF universe (research)

Research-universe eligibility should require: UCITS structure, retail marketability in Germany, current PRIIPs KID, EUR trading line, and supported execution venue/session metadata.  
BaFin’s retail investment-funds database is a valid control source for Germany marketability, and official EU materials reflect that PRIIPs KIDs apply to UCITS from 1 January 2023.

### **Task 6** Data collection + preprocessing (individual sources)

### **Task 7** Data normalization and stores (databases, files)

### **Task 8** Build Feature Engine (extendable)

### **Task 9** Build simulation / backtest engine

Bind every decision to a reference-data snapshot and enforce the same venue/session profile, settlement assumptions, and fee model used in live.

### **Task 10** Build execution engine

Keep v1 execution pinned to one configured venue/session profile; do not allow mixed single-venue simulation and multi-venue live routing.

### **Task 11** Build strategy factory + automatic rank-and-rebalance

Start with rule-based, ETF-native, slow ranking only. Monthly rebalance remains the default; threshold rebalance is only allowed if the signal delta clears churn bands, minimum notional, settled-cash, and fee/spread constraints.

### **Task 12** Start Paper-trade / micro-live phase

### **Task 13** **Evaluation**

Acceptance gate for v1:

* broker statement replay reconciles cash, positions, fees, and taxes with no unexplained residuals;
* no simulated order is admitted that would fail live for settled-cash reasons;
* year-end tax processing, including Vorabpauschale inputs and fund tax classification state, is reproducible from dated reference data.

### **Stage 14** Scale up (after passing acceptance gate)


Validate your research and return a concise, technical, accurate report.