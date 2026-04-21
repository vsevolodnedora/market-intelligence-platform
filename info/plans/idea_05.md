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
* config defaults to one batch decision window per day maximum
* at the initial live account size, live trading defaults to **threshold-first, low-turnover rebalancing with a quarterly review cadence**

Monthly evaluation may still be run for research, but live orders are admitted only when score change clears a wide churn band, minimum notional is met, 
and estimated **all-in** cost remains economically justified after broker commission minimums, expected spread, and **venue / route / order-class / session-specific** third-party fees. 
For Xetra in particular, fee treatment is not determined by venue and route alone: 
IBKR distinguishes retail vs standard fee treatment, and transaction fees may be waived for certain retail orders executed on Xetra under Best Execution Policy, while direct-routed retail orders are charged regular retail rates. 
Therefore, v1 default = conservative non-waived fees post-validation upgrade = pinned validated Xetra order-class profile

At current IBKR Germany pricing, the commission minima and venue fees are large enough that small-ticket monthly rotation is usually uneconomic: 
SmartRouting fixed is 0.05% with a EUR 3 minimum, generic direct routing is 0.10% with a EUR 4 minimum, SWB direct is 0.12% with a EUR 6 minimum, 
and GETTEX direct is 0.05% with a EUR 3 minimum; SWB ETFs also carry a 0.10% exchange fee plus EUR 0.01 regulatory fee, while GETTEX currently shows zero exchange and clearing fees.

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

All order generation must be constrained by:

* settled cash
* **cash already reserved for admitted but non-terminal orders**
* minimum notional
* route / venue / **order-class / session-profile** fees
* expected spread cost
* broker cash-account admissibility rules

No order may be admitted if it would fail live for settled-cash reasons or if admitting it would double-count cash already committed to another live working order.

* universe expansion and capital scaling are blocked until live-vs-sim reconciliation passes

For IBKR cash accounts, order admissibility is a broker-facing hard constraint.  
The engine must require enough cash to cover principal, commissions, taxes, and venue fees, and it must require **settled cash** to enter buys. 
The engine must also maintain a deterministic **reserved-cash state** for admitted but non-terminal orders so that two locally admitted orders cannot consume the same settled cash before broker-side rejection; 
this reserved-cash state is an OMS control derived from execution events and is released only by terminal order outcomes or explicit quantity reduction.
Settlement timing must **not** be hardcoded from a single broker help page: instead, the system maintains an effective-dated **broker / venue / instrument / route / order-policy settlement table**, 
uses that table for order admission and forecasting, and validates it continuously against live broker statements and Flex reports. 
Any additional broker-side cash buffers for specific order types must be represented as configurable broker rules and must be validated in paper-trade or micro-live before being relied on in simulation or live admission. 

IBKR's public docs currently support the need for this wording: 
one cash-account page says German stock cash becomes available after two days and that cash accounts must have settled cash to enter trades, 
while another IBKR settled-cash glossary page still uses a generic stock-settlement example of trade date + 3 days.

---

## Architecture / data model

* keep **one canonical append-only accounting journal** as the sole economic source of truth
* represent fills, fees, taxes, cash movements, FX, corrections, distributions, tax accruals/payments, trade-date obligations, settlement releases, and year-end adjustments as journal entries
* Derive **positions, trade-date cash, settled cash, tax lots, statutory tax state, broker-posted tax cashflows, accrued tax state, and PnL** as materialized views from the journal. 
* Derive **reserved cash** and **cash available to admit new orders** from the execution-event layer combined with journal state. 
* The journal remains the sole economic source of truth; the reserved-cash view is an operational control view, not a competing ledger. 
* Statutory tax state and broker-posted tax cashflows must remain separate because German fund-tax obligations are determined by dated legal/tax reference data, while broker postings are external booking events that may occur on a different timetable.
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
IBKR's published commission rules explicitly state that modified orders are treated as cancellation-and-replacement in relevant cases, and orders persisting overnight may be treated as new orders for commission minimum purposes.

* Venue, routing, commission schedule, settlement cycle, **order class**, **session profile**, tax classification, **Teilfreistellung regime**, **Basiszins inputs for Vorabpauschale**, and corporate-action rules must be effective-dated reference data. 
  * Fee reference data must be modeled at least by **route × venue × order class × session profile × effective date**, because venue fees are not fully determined by route and venue alone. 
  * The **document-evidence registry** must be first-class reference data and store, per eligibility or tax decision, the source document URI, retrieval timestamp, document hash, parsed validity interval, parser/version identifier, and the specific fields extracted for compliance or tax use. 
