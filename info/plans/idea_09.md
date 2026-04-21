# Germany-tax-aware, settled-cash-aware, single-route UCITS ETF execution, accounting, and allocation-research kernel

## 1) Project charter

### Objective

Build a **Germany-retailable UCITS ETF execution, accounting, and allocation-research kernel** that is:

* correct on **cash-account trading admissibility**
* correct on **fees, settlement, accounting, and tax state**
* robust under **broker replay and reconciliation**
* economically **falsifiable** for low-turnover ETF trading (not merely "sensible")
* extensible later into a **portfolio-reactive event-driven system**

### What v1 is and is not

**v1 is not** a broad "multi-venue, multi-session, multi-strategy platform," and **v1 is not** a scalable allocation engine.  
With 2–4 live ISINs and typically 1 funded position, v1 is a single-route execution, accounting, tax, and admission kernel with a small alpha pilot.

**v1 is** a narrowly scoped, production-quality proof that one live ETF trading profile can:

1. admit only orders that a live IBKR cash account can actually support,
2. reconcile exactly to broker records,
3. remain legally and tax defensible in Germany,
4. produce deterministically replayable economic records under broker corrections,
5. and still retain a plausible, falsifiable path to economic viability.

### Primary success criteria

v1 succeeds only if **all four** acceptance gates pass (see §17). In summary form:

**Operational correctness**  
no unexplained reconciliation residuals in positions, settled cash, fees, and broker-posted tax cashflows; 
no avoidable live rejects; deterministic replay from strategy intent to broker result to journal state in the **economic-state-invariance**  
sense defined in §10 (identical position, cash, fee, and tax state under canonical ordering by `broker_perm_id` and `exec_correction_group_id`), including under broker corrections.

**Cost-model closure**  
observed commissions, venue/clearing/regulatory fees, and slippage fit the conservative model within tolerance, 
across every fee dimension the production profile touches, based on **observed live exemplars**, not assumption.

**Alpha evidence**  
out-of-sample shadow decisions show a **pessimistic lower-confidence-bound excess benefit** over the do-nothing state that exceeds the full modeled cost hurdle.

**Capital sufficiency**  
the derived route-specific minimum AUM for the intended position count (see §6) is met.

#### Context note

At current IBKR Germany public pricing, fixed minimum commissions alone are large relative to a EUR 5,000 account:  
SmartRouting fixed has a EUR 3 minimum, generic direct routing a EUR 4 minimum, SWB direct a EUR 6 minimum, GETTEX direct a EUR 3 minimum.  
A EUR 3 minimum is about **20 bps on a EUR 1,500 ticket** and falls to **10 bps only at EUR 5,000**, before venue fees and spread. 
This drives the capital schedule in §6.

---

## 2) Hard scope decisions

### v1 Scope

* Germany-based taxable private investor
* IBKR **cash account**
* low-turnover ETF allocation **research** (live rotation strictly gated)
* preference for **UCITS**, **PRIIPs/KID-compliant**, Germany-retailable, preferably accumulating ETFs
* one canonical accounting journal
* replay, reconciliation, and reference-data versioning as first-class concerns
* v1 alpha restricted to slow, ETF-native ranking
* event-to-ETF mapping and event-driven runtime deferred to v2

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

### Broker and market micro-assumptions=

IBKR's public materials still imply that  
**cash-account buy orders must be supported by settled cash and sufficient cash to cover principal plus commissions and fees**,  
while its glossary pages are not perfectly internally consistent on settlement examples:  
one page says German stock sale proceeds become available after two days, while another still gives a generic stock example of T+3.  
Therefore settlement must be modeled as an effective-dated broker policy and validated against observed broker records, not hardcoded as a timeless constant.  
The EU cash-equities market is on a formal path to **T+1 on 11 October 2027**, so the settlement layer must be versioned by regime, not by constant (see §11).

Xetra's current official trading hours are **09:00–17:30 CET** for core trading,  
while the **Extended Xetra Retail Service** is a distinct retail-only regime adding early trading from 08:00–08:55 CET and late trading from 17:30 to 22:00 CET.  
Since these are distinct published profiles, v1 must not blur them in simulation or live routing.

IBKR states that **directed API stock orders cannot use Tiered pricing**, while **SmartRouted API orders can use either Tiered or Fixed**,  
and that **modified orders** or orders that **persist overnight** may be treated as new orders for minimum-commission purposes.  
This directly affects fee modeling and supports a v1 rule of avoiding modify-and-persist behavior in live trading.

Deutsche Börse publishes the **Xetra Liquidity Measure (XLM)** as a basis-point implicit-transaction-cost measure **at specified, standardized round-trip order sizes** 
(e.g., EUR 100,000), and exposes the open order book intended to support trading decisions. 
Because the XLM measurement size is an order of magnitude larger than v1 retail one-way tickets and uses a round-trip reference, 
XLM is a **venue-level / ISIN-level** liquidity indicator, **not** a per-ticket cost model at retail scale (see §13).  
Retail-scale per-order admission relies on quoted spread and observed realized slippage;  
XLM enters the system only as (i) an ISIN-level universe-admission check and (ii) a monitoring signal feeding the conservative slippage reserve.

### German fund-tax assumptions

