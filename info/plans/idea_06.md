## German-tax-aware, settled-cash-aware, low-turnover UCITS ETF allocation and execution engine

## 1) Project charter

### Objective

Build a **Germany-retailable UCITS ETF allocation and execution engine** that is:

* correct on **cash-account trading admissibility**
* correct on **fees, settlement, accounting, and tax state**
* robust under **broker replay and reconciliation**
* economically sensible for **low-turnover ETF trading**
* extensible later into a **portfolio-reactive event-driven system**

### What v1 is and is not

**v1 is not** a broad “multi-venue, multi-session, multi-strategy platform.”
**v1 is** a narrowly scoped, production-quality proof that one live ETF trading profile can:

1. admit only orders that a live IBKR cash account can actually support,
2. reconcile exactly to broker records,
3. remain legally, and tax defensible in Germany,
4. and still retain a plausible path to economic viability.

### Primary success criteria

v1 succeeds only if **both** gates pass:

**Operational gate**

* no unexplained reconciliation residuals in positions, settled cash, fees, and broker-posted tax cashflows,
* no avoidable live rejects for settled-cash, unsupported session, fee misclassification, or compliance reasons,
* deterministic replay from strategy intent to broker result to journal state.

**Economic gate**

* every live trade is admitted only when its estimated net benefit exceeds a conservative all-in cost hurdle,
* the live profile remains viable under current broker commission minima and venue fees,
* scale-up occurs only when account size supports economically acceptable ticket sizes.

#### Context Note / Concise Reasoning

This second gate is mandatory because at current IBKR Germany public pricing, the fixed minimum commissions alone are large relative to a EUR 3,000 account. 
SmartRouting fixed has a EUR 3 minimum; generic direct routing has a EUR 4 minimum; SWB direct has a EUR 6 minimum; GETTEX direct has a EUR 3 minimum. 
That means, for example, that the broker minimum alone is about **20 bps on a EUR 1,500 ticket** and only falls to 
**10 bps at EUR 3,000 on a EUR 3 minimum** or **EUR 4,000 on a EUR 4 minimum**, before venue fees and spread are added. 

---

## 2) Hard scope decisions

### v1 Scope

* Germany-based taxable investor
* IBKR **cash account**
* low-turnover ETF allocation engine
* preference for **UCITS**, **PRIIPs/KID-compliant**, Germany-retailable, preferably accumulating ETFs
* one canonical accounting journal
* replay, reconciliation, and reference-data versioning as first-class concerns
* v1 alpha restricted to slow, ETF-native ranking
* event-to-ETF mapping and event-driven runtime deferred to v2. 

### Out of Scope for v1

* multi-venue live routing
* extended retail sessions
* auction-phase trading
* market orders
* intraday reactive trading
* holdings-propagated issuer-event alpha
* NLP/LLM-driven alpha
* automated handling of ambiguous tax and compliance edge cases without human sign-off
* live trading in more than one supported execution profile

---

## 3) External assumptions that the design must respect

### Broker and market micro-assumptions

IBKR’s public materials still imply that **cash-account buy orders must be supported by settled cash and sufficient 
cash to cover principal plus commissions and fees**, while its glossary pages are not perfectly internally consistent on settlement examples: 
one page says German stock sale proceeds become available after two days, while another still gives a generic stock example of T+3. 
Therefore settlement must be modeled as an effective-dated broker policy and validated against observed broker records, not hardcoded as a timeless constant. 


Xetra’s current official trading hours are **09:00–17:30 CET** for core trading, while the **Extended Xetra Retail Service** 
adds early trading from **08:00–08:55 CET** and late trading from **17:30 to 22:00 CET**. Since these are now distinct published profiles, 
v1 must not blur them in simulation or live routing. 


IBKR also states that **directed API stock orders cannot use Tiered pricing**, while **SmartRouted API orders can use either Tiered or Fixed**, 
and that **modified orders** or orders that **persist overnight** may be treated as new orders for minimum-commission purposes. 
This directly affects fee modeling and supports a v1 rule of avoiding modify-and-persist behavior in live trading. 


### German fund-tax assumptions

German fund-tax treatment remains first-order. Official law places **Vorabpauschale** in **§18 InvStG**, **Teilfreistellung** in **§20 InvStG**, 
and the deemed sale/deemed repurchase rule for a change in applicable Teilfreistellung in **§22 InvStG**. 
The **Basiszins** used in the Vorabpauschale calculation is published separately by the BMF and therefore 
must be external dated reference data, not a constant in code. 


### Retail eligibility assumptions

