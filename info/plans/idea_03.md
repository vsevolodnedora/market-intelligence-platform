Assume a role of a lead software engineer in quantitative finance and thoroughly and critically analyze the following personal project.

---

# German-tax-aware, settled-cash-aware, low-turnover UCITS ETF allocation/algo trading engine

## Validated and corrected plan

## Scope

* German-tax-aware, settled-cash-aware, low-turnover UCITS ETF allocation/algo trading engine
* Event-aware overlay explicitly deferred to the post-reconciliation phase; long-term target remains an event-driven system
* v1 builds core infrastructure, accounting, replay, reconciliation, and research tooling
* v1 success criterion = accounting, settlement, fee, and tax correctness under live broker statements
* v1 alpha criterion = simple, slow, ETF-native ranking only
* v2 is out of scope here; it may later introduce event-driven execution, ML/NLP overlays, and larger position sizing

---

## Core Ideas

* ETF-native slow ranking is the core engine; event-to-ETF mapping is a later overlay

  * research universe may remain 5–10 ETFs
  * live universe starts at 1–2 funded positions max until economics and reconciliation are proven
  * scale only after live system stability and broker-statement reconciliation are demonstrated
* ETF-native slow ranking first; event-to-ETF mapping second
* long daily or multi-day decision latency
* config defaults to one batch window per day maximum; monthly rebalance by default; threshold rebalance only if score change exceeds churn band and minimum notional is met
* prefer tax-aware accumulating ETFs and longer holding periods
* focus on EUR-denominated, PRIIPs/KID-compliant, Germany-retailable UCITS ETFs
* build the accounting, replay, and reconciliation engine first; paper-trade or micro-trade live; scale only once live fills, fees, settlement, and tax handling match simulation closely
* IBKR TWS API / IB Gateway for order submission, execution capture, and broker reconciliation
* open, free, and scrapable data first; low-cost subscriptions later

---

## Non-negotiable hard constraints

* legal / location: Germany with German Tax ID; employee of a finance firm forbidden to trade:

  * commodity derivatives
  * options on futures
  * swaps
  * ETFs that hold positions in commodities or commodity derivatives, excluding gold and silver ETFs
  * physical commodities
  * single-stock ETFs
* compute: start on a laptop with Intel Core i7 + RTX 2070 GPU during trading hours
* broker: Interactive Brokers, **cash account only**, initial EUR 3,000 for paper-trade + 1 test live; add capital only if live behavior reflects backtest and the system works end-to-end
* TWS API connected to IB Gateway for execution and broker reconciliation
* budget: minimal; strong preference for free / scrapable / open data even if it requires more engineering
* small-account commission floors are a **hard system constraint**
* all order generation must be constrained by:

  * settled cash
  * minimum notional
  * route / venue fees
  * expected spread cost
  * broker cash-account admissibility rules
* no order may be admitted if it would fail live for settled-cash reasons
* universe expansion and capital scaling are blocked until live-vs-sim reconciliation passes

For IBKR cash accounts, the account must have enough cash to cover the trade plus commissions, must have **settled cash** to enter trades, and market / relative / VWAP orders require a **5% cash cushion**. 

The system should therefore treat settled-cash forecasting and order admissibility as broker-facing hard constraints, not backtest-only assumptions. 

---

## Architecture / data model

* keep **one canonical append-only accounting journal** as the sole economic source of truth
* represent fills, fees, taxes, cash movements, FX, corrections, distributions, tax accruals/payments, trade-date obligations, settlement releases, and year-end adjustments as journal entries
* derive positions, trade-date cash, settled cash, tax lots, accrued tax state, and PnL as materialized views from the journal
* retain raw broker events and raw broker statements as immutable external records
* retain a separate immutable **execution-event record** linked to the journal for:

  * strategy decision / order intent
  * pre-trade admissibility decision
  * order submission
  * broker acknowledgement
  * partial fills
  * cancel / replace
  * reject
  * expiry
  * broker correction / restatement
    This execution-event record is not a competing portfolio ledger; it is the audit trail that explains how economic journal entries were reached.

The execution-event layer is required because the draft otherwise cannot fully explain live-vs-sim path divergence.  
IBKR’s published commission rules explicitly state that modified orders are treated as cancellation-and-replacement in relevant cases, and orders persisting overnight may be treated as new orders for commission minimum purposes. 

* venue, routing, commission schedule, settlement cycle, tax classification, **Teilfreistellung regime**, **Basiszins inputs for Vorabpauschale**, and corporate-action rules must be effective-dated reference data
* routing is a first-class dimension: SMART, Xetra/direct, GETTEX/direct, SWB/direct, etc.
* every backtest and live decision must bind to a reference-data version snapshot
* execution policy v1 = one explicitly configured venue/session profile only

  * start with: **Xetra-only, core continuous session**
  * treat **extended retail trading windows** as a separate optional session profile, never as an implicit default
  * later expansion may add venue-aware execution with routing-specific fee/slippage models
* forbid mixed “single-venue simulation / multi-venue live routing” semantics