German fund-tax treatment remains first-order. Official law places **Vorabpauschale** in **§18 InvStG**, **Teilfreistellung** in **§20 InvStG**,  
and the deemed sale/deemed repurchase rule for a change in applicable Teilfreistellung in **§22 InvStG**.  
The **Basiszins** used in the Vorabpauschale calculation is published separately by the BMF and therefore must be external dated reference data, not a constant in code.

### Retail eligibility assumptions

BaFin's investment-funds database is useful but should remain a corroborating control source only; BaFin states that the database is public, may lag,  
and does not guarantee completeness or correctness. The PRIIPs KID requirement applies to UCITS made available to EU retail investors from **1 January 2023**,  
so KID validity must be date-aware.

### Broker reporting assumptions

IBKR states that **Activity Statement Flex Queries** update once daily at close of business, while **Trade Confirmation Flex Queries**  
update intraday but typically with a **5–10 minute delay**. TWS/IB Gateway is therefore intraday execution truth and Flex is delayed reconciliation truth.  
IBKR API documentation further states that execution corrections arrive as additional `execDetails` callbacks with the same parameters except  
for the `execId`, and that `permId` is the account-wide unique identifier bound to API order IDs. This drives the canonical identity model in §10.

---

## 4) Operating Philosophy

The project will follow five rules:

### Rule 1 — Unsupported cases are rejected, not approximated

If the system cannot prove the cash, fee, session, eligibility, or tax assumptions for a trade, the trade is not admitted.

### Rule 2 — v1 optimizes for correctness first, economics second, breadth last

The system must first prove exact broker reconciliation. It then proves that a low-turnover strategy can economically survive current small-account cost floors.  
Only after that may it widen the universe or execution scope.

### Rule 3 — Separate research scope from live scope

The research universe may be 5–10 ETFs. The live universe will be much smaller and pre-approved. The supported live execution profile will be smaller still.

### Rule 4 — Journal is sole economic truth

Only the append-only accounting journal determines portfolio economics. Execution-state records explain *how* the economic state happened but do not compete with the journal.

### Rule 5 — Human review remains mandatory for ambiguous tax/compliance changes

The system may compute candidate tax and eligibility state, but v1 does not auto-bless ambiguous legal or tax transitions.

---

## 5) Concrete v1 live trading profile

### Supported v1 production execution profile

v1 live trading allows and supports exactly **one** production profile:

* **Broker:** Interactive Brokers cash account
* **Instrument type:** long-only UCITS ETFs
* **Currency:** EUR-funded account, EUR trading lines only
* **Venue / route:** **one production route, empirically selected at Gate 2.** 
  * The route is **not** fixed by fiat at v1 start. Initial candidates are **Xetra directed** and **SmartRouting Fixed**. 
  * Until Gate 2 clears, plumbing and exemplar collection run on **Xetra directed** as the working default, while SmartRouting Fixed is run in parallel as a mandatory shadow candidate under §12. 
  * Production status attaches to whichever route empirically minimizes realized all-in cost at operational ticket sizes (§6, §12). 
  * Public IBKR pricing alone does not settle the choice: 
    * SmartRouting Fixed has a lower stated minimum (EUR 3) than generic directed routing (EUR 4), but routing behaviour, venue-fee pass-through, and realized slippage are only knowable from observed live exemplars.
  * **Candidate-route asymmetry.** The two candidates are **not symmetric objects**. 
    * SmartRouting Fixed is a **routing policy** applied under IBKR's Best Execution framework that may select venue(s) dynamically; 
      * Xetra directed is an **explicit destination** that pins the venue but not the handling path. 
      * As a consequence, when both candidates execute on Xetra, the fee treatment on the executed venue can still differ 
      * (retail-vs-direct Xetra fee handling depends on whether the order was routed under Best Execution Policy or as an explicitly directed order). 
      * Gate 2 therefore compares **end-to-end realized all-in cost**, not routing rules in isolation, 
      * and route selection output records the observed fee-path classification for each candidate, not just its realized commission.
* **Session:** **continuous trading only**
* **Trading hours:** 09:00–17:30 CET only
* **Order type:** **DAY limit** (limit-price construction per §13)
* **Routing / pricing plan:** Tiered pricing is unavailable for directed API orders, so the directed candidate uses **Fixed**; SmartRouting may use Fixed.  
  * The chosen plan is entailed by the selected route.
* **No use of:** extended retail session, auctions, MOC/MOO/IOC, overnight persistence, or order modification as a normal operating pattern

### Shadow execution profiles

Routes not selected as the production route at Gate 2 remain runnable in paper or simulation but are not production-admissible.  
A shadow route can be promoted to production only after it independently clears **Gate 1 and Gate 2** under §17.

---

## 6) Capital policy, economic admission, and route-specific capital schedule

### Starting capital policy

The initial **EUR 5,000** account is a **micro-live proving account**, not production capital.  
It is sufficient to validate broker replay, cash reservation, settlement handling, fee booking, and tax-state plumbing.  
It is **not** sufficient to justify a diversified, frequent, multi-ticket ETF rotation strategy under current cost floors.

### Minimum economic notional (per-ticket constraint)

For each supported route `r` and effective date `t`, define the **minimum economic notional** - the smallest order size that keeps estimated one-way all-in cost under the configured one-way cost budget `b_1w`:

```
N_min(r, t) = [ F_min(r, t) + V_min(r, t) + R(r, t) ] / b_1w
```

where