BaFin’s investment-funds database is useful but should remain a corroborating control source only; 
BaFin states that the database is public, may lag, and does not guarantee completeness or correctness. 
Also, the PRIIPs KID requirement applies to UCITS made available to EU retail investors from **1 January 2023**, so KID validity must be date-aware. 

### Broker reporting assumptions

IBKR states that **Activity Statement Flex Queries** update once daily at close of business, while **Trade Confirmation Flex Queries** 
update intraday but typically with a **5–10 minute delay**. That supports using TWS/IB Gateway as intraday execution truth and Flex as delayed reconciliation truth. 


---

## 4) Operating Philosophy

The project will follow five rules:

### Rule 1 — Unsupported cases are rejected, not approximated

If the system cannot prove the cash, fee, session, eligibility, or tax assumptions for a trade, the trade is not admitted.

### Rule 2 — v1 optimizes for correctness first, economics second, breadth last

The system must first prove exact broker reconciliation. It then proves that a low-turnover strategy can economically survive current small-account cost floors.  
Only after that may it widen the universe or execution scope.

### Rule 3 — Separate research scope from live scope

The research universe may be 5–10 ETFs.
The live universe will be much smaller and pre-approved.
The supported live execution profile will be smaller still.

### Rule 4 — Journal is sole economic truth

Only the append-only accounting journal determines portfolio economics.
Execution-state records explain *how* the economic state happened but do not compete with the journal.

### Rule 5 — Human review remains mandatory for ambiguous tax/compliance changes

The system may compute candidate tax and eligibility state, but v1 does not auto-bless ambiguous legal or tax transitions.


---

## 5) Concrete v1 live trading profile

### Supported v1 production execution profile

v1 live trading allows and supports exactly **one** production profile:

* **Broker:** Interactive Brokers cash account
* **Instrument type:** long-only UCITS ETFs
* **Currency:** EUR-funded account, EUR trading lines only
* **Venue:** **Xetra direct**
* **Session:** **continuous trading only**
* **Trading hours:** 09:00–17:30 CET only
* **Order type:** **DAY limit**
* **Routing / pricing plan:** fixed-fee profile consistent with directed routing
* **No use of:** extended retail session, auctions, MOC/MOO/IOC, overnight persistence, or order modification as a normal operating pattern


### Shadow execution profiles

The system may paper-trade or simulate other profiles, including SmartRouting fixed, but they are not production-admissible until separately validated with observed live broker charges and reconciliation.


---

## 6) Capital policy and economic viability policy

### Starting capital policy

The initial **EUR 3,000** account is a **micro-live proving account**, not production capital.

That means:

* it is sufficient to validate broker replay, cash reservation, settlement handling, fee booking, and tax-state plumbing,
* it is **not** sufficient to justify a diversified, frequent, multi-ticket ETF rotation strategy under current cost floors.

### Position-count policy

At starting capital, live trading defaults to:

* **maximum 1 funded ETF position**
* no live rotation unless the trade clears the economic hurdle
* cash may be held deliberately rather than forcing diversification

A second live position is allowed only when the intended per-order ticket size still meets the route-specific economic minimum.

### Economic admission rule

A rebalance or entry is admitted only if all of the following are true:

1. **expected benefit over the “do nothing” state is positive,**
2. **expected benefit exceeds estimated round-trip all-in cost plus model-risk buffer,**
3. the ticket exceeds the **minimum economic notional** for the active route,
4. turnover remains within the churn budget.

### Minimum economic notional

For each supported route, define:

**minimum economic notional**
= lowest order size for which estimated one-way all-in cost stays below the configured one-way cost budget.

The cost budget is a governance parameter, not a backtest convenience.
For v1 it is conservative and must include:

* commission minimum
* venue/exchange/clearing/regulatory fees
* spread estimate
* tax friction where applicable
* safety margin for fee misclassification

**Note:**    
Because the EUR 3 and EUR 4 minimum commissions dominate small tickets, the account will likely need more capital before a multi-position allocation strategy becomes economically credible. 
This is not a bug in the design; it is a real market constraint that the plan now explicitly accepts. ([Interactive Brokers][1])

---

## 7) Universe policy

### Research universe

The research universe may contain **5–10 ETFs**, but only if each ETF satisfies baseline structural requirements:

* UCITS
* not commodity-backed or commodity-derivative-backed, except where separately approved and lawful
* not single-stock ETF
* retail-marketable in Germany
* valid PRIIPs KID on the decision date
* tradable on a supported EUR line
* plain-vanilla structure preferred
* classification evidence sufficient for dated tax handling