Deutsche Börse currently defines Xetra core trading at **09:00–17:30 CET**.  
Extended Xetra Retail Service is separate and explicitly time-bounded before and after core hours, so it must be modeled as a distinct session profile with distinct execution assumptions. 

* separate trading-calendar logic from settlement-calendar logic
* model settlement regime as effective-dated by venue and instrument
* encode the current regime explicitly rather than hardcoding it forever:

  * current German cash-market baseline = **T+2**
  * calendar edges such as **settlement-open / trading-closed** days must be modeled explicitly
  * future **T+1** migration is a scheduled regime change, not a compile-time constant

---

## Main Complexities

### Economics

* smart-routed and direct-routed pricing differ materially
* exchange, clearing, and regulatory fees are route-specific and venue-specific
* fixed minimum commissions dominate at small ticket sizes
* reactive ETF rotation is uneconomic at the initial account size
* order admission must use all-in expected cost, not raw commission alone

For Germany at IBKR, current fixed pricing is 
* **0.05% with EUR 3 minimum** for SmartRouting, 
* **0.10% with EUR 4 minimum** for general direct routing, 
* **0.12% with EUR 6 minimum** for **SWB**, and 
* **0.05% with EUR 3 minimum** for **GETTEX**, 
before venue-specific third-party fees. 

IBKR’s venue pages also show materially different third-party costs across venues, including **0 exchange / 0 clearing fee** at GETTEX and **0.10% ETF exchange fee plus EUR 0.01 regulatory fee** at SWB.

* direct API routing fee semantics must be modeled correctly
* if later using directed API orders, pricing-plan behavior must be explicit in reference data and execution tests

IBKR states that **directed API orders cannot use the Tiered fee structure**; SmartRouted API orders can use either Tiered or Fixed.  
That means routing policy and commission-plan semantics interact and must be represented directly in the execution cost model. 

### Signal dilution

* single-name EDGAR / issuer-news signals dilute heavily at ETF level
* holdings-based event propagation may be useful later, but it starts as an overlay, not as the core ranker
* v1 alpha remains slow, ETF-native, low-turnover, and deliberately simple
* v2, out of scope here, will add general news / headlines that are hard to map to ETF level even with LLM processing / ML

### Tax complexity

* German Investmentsteuergesetz treatment is first-order
* accumulating ETFs still fall under **Vorabpauschale**
* applicable **Teilfreistellung** must be modeled as dated state, not timeless metadata
* a change in applicable Teilfreistellung rate, or loss of eligibility, must trigger the statutory deemed-sale / deemed-repurchase treatment
* annual **Basiszins** must be loaded as dated external reference data, never hardcoded

German law places **Vorabpauschale** in **§18 InvStG**, **Teilfreistellung** in **§20 InvStG**, and changes in applicable Teilfreistellung regime in **§22 InvStG**, including deemed sale / reacquisition semantics.  
The **Basiszins** is published separately by the BMF and must therefore enter the system as dated reference data.  

### Time integrity

* event, holdings, price, contract, session, settlement, and tax data must all be timestamped and replayable
* without strict timestamp discipline the backtest will leak information
* every simulation run and live decision must bind to:

  * market data version
  * holdings / metadata version
  * reference-data snapshot
  * session profile
  * settlement regime snapshot

### Execution-state integrity

* economic correctness alone is insufficient; execution-path correctness must also be replayable
* the system must preserve the causal chain from strategy intent to broker outcome
* partial fills, broker rejects, cancel/replace chains, expiries, and statement corrections must be auditable and reproducible
* reconciliation must explain not only **what** changed economically, but **why** the live execution path differed from simulated intent

---

## Workflow / development stages

### Task 1 — Build reference-data pipeline

Start with effective-dated reference data for:

* venue
* routing
* commission schedule
* third-party venue fees
* settlement cycle
* session profile
* tax classification
* Teilfreistellung regime
* Basiszins inputs
* corporate-action rules
* pricing-plan / routing interaction rules

Germany venue fees must stay route-specific, tax inputs must stay dated, and settlement rules must be venue/instrument effective-dated rather than broker-help-text assumptions. 

### Task 2 — Build raw broker ingest + immutable archives

Ingest and store as immutable records:

* IBKR raw broker events
* order status events
* contract metadata
* broker statements
* statement revisions / corrections
* local strategy decisions and order intents
* local pre-trade admissibility results
* submission / acknowledgement / reject / cancel / replace / expiry / fill events

The plan must preserve both broker-originated facts and local OMS-originated facts so that live execution can be reconstructed without relying on derived portfolio state alone.

### Task 3 — Build canonical accounting journal

Implement the append-only journal first. No parallel portfolio ledger may become a competing source of truth.

Journal posting rules must convert immutable external facts into economic entries for:

* fills
* commissions
* exchange / regulatory fees
* taxes
* cash movements
* FX
* distributions
* settlement releases
* tax accruals
* year-end adjustments
* broker corrections

### Task 4 — Build reconciliation views and harness

Derive from the journal:

* positions
* trade-date cash
* settled cash
* tax lots
* accrued tax state
* realized / unrealized PnL
* fee totals
* cash movements