* routing is a first-class dimension: SMART, Xetra/direct, GETTEX/direct, SWB/direct, etc.
* every backtest and live decision must bind to a reference-data version snapshot
* execution policy v1 = one explicitly configured **venue / phase profile** only
* start with: **Xetra-only, continuous-trading only**
* represent trading phases explicitly as named states: **pre-trading, opening auction, continuous trading, closing auction, extended retail trading**
* treat **Extended Xetra Retail Service** and **Börse Frankfurt** trading as separate optional profiles with their own liquidity, spread, and fee assumptions
* forbid mixed "continuous-session simulation / auction-session or retail-session live routing" semantics

Deutsche Börse currently publishes Xetra continuous trading as 09:00–17:30 CET, with Extended Xetra Retail Service separately published as 08:00–08:55 CET and from 17:30 to 22:00 CET.

* separate trading-calendar logic from settlement-calendar logic
* model settlement regime as effective-dated by **broker, venue, instrument, and route policy where required**
* do **not** encode "German cash market = T+2" as a timeless system fact
* instead encode an initial broker settlement-policy reference row and mark it provisional until reconciled to live broker statements
* calendar edges such as **settlement-open / trading-closed** days must be modeled explicitly
* future regime changes such as **T+1** migration are scheduled reference-data changes, never compile-time constants

Deutsche Börse explicitly publishes dates where there is no trading but settlement remains open, and IBKR's own public settlement references are not fully consistent enough to justify a hardcoded constant.

---

## Main Complexities

### Economics

* smart-routed and direct-routed pricing differ materially
* exchange, clearing, and regulatory fees are route-specific and venue-specific
* fixed minimum commissions dominate at small ticket sizes
* reactive ETF rotation is uneconomic at the initial account size
* order admission must use all-in expected cost, not raw signal movement

For Germany at IBKR, current fixed pricing is route-dependent and must be modeled explicitly in reference data:  
* **0.05% with EUR 3 minimum** for SmartRouting fixed, 
* **0.10% with EUR 4 minimum** for general direct routing fixed, 
* **0.12% with EUR 6 minimum** for **SWB** direct fixed, 
* **0.05% with EUR 3 minimum** for **GETTEX** direct fixed, before venue-specific third-party fees. 

Third-party fees differ materially by venue: 
* **GETTEX** currently publishes zero exchange and zero clearing fees, 
* **SWB ETFs** publish a **0.10% exchange fee** plus **EUR 0.01 regulatory fee**, 
* **Xetra** publishes separate exchange, clearing, and regulatory fees. 

At the initial account size, minimum commissions alone can consume tens of basis points per side on small tickets, so order admission and rebalance logic must optimize for **all-in cost**, not raw signal movement.

This is the main reason the live default should be threshold-driven and low-turnover.

* direct API routing fee semantics must be modeled correctly
* if later using directed API orders, pricing-plan behavior must be explicit in reference data and execution tests
* **directed API orders cannot use Tiered pricing; SmartRouted API orders may use Tiered or Fixed**
* commission-minimum logic must also account for **cancel/replace** and, where applicable, **overnight persistence** semantics that can cause orders to be treated as new orders for minimum-commission purposes

IBKR states both of those points explicitly in its commission materials.

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
* **venue phase profile**
* **order class / fee classification**
* **trading calendar**
* **settlement calendar**
* **broker settlement policy**
* routing
* commission schedule
* **venue-fee waivers / retail-order fee rules where applicable**
* third-party venue fees
* **order-type-specific broker cash-buffer rules**
* pricing-plan / routing interaction rules
* **legal-compliance allowlist / denylist at ISIN level**
* **document-evidence registry with source URI, retrieval timestamp, document hash, validity interval, parser/version, and extracted decision fields**
* tax classification
* Teilfreistellung regime
* Basiszins inputs
* corporate-action rules

### Task 2 — Build raw broker ingest + immutable archives

Ingest and store as immutable records:

* IBKR raw broker events from TWS / IB Gateway
* order status events
* contract metadata
* broker statements
* **IBKR Flex Query outputs, at minimum daily Activity and Trade Confirmation reports**
* statement revisions / corrections
* local strategy decisions and order intents
* local pre-trade admissibility results
* submission / acknowledgement / reject / cancel / replace / expiry / fill events
* local reference-data snapshot identifiers bound to each decision and admission event