* `F_min(r, t)` = broker minimum commission for route `r`,
* `V_min(r, t)` = minimum venue / clearing / regulatory fee burden,
* `R(r, t)` = reserve for spread, fee misclassification, tax friction, and FX,
* `b_1w` = allowed one-way cost budget as a fraction (e.g., 10 bps = 0.001).

`b_1w` is a governance parameter, not a backtest convenience. It is conservative and pinned per route.

### Minimum AUM schedule (per-account constraint)

For a target number of simultaneously funded positions `k`, the minimum account size under route `r` at date `t` is:

```
AUM_min(k, r, t) = k · N_min(r, t) + cash_buffer(t)
```

where `cash_buffer(t)` covers tax postings, FX slippage, and a fee-misclassification reserve.

### Tiered capital-state policy

The following tiers gate live behaviour:

* **Tier 0 - Proving account.** Default at starting capital. Maximum 1 funded position. No discretionary rotation. Trades only to validate plumbing.
* **Tier 1 - One-position economic trading.** Allowed only once `AUM_min(1, r, t)` is met *and* live fee/slippage tracking is within tolerance for the route.
* **Tier 2 - Two funded positions.** Allowed only once `AUM_min(2, r, t)` is met, Tier 1 has accumulated sufficient observed-cost evidence, and the alpha gate (below) clears for rotation.

Transitions between tiers require explicit governance approval; they do not auto-activate on AUM changes.

### Economic admission rule — LCB formulation

A rebalance or entry is admitted only if **all** of the following hold:

1. The **lower confidence bound** of expected excess benefit versus the do-nothing state exceeds the full modeled cost hurdle:

   ```
   LCB[ Δα_vs_do_nothing ] > Ĉ_RT + B_model + B_ops
   ```

   where

   * `LCB[...]` is a pessimistic forecast of excess benefit (point estimate minus a calibration-derived margin),
   * `Ĉ_RT` is expected round-trip all-in cost (commission + venue/clearing/regulatory fees + spread + tax friction + FX),
   * `B_model` is a model-risk buffer,
   * `B_ops` is an operational buffer covering fee-misclassification and residual reconciliation risk.

2. The ticket size meets or exceeds `N_min(r, t)` for the active route.
3. The account's current tier permits the proposed position count.
4. Turnover over the churn-budget window remains within its cap.

### Governance parameters and pinning

`N_min`, `AUM_min`, and the LCB admission rule are **formally incomplete** until their governance inputs are pinned.  
The following are **governance parameters**, not free engineering constants, and each requires a documented,  
versioned decision under the evidence registry (§14) **before Gate 2 can be evaluated**:

* `b_1w` — one-way cost budget (fraction of notional).
* `R(r, t)` — the spread / fee-misclassification / tax-friction / FX reserve, with its composition itemised per route.
* `cash_buffer(t)` — composition and sizing, including the fee-misclassification reserve portion.
* `B_model` and `B_ops` — buffer magnitudes in the LCB rule.
* LCB **confidence-margin calibration** — the procedure and sample requirements that convert a point estimate of excess benefit into its pessimistic lower bound 
  * (sampling distribution assumption, calibration window, and the calibration-refresh cadence).
* **Churn-budget window and cap** — explicit numeric bounds.

Until each of these is pinned, the engine operates in **exploratory mode** under documented conservative placeholders: `b_1w`, `R`, 
and the buffers are set to the upper end of plausible values, and no Gate-2 cost-closure claim and no Gate-3 alpha claim may be asserted using exploratory placeholders. 
The transition from exploratory to pinned values is itself a governance event and is journalled under the evidence registry.

---

## 7) Universe policy

### Research universe

The research universe may contain **5-10 ETFs**, but only if each ETF satisfies baseline structural requirements:

* UCITS
* not commodity-backed or commodity-derivative-backed, except where separately approved and lawful
* not single-stock ETF
* retail-marketable in Germany
* valid PRIIPs KID on the decision date
* tradable on a supported EUR line
* plain-vanilla structure preferred
* classification evidence sufficient for dated tax handling

### Live universe

The live universe is a strict subset of the research universe and begins with **2-4 pre-approved ISINs**,  
of which only **1-2** may be funded at a time depending on the active tier (§6).

Each live ISIN must additionally satisfy the **XLM universe-admission check**  
(§13): XLM at the nearest standardized XLM size must classify the ISIN as sufficiently liquid under a documented, pinned ISIN-level threshold.  
Sustained deterioration of an ISIN's XLM is a freeze condition for further buying of that ISIN, pending review.  
This is the only role in which XLM binds; per-order admission remains ticket-size-native (§13).

### Source hierarchy for eligibility

Eligibility is determined in this order:

1. issuer legal documents and KID/prospectus,
2. official exchange/instrument metadata,
3. broker contract metadata,
4. BaFin fund-database corroboration,
5. manually reviewed compliance decision record.

**Note:** BaFin is never the sole authority for marketability status because BaFin itself disclaims completeness and timeliness of the database.  
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

## 9) Tax model, ledgers, and governance

### Tax design principle

Statutory tax state and broker-posted tax cashflows are separate objects and must not be merged.  
v1 maintains them as **two explicit ledgers** plus a reconciliation artifact.

### v1 operational minimality

The **structural** two-ledger model is maintained from day one to avoid costly retrofit when the universe expands. 
But **v1 operation** of the tax close is deliberately minimal and matched to the frozen live universe (2–4 pre-approved ISINs, 1 funded position typical):