Reconcile these views against broker statements before any strategy component is treated as live-ready.  
Reconciliation must include both economic state and execution-path explanations.

### Task 5 — Create ETF universe (research)

Research-universe eligibility should require:

* UCITS structure
* retail marketability in Germany
* current PRIIPs KID
* EUR trading line
* supported execution venue/session metadata
* tax-classification evidence sufficient for dated Teilfreistellung handling
* legal requirements for trading (see *Non-negotiable hard constraints*)

Use **issuer KID / prospectus / official fund documents / exchange metadata** as primary eligibility sources.  
Use **BaFin’s investment-funds database** as a control source for Germany marketability, not as the sole authority.

BaFin states that its database is published voluntarily, is in principle updated daily, may lag, and carries no liability for completeness or correctness.  
EU materials confirm the PRIIPs KID timing relevant to UCITS from **1 January 2023**.

### Task 6 — Data collection + preprocessing

Collect only what v1 needs:

* OHLCV / total-return-capable price inputs
* ETF metadata
* distribution history
* fund classification evidence
* tax-reference inputs
* corporate-action data
* venue/session metadata

Do not build event / NLP pipelines into v1 core but build ports to prepare for them (adapters are only in v2).

### Task 7 — Data normalization and stores

Create normalized stores for:

* market data
* reference data
* immutable broker / OMS events
* accounting journal
* derived views
* research outputs

Sketch normalization ports for data expected to come in v2. Do not implement adaptors until v2. 

All stores must support versioning and replay by as-of timestamp.  

### Task 8 — Build feature engine

Implement an extendable but intentionally narrow v1 feature engine:

* slow ETF-native features only
* no holdings-propagated issuer-event model in core
* no NLP dependency in v1 alpha path

General infrastructure should allow for future ports / adaptors for LLM, NLP, etc features coming in v2.

### Task 9 — Build simulation / backtest engine

Bind every decision to:

* reference-data snapshot
* venue/session profile
* settlement assumptions
* commission model
* spread model
* tax state

The simulator must enforce the same venue/session profile, settlement regime, and fee model used in live. It must also enforce the same settled-cash admissibility logic used by the live execution layer.

### Task 10 — Build execution engine

Keep v1 execution pinned to one configured venue/session profile. Do not allow mixed single-venue simulation and multi-venue live routing.

Execution must include deterministic pre-trade admission checks for:

* settled cash forecast
* broker cash cushion where applicable
* minimum notional
* route-specific commission floor
* third-party venue fees
* expected spread cost
* session validity
* instrument eligibility
* strategy churn bands

Execution logging must preserve the full path from strategy decision to broker result.

### Task 11 — Build strategy factory + automatic rank-and-rebalance

Start with rule-based, ETF-native, slow ranking only.

Scheduled rebalance (monthly, default). Threshold rebalance is allowed only if:

* signal delta clears churn bands
* minimum notional is met
* settled cash is sufficient
* fee/spread constraints are satisfied
* turnover remains economically justified at the current account size

### Task 12 — Start paper-trade / micro-live phase

Begin with paper-trade and then micro-live using the exact same:

* reference-data snapshots
* venue/session profile
* order-admission logic
* accounting journal
* reconciliation harness

No strategy broadening before reconciliation is consistently clean.

### Task 13 — Evaluation

Acceptance gate for v1:

* broker statement replay reconciles cash, positions, fees, and taxes with no unexplained residuals
* no simulated order is admitted that would fail live for settled-cash reasons
* year-end tax processing, including Vorabpauschale inputs and dated fund-tax-classification state, is reproducible from reference data
* changes in Teilfreistellung regime are handled explicitly, including deemed-sale / deemed-repurchase treatment where applicable
* execution-path replay explains live outcomes from order intent through broker status chain to final journal state
* broker corrections or restatements do not create unexplained breaks in replay or reconciliation

### Task 14 — Scale up (v1 -> v2) [SCAFFOLD! TO BE USED IN v1 ONLY AS A CONTEXT FOR WHERE THE PROJECT IS HEADING]

Scale capital, turnover, and universe size only after the v1 acceptance gate is passed.  
Transition to event-reactive trading (not microstructure-heavy system, which is out of scope for v2).  
v2 idea is to transition to a portfolio-reactive event-driven system: react to intraday price moves, auction states, NAV dislocations, spreads, news, or broker/risk events, but still trade infrequently and in size-constrained ETF tickets.
* durable event-processing layer
* runtime state machine between raw events and economic postings
* expand the event taxonomy beyond fills and statements
* extend batch strategy evaluation with stateful incremental strategy logic:
  * consume events continuously -> update state incrementally -> emit intents only when conditions are met
* order management and execution management subsystem.
  * aka: INTENT -> ADMITTED -> SUBMITTED -> ACKED -> PARTIALLY_FILLED -> FILLED / CANCELLED / REJECTED / EXPIRED
* upgrade the data layer (become operational feeds)
* add an external-event interpretation layer
* move from bar-based backtesting to event-driven simulation


Validate your research and return a concise, technical, accurate report.