### Live universe

The live universe is a strict subset of the research universe and begins with **2–4 pre-approved ISINs**, of which only **1–2** may be funded at a time depending on account size.

### Source hierarchy for eligibility

Eligibility is determined in this order:

1. issuer legal documents and KID/prospectus,
2. official exchange/instrument metadata,
3. broker contract metadata,
4. BaFin fund-database corroboration,
5. manually reviewed compliance decision record.

**Note:**    
BaFin is never the sole authority for marketability status because BaFin itself disclaims completeness and timeliness of the database.  
PRIIPs KID validity is date-sensitive because the UCITS exemption ended on 31 December 2022. 

---

## 8) Compliance policy

### Instrument compliance model

The compliance layer is **ISIN-centric** and pre-trade, not best-effort.

Each ISIN in the live universe must have a signed decision record with:

* ISIN
* fund legal name
* fund structure
* asset-class classification
* commodity exposure determination
* single-stock ETF determination
* employer-restriction pass/fail
* Germany retail-marketability evidence
* PRIIPs KID validity interval
* reviewer identity and review date
* evidence package hash and source list

### Compliance control rule

No new ISIN may go live without a manual compliance review entry.
If the evidence becomes stale or contradictory, the instrument is frozen from further buying until reviewed again.

---

## 9) Tax model and governance

### Tax design principle

The system must separately maintain:

* **statutory tax state**
* **broker-posted tax cashflows**

These are not the same object and must not be merged.

### Tax logic to support in v1

v1 supports:

* dated fund tax classification
* dated Teilfreistellung regime
* tax lot tracking
* broker-posted tax cashflows
* distributions
* year-end candidate Vorabpauschale state
* deemed sale/deemed repurchase handling when the applicable Teilfreistellung changes

### Tax governance correction

The engine may compute candidate year-end tax outcomes automatically, but production acceptance requires a **manual annual tax-close checklist** that verifies:

* Basiszins source for the relevant year
* fund classification evidence
* Teilfreistellung evidence
* any change in regime and resulting deemed transactions
* broker-posted tax entries vs statutory model
* adjustments required to align statutory and broker timing differences

---

## 10) Accounting, OMS, and source-of-truth model

### Economic source of truth

Maintain one **append-only canonical accounting journal**.
Everything economic is posted there:

* fills
* commissions
* exchange / clearing / regulatory fees
* tax cashflows
* cash movements
* FX
* distributions
* settlement releases
* broker corrections
* year-end tax adjustments

### Operational source of truth

Maintain a separate immutable **execution-event ledger** for:

* strategy intent
* pre-trade admission decision
* order submission
* broker acknowledgement
* partial fill
* cancel
* reject
* expiry
* correction / restatement

This ledger does not compete with the journal; it explains causal path.

### Truth hierarchy

The system uses this hierarchy:

1. **TWS / IB Gateway**: intraday execution-state truth
2. **Trade Confirmation Flex**: delayed execution confirmation
3. **Activity Flex / daily statements**: end-of-day economic reconciliation truth
4. **later broker corrections**: explicit replay exceptions that generate journal adjustments

---

## 11) Cash, settlement, and admission controls

### Admission controls

A buy order is admitted only if the system can prove, under the active broker policy, that there is enough:

* settled cash
* reserved cash headroom
* principal
* commission allowance
* venue/exchange/clearing/regulatory fee allowance
* tax/FX allowance where relevant
* spread allowance
* minimum economic notional

### Reserved cash policy

Reserved cash is an **operational control view**, not a competing ledger.
It is created when an order is admitted and released only on:

* terminal cancel/reject/expiry,
* full fill,
* explicit quantity reduction,
* or reconciliation-driven correction.

### Settlement policy

The system maintains an effective-dated settlement table keyed by supported live profile.
For v1, only the settlement policy for the active Xetra-direct ETF profile is production-supported.
Other profiles may exist in research tables but are not live-admissible until validated.

---

## 12) Fee model

### Fee design principle

Fees are modeled at the level of:

* route
* venue
* order class
* session profile
* effective date

### v1 fee policy

For the production live profile, the fee model must be fully pinned and testable.
If any fee classification is ambiguous, the simulator and admission engine use the **conservative higher-cost path**.

### Order-modification policy

Because IBKR states that modified or overnight-persisting orders may be treated as new orders for commission-minimum purposes, v1 live operations default to:

* no normal use of modify/replace,
* no overnight order persistence,
* expired DAY limits rather than carrying working orders into a new session.

---

## 13) Research, ranking, and rebalance policy