* statutory state updates occur **on an event cadence** (buy, sell, distribution, regime change, annual close), not as a running engine,
* Vorabpauschale and annual Basiszins inputs are entered and versioned under the evidence registry (§14) as part of the **manual annual tax-close checklist**, not computed by an always-on service,
* deemed-sale / deemed-repurchase bookings under §22 InvStG are treated as **explicit, manually-approved events** in v1 — the engine computes candidate entries; a reviewer signs them off,
* full automation of regime-change detection, Teilfreistellung reclassification, and distribution-tax derivation across a broader universe is **deferred to scope expansion**.

This keeps v1 tax surface area bounded to what the live universe can actually produce, while preserving the ledger shape so that wider automation is additive later.

### A. Statutory tax state ledger

Maintains (effective-dated):

* dated fund classification
* dated Teilfreistellung regime
* candidate Vorabpauschale state (including Basiszins input used)
* deemed sale / deemed repurchase state on Teilfreistellung regime change (§22 InvStG)
* tax lots (acquisition cost, acquisition date, quantity, lot-level accumulated notional gains/losses)
* distributions and their statutory tax treatment

### B. Account tax-cash ledger

Maintains (as-posted by broker):

* withholding postings
* tax-adjustment postings
* distribution-tax postings
* annual-close adjustments
* timing-difference bridge entries linking statutory state to posted cash

### Annual tax reconciliation

Each fiscal year produces a reconciliation artifact enforcing:

```
statutory_tax_delta(year) = Σ broker_posted_tax_cashflows(year)
                          + Σ explicit_bridge_adjustments(year)
```

Unexplained residuals block year-end close. The reconciliation is reviewed under the manual annual tax-close checklist, which verifies:

* Basiszins source for the relevant year
* fund classification evidence
* Teilfreistellung evidence
* any change in regime and resulting deemed transactions
* broker-posted tax entries vs statutory model
* bridge adjustments required to align statutory and broker timing differences

### Tax governance rule

The engine may compute candidate year-end tax outcomes automatically; production acceptance requires the manual annual tax-close checklist to sign off.

---

## 10) Accounting, OMS, and source-of-truth model

### Economic source of truth

Maintain one **append-only canonical accounting journal**. Everything economic is posted there:

* fills
* commissions
* exchange / clearing / regulatory fees
* tax cashflows (both ledgers in §9 post to the journal under distinct account codes)
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

### Canonical event identity and correction model

Every event carries a canonical identity drawn from the following ID namespace:

* `strategy_intent_id` — unique per strategy decision
* `admission_decision_id` — unique per admission evaluation (links to the LCB inputs used)
* `api_order_id` — the local API order identifier
* `broker_perm_id` — IBKR's account-wide unique order identifier; **canonical order identity**
* `parent_perm_id` — parent of child/replacement orders where applicable
* `exec_id` — broker execution identifier
* `exec_correction_group_id` — identity grouping an original execution and its corrections
* `commission_report_id` — broker commission-report identity
* `statement_posting_id` — identity on the daily activity statement

Rules:

1. `broker_perm_id` is the canonical order identity; all downstream records reference it.
2. Execution corrections do **not** mutate prior events. They create new events that share an `exec_correction_group_id` with the original and **supersede** prior events within that group.
3. Journal postings always reference the **corrected** execution set, never raw callback order.
4. **Deterministic replay** is defined as **economic-state invariance**:  
   given the archived broker event stream and reference-data snapshots, replay reproduces identical position, settled-cash, reserved-cash, fee, 
   and tax state under a **canonical ordering keyed by `broker_perm_id` and `exec_correction_group_id`**, with corrections applied as supersessions within their group. 
   Literal byte-identity of journal records is **not** required — it would be brittle under API-order-ID remapping, rebinding (which IBKR documents as capable of cancelling 
   and resubmitting a working order with fresh queue priority), and callback-ordering differences that carry no economic content. 
   Any divergence in the set of economic-state invariants is a replay failure and must be investigated.

### Truth hierarchy

1. **TWS / IB Gateway**: intraday execution-state truth.
2. **Trade Confirmation Flex**: delayed execution confirmation.
3. **Activity Flex / daily statements**: end-of-day economic reconciliation truth.
4. **Later broker corrections**: explicit replay exceptions that generate journal adjustments via the correction-group mechanism above.

---

## 11) Cash, settlement, and admission controls

### Admission controls

A buy order is admitted only if the system can prove, under the active broker policy and active settlement regime, that there is enough:

* settled cash
* reserved cash headroom
* principal
* commission allowance
* venue/exchange/clearing/regulatory fee allowance
* tax/FX allowance where relevant
* spread allowance
* minimum economic notional (§6)

### Live admission authority

The **authoritative** cash input for live admission is **broker-reported settled cash and buying power**  
(read from TWS/IB Gateway, confirmed by Trade Confirmation Flex, reconciled by Activity Flex).  
The **local settlement regime engine is not the live authority**; it exists to:

* **forecast** settled cash after pending fills and expected settlement dates,
* **reconcile** broker-posted settlement against engine projection,
* **test** regime transitions, including the EU **T+1 cutover on 11 October 2027** and in-flight unsettled trades straddling it.