The archive must preserve both broker-originated facts and local OMS-originated facts so that live execution can be reconstructed without relying on derived portfolio state alone. 
Operationally, the system should treat **TWS / IB Gateway events as intraday execution truth** and treat **Flex Activity / Trade Confirmation reports as delayed reconciliation truth**. 
Activity Flex data is updated once daily at close of business; Trade Confirmation Flex data updates intraday but is typically delayed by several minutes, so Flex must confirm and reconcile live state rather than drive it. 

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
* **currently valid PRIIPs KID**
* EUR trading line on an explicitly modeled venue / phase profile
* supported execution venue / phase metadata
* tax-classification evidence sufficient for dated Teilfreistellung handling
* **explicit ISIN-level compliance pass** against the legal and employer restrictions in scope
* retained documentary evidence linking each eligibility decision to issuer, exchange, BaFin, and tax-reference sources, **including stored document hashes and validity windows**

Use **issuer KID / prospectus / official fund documents / exchange metadata** as the primary eligibility sources.  
Use **BaFin's investment-funds database** only as a corroborating control source for Germany marketability, never as the sole authority, 
because BaFin states that the database is published on a voluntary basis, is generally updated daily, and excludes liability for completeness or correctness; 
delays in reflecting current status are explicitly possible. 
PRIIPs/KID validation must be date-aware, because UCITS available to retail investors have required PRIIPs KIDs since 1 January 2023.

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
* venue / phase profile
* **order-class / fee-classification snapshot**
* **broker settlement-policy snapshot**
* trading-calendar snapshot
* settlement-calendar snapshot
* commission model
* third-party-fee model
* spread model
* tax state
* legal-compliance policy snapshot
* **reserved-cash state for working orders**

The simulator must enforce the same venue / phase gating, broker settlement policy, fee model, reserved-cash logic, and admissibility logic used in live. 
If the configured live route cannot prove its fee classification from validated live evidence, the simulator must charge a conservative fee path rather than an optimistic one.  
A simulation result is invalid if it admits any order that the active cash-account policy, reserved-cash policy, venue-phase policy, fee-classification policy, or compliance policy would block in live trading.  

### Task 10 — Build execution engine

Keep v1 execution pinned to one configured venue / phase profile. Do not allow mixed single-phase simulation and multi-phase or multi-venue live routing.

Execution must include deterministic pre-trade admission checks for:

* **ISIN-level legal / compliance allowlist**
* venue / phase validity
* settled-cash forecast under the active broker settlement policy
* **cash already reserved for admitted but non-terminal orders**
* configured broker cash buffers for order types that require them
* minimum notional
* route-specific commission floor
* **venue / route / order-class / session-specific third-party fees**
* expected spread cost
* session validity
* instrument eligibility
* strategy churn bands

If fee classification is ambiguous for the configured route, admission must use the conservative fee path until validated by live broker charges.  
Execution logging must preserve the full path from strategy decision to broker result, including any cancel/replace chains and any overnight persistence that can cause order minimums to be charged again. 

### Task 11 — Build strategy factory + automatic rank-and-rebalance

Start with rule-based, ETF-native, slow ranking only.

Scheduled **evaluation** may run monthly, but live trading at the initial account size defaults to **threshold-driven rebalancing with quarterly review**.  
Monthly live rebalance is disabled at start unless empirical live reconciliation shows that the ticket sizes, routing profile, and all-in costs remain economical.

Threshold rebalance is allowed only if:

* signal delta clears churn bands
* minimum notional is met
* settled cash is sufficient under the active broker settlement policy
* fee / spread constraints are satisfied
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

* broker statement **and Flex-report** replay reconciles cash, positions, fees, settlement releases, and taxes with no unexplained residuals
* broker settlement-policy assumptions have been validated against observed broker reporting for the live instrument / venue set
* configured fee-classification assumptions, including any Xetra retail-vs-standard behavior, have been validated against observed live broker charges
* no simulated order is admitted that would fail live for settled-cash, reserved-cash, venue-phase, fee-classification, or compliance reasons
* year-end tax processing, including Vorabpauschale inputs and dated fund-tax-classification state, is reproducible from reference data
* statutory tax state and broker-posted tax cashflows reconcile without unexplained breaks
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