### v1 alpha policy

The alpha layer remains deliberately simple (but constructed in a way to support extensions):

* ETF-native features only
* slow horizon only
* no issuer-event propagation in core
* no NLP dependency
* no intraday reactivity

### Decision cadence (configurable but fixed for v1)

Research may evaluate monthly.
Live strategy review occurs quarterly by default.
Trades can occur between reviews only if a threshold breach is large enough to clear the cost hurdle.

### Trade decision benchmark

Each live trade is evaluated against two alternatives:

* **do nothing** and keep current holdings,
* **strategic reference basket** with no turnover.

### Default live strategy form

At startup, live trading is **threshold-first, low-turnover rebalancing**, not scheduled monthly rotation.

---

## 14) Data and reference-data model

### Reference data

The following are effective-dated reference data:

* supported live execution profile
* trading calendar
* settlement calendar
* broker settlement policy
* commission schedule
* third-party venue fees
* order class / session profile
* instrument eligibility state
* tax classification state
* Teilfreistellung regime
* Basiszins inputs
* corporate-action rules
* evidence registry

### Evidence registry

Every eligibility, compliance, and tax decision stores:

* source URI or document identity
* retrieval timestamp
* document hash
* parser/version identifier
* extracted fields
* stated validity interval
* reviewer status

### As-of versioning

Every backtest and every live decision binds to:

* market-data snapshot
* reference-data snapshot
* instrument-evidence snapshot
* session profile
* fee profile
* settlement policy profile

---

## 15) Simulation and backtest rules

### Simulator validity rule

A backtest is invalid if it admits any order that live v1 would reject under:

* settled cash
* reserved cash
* supported session
* supported order type
* fee classification
* compliance policy
* economic hurdle

### Backtest realism requirements

The simulator must use:

* the exact production fee model for the supported live profile
* the same minimum-economic-notional rule
* the same threshold/churn policy
* the same settlement logic
* the same session gating
* conservative spread assumptions
* dated fund metadata and tax state

### Mandatory stress tests

Before paper trading, run:

* fee stress
* spread stress
* settlement-delay stress
* stale-KID / stale-classification freeze tests
* broker-correction replay tests
* order-partial / reject / cancel path tests

---

## 16) Paper, micro-live, and scale path

### Phase A — build and offline validation

Deliver:

* reference-data store
* evidence registry
* immutable broker/event archive
* append-only journal
* derived reconciliation views
* simulator with supported live profile only

Exit only when historical and synthetic replay passes.

### Phase B — paper trading

Run the exact production profile in paper with:

* same admission logic
* same fee model
* same journal
* same reconciliation harness

Paper trading is not used to prove alpha.
It is used to prove **path consistency and failure handling**.

### Phase C — micro-live

Run micro-live with:

* maximum 1 funded ETF position
* no same-day rotation
* no extended-session trading
* no unsupported instruments
* no route changes
* no order modification except explicit manual exception

### Phase D — controlled scale-up

Scale only when both gates pass and the intended per-order ticket size is large enough that the selected route’s one-way all-in cost is acceptable.

In practice, this likely requires capital above the starting EUR 3,000 because the fixed minimum commissions are too large for diversified low-ticket rotation.  
Public IBKR pricing is explicit enough that this constraint should be treated as structural, not temporary. 

---

## 17) Acceptance gates

### Gate 1 — operational release

The system may trade micro-live only when:

* broker events, trade confirmations, and activity statements reconcile,
* no unexplained cash or fee residuals remain,
* the supported live profile is fully pinned,
* compliance and tax evidence records exist for every live ISIN,
* simulator and live engine share identical admission logic.

### Gate 2 — economic release

The system may increase capital or position count only when:

* each admitted live trade clears the economic hurdle,
* realized fee/slippage tracking matches the conservative model within governance tolerances,
* no route/session assumptions are being “forgiven” in reconciliation,
* average live ticket size is sufficient for the selected profile.

### Gate 3 — scope expansion

The system may add a second live route or session only after the first profile is fully validated and independently cost-tested.

---

## 18) v2 scaffold

v2 future direction from the original plan, but only after v1 gates pass. 

v2 may add:

* event-aware overlays
* portfolio-reactive state machines
* intraday risk/event handling
* wider ETF universe
* alternate execution profiles
* event-driven simulation
* NLP/LLM or holdings-propagated signals

But v2 may not weaken any of the following v1 disciplines:

* cash-account admissibility
* versioned reference data
* effective-dated tax state
* deterministic replay
* broker-first reconciliation
* explicit economic hurdle per trade

---