If broker-reported settled cash and the engine projection disagree by more than the governance tolerance, the divergence is a **reconciliation break**.  
It freezes further admission on the affected account until investigated. Under no circumstance does the engine override the broker when the broker says cash is insufficient,  
and under no circumstance does the engine admit a trade the broker reports as unsupported by settled cash.

### Cash-state decomposition

At any instant, account cash is decomposed into:

* **trade-date cash** — economic position as of trade booking,
* **settled cash** — available for new purchases under the active settlement regime,
* **reserved cash** — earmarked by admitted but not-yet-terminal orders,
* **withdrawable cash** — available for transfer out (subject to broker policy).

Admission gates buy against settled and reserved; withdrawal gates against withdrawable. Mixing these categories is forbidden.

### Reserved cash policy

Reserved cash is an **operational control view**, not a competing ledger. It is created when an order is admitted and released only on:

* terminal cancel/reject/expiry,
* full fill,
* explicit quantity reduction,
* or reconciliation-driven correction.

### Settlement regime engine

Settlement is modelled as a **regime engine**, not a static table. Each posting binds to a `settlement_regime_id` with:

* regime effective-date range (open, close)
* jurisdiction and venue scope
* instrument-class scope
* instrument/venue overrides (exception list)
* day-count rule (T+N in business days under the applicable calendar)
* cash-availability rule for sale proceeds

v1 ships with the current Xetra ETF regime applicable to the production profile. 
The engine must carry **forward-dated** regime entries so that the EU migration to **T+1 on 11 October 2027** is representable today, with:

* a regime-transition effective date,
* transition test cases (orders straddling the cutover),
* explicit behaviour for in-flight unsettled trades at regime change.

Other profiles may exist in research tables but are not live-admissible until validated.

---

## 12) Fee model

### Fee design principle

Fees are modelled at the level of:

* route
* venue
* order class
* session profile
* retail-vs-direct handling path
* order lifecycle state (new, modified, overnight-carried)
* effective date

For the production live profile, the fee model must be fully pinned and testable.  
If any fee classification is ambiguous, the simulator and admission engine use the **conservative higher-cost path**.

### Fee-closure gate (pre-production)

**No profile becomes production-supported until every fee component *that the production profile actually touches*  
has at least one observed live exemplar and the model reproduces posted broker charges within governance tolerance.**  
The required dimensions for exemplar coverage are bounded by the §5 production profile and are:

* routing mode of the candidate route (directed vs SmartRouted) — **required for each route still under Gate 2 evaluation** (see "Production-route selection" below),
* session regime: **continuous trading only** (extended retail, auctions are excluded from the production fee matrix because §5 excludes them),
* instrument class (UCITS ETF),
* order lifecycle state: **new orders only** — modified, replaced, and 
  * overnight-persisting orders are **explicitly out of scope** for the production fee matrix because §5 and Rule 1 exclude them from normal live operation; 
  * no fee-closure exemplar for these states is required to pass Gate 2, and none is collected in production,
* retail-vs-direct handling path as actually configured on the live account,
* Xetra-specific fee treatments where applicable.

Retail Xetra fee waivers are **not** assumed by model. They are only applied when live broker postings demonstrate the account is actually on that fee path.

### Production-route selection (Gate 2 output)

Gate 2 compares **realized all-in cost at operational ticket sizes** across candidate routes (Xetra directed, SmartRouting Fixed).  
Production route status is awarded to whichever route produces the lower realized all-in cost over an exemplar set covering the ticket-size distribution the live profile actually generates.  
Public broker pricing is an input but not a conclusion — a EUR 1 difference in stated minimum commission is not de minimis at retail scale (EUR 1 is ≈ 7 bps on a EUR 1,500 ticket, ≈ 3 bps on a EUR 5,000 ticket), 
so the selection must be driven by observed postings, not schedules.

**Statistical power and tie-break.** At retail ticket sizes, fixed commission minima dominate and realized slippage is noisy, so a small number of live exemplars cannot robustly separate the candidate routes.  
The selection procedure is therefore constrained as follows:

* **Stratification.** Exemplars must cover the live ticket-size distribution by bucket (at minimum: near `N_min(r, t)`, mid, and upper bound of the expected distribution).
* **Minimum exemplar count.** A per-bucket minimum count is pinned as a governance parameter under §6; no route comparison below this count produces a Gate-2 verdict.
* **Separation criterion.** Production status is awarded only when the per-ticket realized all-in cost difference between candidates is **larger than the exemplar-based noise band** 
  * at a pre-specified confidence level (i.e., the candidates separate under a paired-observation test that is itself specified in governance, not chosen after the data).
* **Conservative tie-break.** When the candidates do **not** cleanly separate, the route with the **more deterministic and more fully documented fee-path classification** (in the §12 dimensions above) is preferred. 
  * Uncertainty about routing behaviour, venue-fee pass-through, or retail-vs-direct handling is a reason to prefer the simpler-to-model candidate, not a reason to rely on a marginal cost advantage.
* **No schedule-only verdict.** A Gate-2 verdict may not be reached from stated-schedule fee differences in the absence of live exemplars meeting the minimum-count requirement.

### Order-modification policy

Because IBKR states that modified or overnight-persisting orders may be treated as new orders for commission-minimum purposes,  
and because IBKR documents that rebinding can cancel and resubmit a working order with fresh queue priority, v1 live operations default to:

* no normal use of modify/replace,
* no overnight order persistence,
* expired DAY limits rather than carrying working orders into a new session.

Exemplars for the excluded lifecycle states may be collected as **research-only artifacts** in shadow environments;  
they do not feed the production fee matrix and their absence does not block Gate 2.

---

## 13) Research, ranking, rebalance, and deterministic execution policy

### v1 alpha policy

The alpha layer remains deliberately simple, but constructed to support extensions:

* ETF-native features only
* slow horizon only
* no issuer-event propagation in core
* no NLP dependency
* no intraday reactivity

### Decision cadence (configurable but fixed for v1)

Research may evaluate monthly. Live strategy review occurs quarterly by default.  
Trades can occur between reviews only if a threshold breach is large enough to clear the LCB admission hurdle (§6).

### Trade decision benchmark

Each live trade is evaluated against two alternatives:

* **do nothing** and keep current holdings,
* **strategic reference basket** with no turnover.

### Default live strategy form

At startup, live trading is **threshold-first, low-turnover rebalancing**, not scheduled monthly rotation.

### Pre-trade liquidity gate

Deutsche Börse publishes the **Xetra Liquidity Measure (XLM)** as a basis-point implicit-cost measure for **specified, standardized order sizes** (round-trip, at sizes such as EUR 100,000).  
That standardization makes XLM **useful as a venue-level liquidity indicator** but **ill-posed as a per-order reject gate at retail ticket sizes** (EUR 1,500–5,000, one-way):  
the XLM measurement size is an order of magnitude larger than the live ticket, and the XLM round-trip reference has no direct logical mapping to a retail one-way fill.  
At retail ticket scale, quoted spread, odd-lot fill behaviour, and realized fill slippage on observed live exemplars dominate the actual cost, and XLM is used only in roles where that size mismatch does not break the logic.

Accordingly, XLM is used at two places in the system, and **not** as a per-order reject:

1. **Universe-admission check (ISIN-level).** An ISIN is admissible to the live universe (§7) only if its XLM at the nearest standardized XLM size classifies it as sufficiently liquid under a documented ISIN-level threshold.  
   This is a **slow, standing, universe-level** check, not a per-ticket decision. A sustained deterioration in an ISIN's XLM is a freeze condition for further buying of that ISIN, pending review.
2. **Monitoring signal feeding the conservative reserve.** Until per-ISIN live exemplars exist to anchor expected realized slippage at operational ticket sizes,  
   the simulator and admission engine feed XLM into the **conservative spread/slippage reserve** (`R` in §6) as an upper-bound signal — i.e. worse XLM tightens the reserve, never loosens it. 
   XLM never directly clears or rejects an individual ticket.

The per-order liquidity gate is therefore **ticket-size-native only**. Every live order must pass *all* of:

* **quoted spread** at decision time below an ISIN-specific cap calibrated to the order's notional,
* **expected realized slippage**, anchored to observed live fills in the same production profile at comparable ticket sizes, within the one-way cost budget,
* **liquidity window** eligibility — not the first or last minutes of continuous trading unless empirical fill quality justifies it.

Until live exemplars exist to anchor expected realized slippage, the simulator and admission engine use a **conservative** spread assumption drawn from venue order-book snapshots,  
with XLM contributing only through the reserve channel described above — never as a standalone estimate of retail-ticket cost.

### Limit-price construction (deterministic)

Limit prices are constructed deterministically, not discretionarily:

* anchor to live top-of-book at the decision timestamp,
* cap aggression using a **quoted-spread-derived budget** at the operational ticket size; XLM does **not** enter limit-price construction directly 
  * (its influence is absorbed into the conservative reserve `R` per the monitoring-signal role above),
* use exactly one documented cancel/re-submit policy (single-shot, time-boxed; re-submission counts as a new order for fee accounting — see re-submit budget below),
* discretionary manual repricing is forbidden except under an explicit emergency-override with a signed exception record.

This makes simulator execution and live execution comparable by construction.

### Cancel/re-submit budget (fee-trap control)

The "no modify, single-shot cancel/re-submit" policy avoids the modify/overnight fee trap but exposes a different one:  
because IBKR documents that modified or overnight-persisting orders may be treated as new orders for commission-minimum purposes,  
and because a cancel-and-re-submit is likewise treated as a new order, **each re-submit incurs a fresh minimum-commission charge**.  
At a EUR 3 minimum, two re-submits on a EUR 1,500 ticket turn a 20 bps floor into a 60 bps floor.

To contain this without reintroducing modify/replace, v1 imposes:

* a **per-decision re-submit cap** (pinned as a governance parameter under §6),
* a **decision-level fee pre-check** that includes the worst-case commission charge under the re-submit cap when evaluating `N_min` and the LCB rule,
* **mandatory recording** of every re-submit on the execution-event ledger (§10), with its own `strategy_intent_id` linkage so that decision-level realized cost is reconstructable,
* a **cumulative re-submit budget per rolling window**, audited at reconciliation; exceeding the budget freezes further live admission pending review.

No-fill is treated as a legitimate outcome of the DAY limit: an expired unfilled limit is the cheaper failure mode relative to a modified/overnight order or an over-aggressive resubmit loop.

---

## 14) Data and reference-data model

### Reference data

The following are effective-dated reference data:

* supported live execution profile
* trading calendar
* settlement regime entries (§11)
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
* settlement regime id
* canonical identity namespace version

---

## 15) Simulation and backtest rules

### Simulator modes

A fixed set of live v1 constraints — production route, fee path, pinned fee model, pinned `b_1w` / `R` / buffers, and the LCB calibration — is **not knowable before Gate 2 has closed**. A single monolithic simulator that requires all of these to be pinned would either invalidate all research until Gate 2 closes, or silently use drifting placeholders that get retroactively overturned by later route/fee decisions. Neither is acceptable.

The system therefore runs the simulator in **two explicit modes**, selected per run and recorded in the run metadata:

**Mode A — Exploratory simulator (pre-Gate-2).**  
Used for research, ranking experiments, liquidity and threshold sensitivity studies before route and fee path are pinned. Mode A:

* uses **conservative placeholder** values for `b_1w`, `R`, `cash_buffer`, `B_model`, and `B_ops` (upper end of plausible range, per §6 "Governance parameters and pinning"),
* uses a conservative blended fee model that **over-estimates** realized commission and venue-fee burden across candidate routes,
* applies the same session gating, order-type restrictions, settlement regime engine, compliance policy, and ticket-size-native liquidity gate as live v1,
* is tagged `simulator_mode = "exploratory"` on every output.

Mode A output is **not admissible evidence** for Gate 2 or Gate 3. Its role is to de-risk research choices so that they are not invalidated when Gate 2 closes.

**Mode B — Production simulator (post-Gate-2).**  
Used once Gate 2 has awarded production route status and the fee model, `b_1w`, `R`, buffers, and LCB calibration are pinned. Mode B:

* binds to the **pinned** production route, fee path, and fee model,
* uses **pinned** governance parameters from §6,
* applies the **exact** production admission logic, including the re-submit budget from §13,
* is tagged `simulator_mode = "production"` on every output.

**Only Mode B runs may feed Gate 3 alpha evidence.** A Mode A run referenced in Gate 3 arguments is treated as a governance violation.

### Simulator validity rule

A backtest in either mode is invalid if it admits any order that live v1 would reject under:

* settled cash
* reserved cash
* supported session
* supported order type
* fee classification (the applicable production model in Mode B; the conservative blended model in Mode A)
* compliance policy
* LCB economic hurdle (pinned calibration in Mode B; conservative placeholder in Mode A)
* ticket-size-native liquidity gate (spread cap, window, observed-slippage reserve; XLM enters only through universe admission and the conservative reserve per §13, not as a per-ticket budget)

Mode-crossing is also invalid: a single simulator run may not mix exploratory and production parameters.

### Backtest realism requirements

The Mode B simulator must use:

* the exact production fee model for the supported live profile (post fee-closure)
* the same `N_min` and tier rules
* the same LCB admission formulation with pinned calibration
* the same threshold/churn policy
* the same settlement regime engine
* the same session gating and ticket-size-native liquidity gate
* conservative spread assumptions when live exemplars are missing
* dated fund metadata and tax state
* the pinned re-submit budget and its fee consequences

The Mode A simulator must use the same list, but with the exploratory placeholders and the conservative blended fee model in place of their pinned counterparts.

### Mandatory stress tests

Before paper trading, run (in both modes where meaningful):

* fee stress
* spread stress
* settlement-delay stress
* settlement-regime-transition stress (including the 2027 T+1 cutover scenario)
* stale-KID / stale-classification freeze tests
* broker-correction replay tests (verifying `exec_correction_group_id` semantics)
* order-partial / reject / cancel path tests
* re-submit-budget exhaustion tests

---

## 16) Paper, micro-live, and scale path

### Phase 0 — Economic feasibility pre-screen

Before any of the Phase-A build is industrialised, v1 runs a cheap, deterministic **economic-feasibility pre-screen** using only known broker fixed costs and the published venue fee schedule.  
Its purpose is to avoid building a full production-control stack around a strategy whose economics cannot clear the fixed-cost floor at the available capital.

The pre-screen computes, on current IBKR Germany public pricing:

* the **implied one-way floor** in bps at each candidate operational ticket size (e.g., EUR 1,500 / EUR 3,000 / EUR 5,000), from the minimum commission alone, for each candidate route,
* the **implied round-trip floor** including venue/clearing/regulatory fees under conservative assumptions,
* the **theoretical minimum LCB excess benefit** any v1 alpha must deliver to clear `Ĉ_RT + B_model + B_ops` under exploratory placeholders.

Pre-screen pass criterion: there exists **at least one plausible** ETF-native alpha specification (documented in evidence, not asserted)  
whose backed-by-historical-research expected excess benefit plausibly exceeds the implied round-trip floor at the intended capital and ticket-size distribution, by a margin that could survive the LCB confidence margin.

Pre-screen fail criterion: the implied floor dominates any plausible ETF-native excess benefit at the available capital.  
On fail, v1 is **re-scoped before further build** — by raising capital, narrowing to a single buy-and-hold instrument with no rotation, or closing v1 without an execution engine.

The pre-screen is not a gate — it is a stop-loss on project effort. Its output is journalled under the evidence registry and referenced by Gate 3's cost-stack argument.

### Phase A — build and offline validation

Deliver:

* reference-data store
* evidence registry
* immutable broker/event archive
* append-only journal
* derived reconciliation views
* simulator with supported live profile, runnable in **Mode A (exploratory)** from Phase A start; **Mode B (production)** activated post-Gate-2

Exit only when historical and synthetic replay passes, including correction-group replay.

### Phase B — paper trading

Run the exact production profile in paper with:

* same admission logic (including LCB and liquidity gate)
* same fee model
* same journal
* same reconciliation harness

Paper trading is not used to prove alpha. It is used to prove **path consistency and failure handling**.

### Phase C — micro-live (Tier 0)

Run micro-live with:

* maximum 1 funded ETF position
* no same-day rotation
* no extended-session trading
* no unsupported instruments
* no route changes
* no order modification except explicit manual exception
* fee-closure collection under way

### Phase D — controlled scale-up

Advance to Tier 1, and then Tier 2, only when the relevant acceptance gates (§17) pass.  
In practice, advancing past Tier 0 likely requires capital above EUR 5,000 because the fixed minimum commissions are too large for diversified low-ticket rotation.  
Public IBKR pricing is explicit enough that this constraint is treated as structural.

---

## 17) Acceptance gates

The project uses **four** acceptance gates. A gate cannot be claimed passed while any later gate materially depends on behaviour the earlier gate has not yet demonstrated.

### Gate 1 — Operational correctness

The system may trade micro-live (Tier 0) only when:

* Phase 0 economic-feasibility pre-screen (§16) has passed or been explicitly waived under a signed exception,
* broker events, trade confirmations, and activity statements reconcile,
* no unexplained cash or fee residuals remain,
* the supported live profile is fully pinned,
* compliance and tax evidence records exist for every live ISIN,
* the **Mode B production simulator** (§15) and the live admission engine share identical admission logic; divergence between Mode A (exploratory) and live is expected by design and is not a Gate-1 concern,
* deterministic replay (including broker corrections under `exec_correction_group_id`) is demonstrated.

### Gate 2 — Cost-model closure

The profile is production-supported only when:

* the **§6 governance parameters** (`b_1w`, `R`, `cash_buffer`, `B_model`, `B_ops`, LCB confidence-margin calibration, churn-budget window and cap,   
  * per-decision re-submit cap, minimum per-bucket exemplar count) are **pinned** under documented governance decision — a Gate-2 verdict against exploratory placeholders is not admissible,
* the **production route has been empirically selected** from Gate-2 candidates on the basis of **realized all-in cost at operational ticket sizes** (not on listed schedules alone),  
  * under the statistical-power, stratification, separation, and tie-break rules of §12 "Production-route selection",
* every fee dimension *that the production profile actually touches* (per the narrowed scope in §12) has at least one observed live exemplar,
* modelled fees reproduce posted broker charges within governance tolerance,
* realised slippage tracks the conservative spread/liquidity model within tolerance **at the ticket-size distribution the live profile actually generates**,
* no fee assumptions are being "forgiven" in reconciliation.

Exemplars for lifecycle states excluded by §5 and §12 (modified, overnight-persisting) are not required and their absence does not block this gate.

### Gate 3 — Alpha evidence

Live rotation (beyond Tier 0 plumbing trades) is allowed only when:

* **evidence source.** 
  * Out-of-sample LCB evidence is produced primarily by **off-platform shadow evaluation** over the research universe,  
    * applying the **same modelled cost stack, fee model, liquidity gate, admission rule, and re-submit budget** as production, i.e. the **Mode B production simulator** (§15) with pinned §6 governance parameters.
  * **Mode A (exploratory) simulator output is not admissible evidence for Gate 3.**
  * This is a deliberate sequencing choice: at Tier 0 the live universe intentionally suppresses decision frequency (2–4 ISINs, 1 funded position, threshold-first low-turnover rebalancing), 
    * so live trades alone cannot carry the statistical burden of establishing LCB in any reasonable horizon. Shadow decisions carry that burden; live trades verify path consistency.
* out-of-sample shadow decisions produce positive LCB excess benefit over do-nothing after full modelled costs,
* the evaluation horizon is long enough to be credible against the strategy's natural decision cadence,
* no single route/session/regime assumption is carrying the result,
* **ordering constraint.** Gate 3 may not be claimed passed until Gate 2 has closed, because an alpha LCB computed against an uncalibrated cost stack has no meaning.

### Gate 4 — Capital sufficiency

Position count `k` may be raised only when:

* `AUM_min(k, r, t)` is met for the active route,
* the cash buffer covers expected tax postings, FX slippage, and fee-misclassification reserve,
* observed live ticket sizes are consistent with `N_min(r, t)`.

### Scope-expansion rule

Adding a second live route or session is permitted only after **all four gates** have passed for the first profile and the new profile has independently cleared its own Gate 1 and Gate 2.

---

## 18) v2 scaffold

v2 is a future direction and requires all v1 gates to have passed.

v2 may add:

* event-aware overlays
* portfolio-reactive state machines
* intraday risk/event handling
* wider ETF universe
* alternate execution profiles
* event-driven simulation
* NLP/LLM or holdings-propagated signals

v2 may not weaken any of the following v1 disciplines:

* cash-account admissibility
* versioned reference data
* effective-dated tax state and two-ledger tax reconciliation
* deterministic replay under the canonical identity model
* broker-first reconciliation
* LCB-based economic admission per trade
* fee-closure before any new profile goes production
* settlement regime engine with forward-dated regime support