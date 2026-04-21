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

v1 recognises **two** legitimate terminal success states (see "Narrow-success state" below): **full-success v1** requires all four acceptance gates (§17) to pass **in sequence**; **narrow-success v1** requires Gate 1 to pass, with Gates 2–4 explicitly deferred under a documented continuation programme. The four gate criteria are **not co-equal parallel goals**; they are sequential programs, and each gate is admissible only after the prior gates have closed (see §17 "Sequencing rationale"). In summary form:

**Gate 1 — Operational correctness** (foundational)  
no unexplained reconciliation residuals in positions, settled cash, fees, and broker-posted tax cashflows; 
no avoidable live rejects; deterministic replay from strategy intent to broker result to journal state in the **economic-state-invariance**  
sense defined in §10 (identical position, cash, fee, and tax state under canonical ordering by `broker_perm_id` and `exec_correction_group_id`), including under broker corrections.

**Gate 2 — Cost-model closure** (after Gate 1)  
observed commissions, venue/clearing/regulatory fees, and slippage fit the conservative model within tolerance, 
across every fee dimension the **pinned** production profile touches (see §5, §12), based on **observed live exemplars**, not assumption.  
Gate 2 is a **closure** check on the pinned route, not a route-selection experiment. Route-comparison science is deferred to v2 (§18).

**Gate 3 — Alpha evidence** (after Gate 2)  
out-of-sample shadow decisions show a **strategy-level pessimistic lower-confidence-bound excess benefit** over the do-nothing state that exceeds the full modeled cost hurdle 
(see §6 "Economic admission rule", §15 "Scenario overlays"). LCB discipline applies at the **strategy/decision-category level**, not per trade.

**Gate 4 — Capital sufficiency** (after Gate 3)  
the derived route-specific minimum AUM for the intended position count (see §6) is met.

**Sequencing rationale.** Operational correctness, cost-model closure, alpha evidence, and capital sufficiency depend on each other in exactly that order: 
an uncalibrated cost stack invalidates any alpha LCB, and an unreconciled execution path invalidates the cost stack itself. 
Treating the four as co-equal gates would make the project fail for governance reasons even when the execution kernel works. 
v1 therefore **serializes** these gates rather than parallelizing them.

**Narrow-success state.** At the stated operating scale (EUR 5,000 starting capital, 2–4 live ISINs, typically 1 funded position, low-turnover cadence), the live-information budget available within a realistic v1 horizon is sufficient to close Gate 1 but is **not guaranteed** to close Gates 2–4. Gate 2 requires live exemplars across the ticket-size distribution the pinned profile actually generates; Gate 3 requires out-of-sample strategy-level LCB evidence; Gate 4 requires AUM that may exceed starting capital. v1 therefore explicitly recognises a **narrow-success outcome** — Gate 1 closed, Gates 2–4 deferred under a documented continuation programme — as a legitimate terminal state rather than a project failure. A narrow-success v1 leaves in place: broker-correct cash admission, correction-safe replay, exact journal reconciliation, and a manual German tax/compliance support layer, with Gates 2–4 scheduled under capital-scale-up governance. **Full-success v1** is all four gates closed. The two outcomes are both valid terminal states; neither is a default, and which is reached is determined by the evidence actually produced under live operation, not pre-committed.

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

### Explicitly deferred to v2 (or to a later v1 hardening pass)

The following are **architecturally acknowledged** in v1 reference-data shape so they remain additive later, but are **not built, exercised, or gated on** in v1. Each is deferred because its cost exceeds its v1 value at the stated capital and position count:

* **endogenous route-comparison science** — v1 pins a single production route at project start under a documented rationale (§5); comparison of realized all-in costs across candidate routes is a v2 task requiring statistical power that a one-position low-turnover account cannot generate,
* **forward-dated settlement regime entries and cutover test scenarios** (including the EU T+1 cutover on 11 October 2027) — v1 ships with only the currently active regime; §11 keeps the `settlement_regime_id` abstraction so forward-dated entries are additive later,
* **per-trade LCB gating** — v1 uses a simpler per-trade point-estimate cost check plus a strategy-level LCB certified under Gate 3 (§6, §17); per-trade LCB is deferred because a one-position micro-live programme cannot produce a defensible per-trade confidence margin,
* **hard XLM-driven universe freeze** — XLM remains a monitoring and review-trigger signal (§7, §13), not an automatic binding gate,
* **two-mode exploratory/production simulator** — v1 runs one conservative simulator bound to the pinned live profile, with scenario overlays for sensitivity (§15); a formal two-mode architecture is deferred because it formalises uncertainty rather than reducing it.

These deferrals narrow v1 to the scope the extension's re-scope recommendation identifies as finance-grade and achievable: one pinned live route, broker-first cash admissibility, append-only journal plus correction-safe replay, and a minimal manual German tax/compliance assist layer.

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

### Rule 6 — Stale data and transient divergence are handled, not frozen

"Reject unsupported cases" (Rule 1) is not "freeze on any disagreement between data sources". IBKR explicitly documents that Activity Statement Flex is daily-close data and Trade Confirmation Flex is delayed intraday data, so **transient mismatches between these sources are expected by design** and are not by themselves reconciliation breaks. The system therefore operates under three explicit data-state modes per reconciliation channel (broker cash, broker positions, fees, tax postings):

* **Green — fresh and agreeing.** Intraday source (TWS/IB Gateway) and expected state agree within governance tolerance; the live admission path operates normally.
* **Amber — stale or transiently diverging.** One source is behind its expected refresh cadence, or two sources disagree by less than the **tolerated divergence band** for that channel over a time window shorter than the **reconciliation-grace window** (both pinned as governance parameters under §14). The system continues to admit trades under **degraded-but-safe** rules: use the **more conservative** of the divergent values as the operative input (e.g., lower of projected vs broker-reported settled cash), log the divergence to the execution-event ledger, and schedule a re-check at the next refresh boundary.
* **Red — break.** Divergence exceeds the tolerated band, persists past the reconciliation-grace window, or an authoritative source (TWS/IB Gateway live cash) is unreachable. Live admission is frozen on the affected channel until investigated. This is the only case that invokes the §11 "reconciliation break" behaviour.

The tolerated divergence band and reconciliation-grace window are set conservatively at v1 start and tightened as live exemplars accumulate. Amber transitions are normal operating state, not exceptions.

---

## 5) Concrete v1 live trading profile

### Supported v1 production execution profile

v1 live trading allows and supports exactly **one** production profile:

* **Broker:** Interactive Brokers cash account
* **Instrument type:** long-only UCITS ETFs
* **Currency:** EUR-funded account, EUR trading lines only
* **Venue / route:** **one production route, pinned at v1 start under a documented rationale.**
  * The default pinned route is **SmartRouting Fixed**, selected on two grounds: (i) the lowest stated Germany minimum commission (EUR 3) among the candidate retail-accessible routes, which dominates the per-ticket cost floor at the EUR 1,500–5,000 ticket range v1 actually generates, and (ii) the absence of the directed-API Tiered-pricing restriction, which reduces fee-path ambiguity.
  * **Status of the pinning rationale.** SmartRouting Fixed is pinned as an **operationally defensible v1 simplification**, not as a production-grade "best route" verdict. The public evidence supports only that it has a lower stated broker minimum-commission floor than several German direct routes at v1 ticket sizes; it does **not** establish superior all-in ETF execution quality once venue choice, fee pass-through, and fill behaviour are considered. The route-selection evidence required for that verdict is a v2 activity (§18); until then, the v1 pin is accepted on floor-commission grounds and is explicitly an engineering choice, not an empirical claim about route quality.
  * The alternative candidate **Xetra directed** remains documented in reference data as a shadow profile that may be run in paper/simulation but is **not** subject to mandatory live-exemplar collection in v1. This deliberately breaks the §17 gate circularity: pinning the route is a v1-start decision, not a Gate 2 output.
  * **Why not empirical route selection in v1.** At a one-position, low-turnover cash account with ticket sizes dominated by fixed commission minima, observed realized-cost differences between SmartRouting Fixed and Xetra directed cannot be statistically separated from noise within any realistic v1 horizon. A formal comparison is deferred to v2 (§18), where a larger position count and richer exemplar set can support it.
  * **Re-pinning.** The pinned route may be changed during v1 only via a signed governance decision under the evidence registry (§14) that documents why public pricing or broker policy has shifted enough to overturn the start-of-v1 rationale. Such a change re-opens Gate 2 for the new route.
* **Session:** **continuous trading only**
* **Trading hours:** 09:00–17:30 CET only
* **Order type:** **DAY limit** (limit-price construction per §13)
* **Routing / pricing plan:** SmartRouting Fixed pricing. Tiered is unavailable for directed API orders; this does not apply to the pinned route.
* **No use of:** extended retail session, auctions, MOC/MOO/IOC, overnight persistence, or order modification as a normal operating pattern

### Shadow execution profiles

Routes other than the pinned production route remain runnable in paper or simulation as research-only artifacts. A shadow route can be promoted to production only via a governance re-pinning decision followed by an independent Gate 1 and Gate 2 pass on the new profile (§17). No shadow route is eligible for production promotion on the basis of micro-live realized-cost comparisons in v1.

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
* `R(r, t)` = reserve for spread, fee misclassification, and tax friction (see composition note below),
* `b_1w` = allowed one-way cost budget as a fraction (e.g., 10 bps = 0.001).

**`R(r, t)` composition note.** Because v1 live scope is EUR-only (§5), the FX component of `R` is **fixed at zero** for the v1 production profile and is documented as such in the governance pinning decision. FX reserve returns only if a non-EUR trading line is ever added, at which point it is re-scoped as an admission input for that profile.

`b_1w` is a governance parameter, not a backtest convenience. It is conservative and pinned per route.

### Minimum AUM schedule (per-account constraint)

For a target number of simultaneously funded positions `k`, the minimum account size under route `r` at date `t` is:

```
AUM_min(k, r, t) = k · N_min(r, t) + cash_buffer(t)
```

where `cash_buffer(t)` covers tax postings and the fee-misclassification reserve (the FX component is zero under v1 EUR-only scope per the `R(r, t)` composition note above).

**`cash_buffer(t)` sizing rule.** Pre-trade admission can be locally correct while annual tax liquidity is nevertheless underreserved if `cash_buffer` is not sized against the full expected annual tax posting stream. For v1, `cash_buffer(t)` is pinned as the larger of:

* a **floor** (fee-misclassification reserve plus a documented working-cash minimum), and
* a **tax-posting upper bound** computed from the statutory state ledger (§9) at the start of the fiscal year, covering: expected Vorabpauschale-driven withholdings, expected distribution-tax postings, and a worst-case deemed-sale/deemed-repurchase cash effect under §22 InvStG for every live tax lot whose Teilfreistellung regime could plausibly transition within the year.

The upper-bound component is refreshed at each annual tax-close checkpoint (§9) and whenever the statutory state ledger records a regime transition. The sizing rule is pinned alongside the other §6 governance parameters; its numeric inputs are pinned at v1 start under conservative placeholders and re-pinned as live exemplars accumulate. Running with `cash_buffer` below the tax-posting upper bound is a governance exception requiring a signed decision record.

### Tiered capital-state policy

The following tiers gate live behaviour:

* **Tier 0 - Proving account.** Default at starting capital. Maximum 1 funded position. No discretionary rotation. Trades only to validate plumbing.
* **Tier 1 - One-position economic trading.** Allowed only once `AUM_min(1, r, t)` is met *and* live fee/slippage tracking is within tolerance for the route.
* **Tier 2 - Two funded positions.** Allowed only once `AUM_min(2, r, t)` is met, Tier 1 has accumulated sufficient observed-cost evidence, and the alpha gate (below) clears for rotation.

Transitions between tiers require explicit governance approval; they do not auto-activate on AUM changes.

### Economic admission rule — two-level formulation

Per-trade LCB computed against a one-position, low-turnover live programme is pseudo-precision: the calibration sample for a per-trade confidence margin cannot be assembled within any realistic v1 horizon. v1 therefore splits economic admission into a **per-trade point-estimate check** (cheap, decidable at decision time) and a **strategy-level LCB certification** (statistically credible, refreshed at review cadence):

**Per-trade admission — fill-conditional in v1.** v1 admission evaluates the expected excess benefit **conditional on fill**, not over the joint fill/no-fill outcome space. At retail ticket sizes, public venue data do not support reliable inference of a true per-order fill probability from snapshot-style book inputs; folding a bootstrap `p_fill` estimate into the admission scalar would introduce pseudo-precision that can dominate the actual alpha signal. Because the v1 live profile is DAY-limit, continuous-only, with no overnight persistence and no normal modify/replace (§5), an unfilled limit expires and the account returns to the do-nothing state at zero incremental cost, so a fill-conditional admission rule is well-defined and conservative. Fill probability remains first-class in simulation and stress (§13, §15), but it is **not** an input to the admission scalar in v1.

A rebalance or entry is admitted only if **all** of the following hold at decision time:

1. The **point-estimate** expected excess benefit of the trade **conditional on fill** versus the do-nothing state exceeds the full modeled cost hurdle:

   ```
   E[ Δα_vs_do_nothing | fill ] > Ĉ_RT + B_model + B_ops
   ```

   where
   * `E[... | fill]` is the point-estimate expected excess benefit under the live strategy's signal, conditional on the DAY limit filling,
   * `Ĉ_RT` is expected round-trip all-in cost (commission + venue/clearing/regulatory fees + spread + tax friction; FX = 0 under v1 EUR-only scope), including the worst-case commission charge under the per-decision re-submit cap (§13),
   * `B_model` is a model-risk buffer,
   * `B_ops` is an operational buffer covering fee-misclassification and residual reconciliation risk.

2. The ticket size meets or exceeds `N_min(r, t)` for the active route.
3. The account's current tier permits the proposed position count.
4. Turnover over the churn-budget window remains within its cap.
5. The strategy emitting the trade holds a **currently valid Gate-3 strategy-level LCB certification** (see below). A strategy without a live Gate-3 certification may not emit admissible trades beyond Tier-0 plumbing.

Every admission decision **records** the simulator's bootstrap-prior `p_fill` estimate as telemetry on the execution-event ledger (§10) for later calibration, but that estimate does not gate admission in v1.

**Strategy-level LCB certification (Gate 3 input).** At review cadence (§13), the strategy as a whole is evaluated off-platform against the research universe using the v1 simulator with pinned parameters (§15). Gate 3 admits the strategy to live rotation only if:

```
LCB[ Δα_vs_do_nothing | fill, strategy, horizon ] > Ĉ_RT + B_model + B_ops
```

computed at the **strategy/decision-category level** over the Gate-3 evaluation horizon on fill-conditional evidence, where `LCB[...]` is the pessimistic lower bound (point estimate minus a calibration-derived margin) of strategy-level excess benefit. The Gate-3 pass must additionally **survive the mandatory fill-probability stress overlay** (§15): a strategy whose fill-conditional LCB clears hurdle at bootstrap-prior `p_fill` but fails under the tightened stress `p_fill` is **not** a Gate-3 pass. A strategy whose LCB expires or degrades below hurdle loses live-rotation admission until re-certified; existing positions are not forced liquidated but no new rotation trades are admitted.

This two-level design preserves the LCB discipline where it can be statistically supported (at the strategy level, across many shadow decisions) while keeping per-trade admission decidable with the information actually available at a single trade, without importing a fragile fill-probability estimate into the admission scalar.

### Governance parameters and pinning

`N_min`, `AUM_min`, and the admission rule are **formally incomplete** until their governance inputs are pinned.  
The following are **governance parameters**, not free engineering constants, and each requires a documented,  
versioned decision under the evidence registry (§14) **before Gate 2 can be evaluated**:

* `b_1w` — one-way cost budget (fraction of notional).
* `R(r, t)` — the spread / fee-misclassification / tax-friction reserve, with its composition itemised per route (FX component fixed at zero for v1 EUR-only scope per composition note above).
* `cash_buffer(t)` — composition and sizing, including the fee-misclassification reserve portion.
* `B_model` and `B_ops` — buffer magnitudes in the per-trade and strategy-level admission rules.
* **Strategy-level LCB confidence-margin calibration** — the procedure and sample requirements that convert a point estimate of strategy-level excess benefit into its pessimistic lower bound (sampling distribution assumption, calibration window, calibration-refresh cadence, and the Gate-3 evaluation horizon).
* **Churn-budget window and cap** — explicit numeric bounds.
* **Per-decision re-submit cap** — explicit numeric bound (see §13).
* **Tolerated divergence band** and **reconciliation-grace window** per reconciliation channel (see §4 Rule 6, §11).

Until each of these is pinned, the engine operates in **exploratory mode** under documented conservative placeholders: `b_1w`, `R`, 
and the buffers are set to the upper end of plausible values, and no Gate-2 cost-closure claim and no Gate-3 strategy-level alpha claim may be asserted using exploratory placeholders. 
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

The **XLM signal** (Xetra Liquidity Measure at the nearest standardized size; see §13) participates in live-universe admission and monitoring in two non-binding roles:

* **Initial admission.** At the time an ISIN is added to the live universe, its XLM at the nearest standardized size must classify it as sufficiently liquid under a documented ISIN-level threshold. An ISIN failing this check is **not** admitted. This is a standing, universe-level check, not a per-ticket decision.
* **Ongoing monitoring.** Sustained deterioration of an ISIN's XLM triggers a **manual review** of that ISIN's live status; it does **not** by itself automatically freeze buying. The reviewer may freeze, retain, or down-weight the ISIN with a signed decision record. This deliberately avoids hard XLM-driven freezes at retail scale where XLM's standardized round-trip reference size is an order of magnitude larger than the live ticket and does not map cleanly to retail one-way fill cost.

XLM also feeds the conservative slippage reserve per §13. Under no circumstance does XLM directly accept or reject an individual order at retail scale.

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

The compliance layer is **tradable-line-centric** and pre-trade, not best-effort. A "tradable line" is the operational unit on which v1 admits orders and is identified by the composite key:

```
{ fund legal identity (ISIN), listing venue, trading currency,
  broker contract metadata, effective date }
```

This is explicitly **not** an ISIN-only abstraction. The same fund can be the same legal product but operationally different across listing lines for routing, spread, fee-path, retail availability, and contract representation at the broker. A single compliance decision on ISIN alone therefore cannot govern live admission.

Each tradable line in the live universe must have a signed decision record with:

* fund legal identity: ISIN and fund legal name
* fund structure
* asset-class classification
* commodity exposure determination
* single-stock ETF determination
* employer-restriction pass/fail
* Germany retail-marketability evidence
* PRIIPs KID validity interval
* **listing line identity:** venue, MIC, trading currency
* **broker contract metadata:** IBKR contract ID (`conId`), contract-level reference data snapshot
* reviewer identity and review date
* evidence package hash and source list

Where the fund-level evidence (classification, KID, retail marketability) is shared across listing lines of the same fund, it is recorded once in the evidence registry (§14) and referenced by each listing-line record to avoid duplication while keeping operational controls correctly scoped.

### Compliance control rule

No new tradable line may go live without a manual compliance review entry.  
If the fund-level evidence or any listing-line-level evidence becomes stale or contradictory, the affected tradable line is frozen from further buying until reviewed again. Staleness on a fund-level record freezes **all** tradable lines for that fund.

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

**Data-provenance requirement.** Every dated entry in the statutory tax state ledger — fund classification, Teilfreistellung regime, Basiszins input, and each regime transition — must carry a pointer to an evidence-registry record (§14) that names the **authoritative external source**, the retrieval timestamp, the document hash, and the reviewer identity. For v1, authoritative sources are:

* **Basiszins**: the relevant BMF (Bundesministerium der Finanzen) annual publication,
* **Fund classification and Teilfreistellung regime**: the fund's KIID/KID, prospectus, and issuer tax-transparency publications, corroborated where available by broker-reported fund classification,
* **Regime transitions**: the specific document or posting that establishes the transition date.

A statutory tax-state update without a resolved evidence-registry pointer is not valid and cannot post to the ledger. This makes tax-defensibility a function of evidence discipline, not just ledger shape.

### Authority precedence and disagreement resolution

When the three tax information sources — issuer publications, broker classification metadata, and broker-posted tax cashflows — disagree, year-end close can stall indefinitely without an explicit precedence order. v1 resolves this by scoping each source to the ledger it governs and defining a single exception workflow for disagreements.

**Precedence for the statutory tax state ledger (Ledger A, §9.A):**

1. **Issuer legal documents** — KID/KIID, prospectus, and issuer tax-transparency publications — are authoritative for fund classification, Teilfreistellung regime, and the date of any regime transition.
2. **BMF publications** — authoritative for the Basiszins input.
3. **Broker-reported fund classification** — corroborating control source only. A disagreement between broker classification and issuer classification is resolved in favour of the issuer, logged as a disagreement exception, and flagged for the annual tax-close checklist.

**Precedence for the tax-cash ledger (Ledger B, §9.B):**

1. **Broker statement postings** are the authoritative record of cashflows actually booked to the account.
2. **Broker corrections supersede** earlier postings under the correction-group mechanism (§10); the earlier posting is not mutated but superseded within its group.

**Cross-ledger disagreements.** When the §9 annual reconciliation residual exceeds the governance tolerance, the disagreement is **not** auto-resolved toward either ledger. It creates a **tax exception record** in the evidence registry (§14) containing: the statutory ledger state, the posted cashflow set, the unexplained delta, candidate reconciliation hypotheses, and a reviewer assignment. Year-end close does not pass while an unresolved tax exception is open. The reviewer may close the exception by (i) correcting the statutory ledger with a new evidence-registry-backed entry, (ii) booking an explicit bridge adjustment that identifies a known statutory-vs-cashflow timing difference, or (iii) declaring the residual within a signed de-minimis tolerance documented in the exception record. No closure path allows silent absorption of the delta.

**Retrospective broker changes.** Broker restatements of prior-year tax postings are posted to Ledger B under the correction-group mechanism (§10) with effective dates preserved, and automatically open a tax exception record for the affected prior year. The reviewer evaluates whether the prior annual reconciliation needs re-opening. A re-opened prior-year reconciliation is journalled as a new reconciliation artifact; the original is preserved unchanged.

**Retrospective issuer changes.** Issuer publication of a classification or regime change with retroactive effective date is posted to Ledger A under its effective date, not the publication date, and opens a tax exception for every affected prior year. If the retroactive change crosses a fiscal-year boundary, the tax-close checklist for the affected prior year is re-opened; deemed-sale/deemed-repurchase recomputations under §22 InvStG are handled as explicit, manually-approved events consistent with §9's v1 operational minimality rule.

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
* `admission_decision_id` — unique per admission evaluation (links to the per-trade admission-rule inputs used per §6, plus the bootstrap `p_fill` telemetry recorded at decision time per §13 — the latter is recorded on the decision but, in v1, does **not** enter the admission scalar)
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

### Fallback identity for non-API-originated activity

IBKR documents that `PermId` can be **0** for trades that do not originate through the broker's API — for example, manual trades entered through the client portal, broker-side adjustments, and emergency manual exceptions permitted under v1 (§13 limit-price construction; emergency override). Because v1 retains manual-exception paths, using `broker_perm_id` as the sole canonical identity would cause the replay model to break exactly in the exception cases that matter most.

v1 therefore defines a **fallback composite identity** used **only** when `broker_perm_id = 0` (or absent):

```
fallback_identity = (
  account_id,
  trade_date,
  contract_id,          // IBKR conId from the tradable-line record, §8
  side,
  cumulative_qty,
  avg_fill_price,
  statement_posting_id  // daily activity-statement posting identity
)
```

Fallback-identity rules:

1. Fallback identity is used **only** when `broker_perm_id` is 0 or absent; it never competes with `broker_perm_id` when the latter is populated.
2. Every event recorded under fallback identity is flagged `origin = "non_api"` on the execution-event ledger and linked to the governance exception record that authorised the activity (for manual exceptions) or to the broker posting that introduced it (for broker-side adjustments).
3. Corrections to a fallback-identity event are grouped by `exec_correction_group_id` the same way as API-originated events; `statement_posting_id` carries the correction linkage when `broker_perm_id` remains unavailable across the correction.
4. Economic-state invariance replay (Rule 4) extends over fallback-identity events: the canonical ordering key becomes `coalesce(broker_perm_id, fallback_identity)` paired with `exec_correction_group_id`. Any replay that produces different economic state for an event stream containing fallback-identity records is a replay failure.
5. Fallback-identity usage is **audited** at each reconciliation: a spike in non-API-originated volume is a governance signal, not a normal operating state.

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
* **reconcile** broker-posted settlement against engine projection under the Green/Amber/Red data-state model (§4 Rule 6).

Regime-transition testing (including the EU T+1 cutover on 11 October 2027) is **v2 scope** and is not exercised by the v1 engine (§2 "Explicitly deferred to v2", §18).

If broker-reported settled cash and the engine projection disagree by more than the governance tolerance and the divergence persists past the reconciliation-grace window (§4 Rule 6), the divergence is a **Red-state reconciliation break**.  
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

**v1 scope.** v1 ships with **only the currently active regime** for the pinned production profile. The regime-id abstraction is preserved so that forward-dated entries and transition logic can be added additively later, but v1 does **not** build, test, or gate on:

* forward-dated regime entries for the EU T+1 cutover on 11 October 2027,
* transition test cases for orders straddling a regime change,
* explicit behaviour for in-flight unsettled trades at regime change.

Building a forward-dated regime engine in v1 adds testing and governance surface area without improving live admissibility today — once broker-reported settled cash is the live authority (above), the local settlement engine is a forecast/reconciliation utility rather than a trading authority. Forward-dated regime support is scheduled for pre-cutover hardening or v2 (§18).

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

* routing mode of the **pinned** route (the route is fixed at v1 start per §5; exemplars are collected for the pinned route only),
* session regime: **continuous trading only** (extended retail, auctions are excluded from the production fee matrix because §5 excludes them),
* instrument class (UCITS ETF),
* order lifecycle state: **new orders only** — modified, replaced, and 
  * overnight-persisting orders are **explicitly out of scope** for the production fee matrix because §5 and Rule 1 exclude them from normal live operation; 
  * no fee-closure exemplar for these states is required to pass Gate 2, and none is collected in production,
* retail-vs-direct handling path as actually configured on the live account,
* Xetra-specific fee treatments where applicable to the pinned route.

Retail Xetra fee waivers are **not** assumed by model. They are only applied when live broker postings demonstrate the account is actually on that fee path.

### Gate-2 scope: closure, not selection

Gate 2 is a **fee-model closure** check on the pinned production route (§5), not a route-selection experiment. The exemplar set must cover the ticket-size distribution the live profile actually generates (at minimum: near `N_min(r, t)`, mid, and upper bound of the expected distribution), and:

* modelled fees must reproduce posted broker charges within the pinned governance tolerance across every dimension the production profile touches,
* realised slippage must track the conservative spread/liquidity model within tolerance at that ticket-size distribution,
* no fee assumptions may be "forgiven" in reconciliation — unexplained deltas block Gate 2.

Route-comparison science (stratification across multiple candidate routes, separation criteria, paired-observation tests, tie-break rules) is **deferred to v2** (§18). Attempting it in v1 would require statistical power that a one-position, low-turnover account cannot generate within any realistic v1 horizon; running it anyway would produce a verdict dominated by fixed-commission-minimum noise. If, during v1, live experience suggests the v1-start route rationale has become untenable, the remedy is a documented governance **re-pinning** decision (§5), which reopens Gate 2 for the newly pinned route — not an in-flight route-selection experiment.

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
Trades can occur between reviews only if a threshold breach is large enough to clear the per-trade admission hurdle (§6) — i.e. the point-estimate expected excess benefit **conditional on fill** exceeds the modelled round-trip cost stack (inclusive of the worst-case re-submit commission charge), and the strategy holds a current Gate-3 certification.

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

1. **Universe-admission check (ISIN-level).** An ISIN is admissible to the live universe (§7) only if its XLM at the nearest standardized XLM size classifies it as sufficiently liquid under a documented ISIN-level threshold at admission time.  
   This is a **slow, standing, universe-level** check at the point of universe admission, not a per-ticket decision. Sustained deterioration in an ISIN's XLM **triggers a manual review** of that ISIN's live status (§7); the reviewer may freeze, retain, or down-weight the ISIN with a signed decision record, but XLM alone does not automatically freeze buying.
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

### Fill-probability as telemetry and stress input (not admission scalar) in v1

For DAY-limit execution, realized implementation shortfall is driven not only by on-fill slippage but also by **queue position, no-fill probability, and the foregone-trade opportunity cost** of an unfilled limit. A deterministic limit-price rule with spread caps addresses on-fill slippage but is silent on whether the order fills at all.

At retail ticket sizes in a one-position, low-turnover programme, **public venue data do not support reliable inference of a true per-order `p_fill` from snapshot-style book inputs**. Folding a bootstrap `p_fill` into the per-trade admission scalar (§6) would introduce pseudo-precision that can dominate the actual alpha signal and cannot be calibrated out within any realistic v1 horizon. v1 therefore scopes fill probability to three roles — **none of which is the per-trade admission scalar**:

1. **Telemetry on every admission decision.** The simulator's bootstrap-prior `p_fill` estimate at the chosen limit price is recorded on the execution-event ledger (§10) alongside the admission decision, so that live-exemplar fill/no-fill data can later be joined against a consistent model-time estimate. Telemetry does not gate admission.

2. **First-class simulator output.** The canonical simulator (§15) generates no-fill paths as a modelled outcome, not an edge case. Canonical runs track fill rate, mean time-to-fill, and foregone-alpha on no-fill as first-class output metrics alongside realized slippage on fills.

3. **Mandatory scenario-stress input to Gate-3 LCB certification.** Gate 3 (§17) requires the fill-conditional strategy-level LCB (§6) to **survive the fill-probability stress overlay** (§15): a strategy whose fill-conditional LCB clears hurdle at bootstrap-prior `p_fill` but fails under tightened stress `p_fill` is not a Gate-3 pass. This preserves the honest treatment of no-fill risk without importing a fragile point estimate into the per-trade admission scalar.

**Fill model — bootstrap phase.** For telemetry and stress, v1 uses a **conservative bootstrap** fill model: `p_fill` is a monotone-decreasing function of limit aggression inside the spread, anchored to a conservative prior drawn from venue order-book snapshots (queue depth and replenishment rate at the nearest book levels). The bootstrap deliberately under-estimates `p_fill` so that Gate-3 stress runs surface optimism rather than hide it.

**Fill model — exemplar-calibrated phase.** As live fills and no-fills accumulate under the pinned production profile, a calibrated fill model replaces the bootstrap for telemetry and stress roles. The calibration procedure (minimum exemplar counts, calibration window, refresh cadence) is pinned as a governance parameter under §6. **Promoting `p_fill` from a stress input into an admission-scalar input is a v2 activity** (§18), conditional on the calibrated model demonstrating stability across the live exemplar set — not a v1 decision.

**Re-submit fee interaction.** The per-decision re-submit cap (below) is priced into `Ĉ_RT` in the per-trade admission rule (§6): the worst-case commission charge under the re-submit cap is included in the cost hurdle, so fill-conditional admission does not understate re-submit exposure. Each re-submit adds a fresh minimum-commission charge and is recorded on the execution-event ledger with its own `strategy_intent_id` linkage (§10), so decision-level realized cost remains reconstructable even though `p_fill` is not in the admission scalar.

No-fill remains a legitimate outcome of the DAY limit: an expired unfilled limit is the cheaper failure mode relative to a modified/overnight order or an over-aggressive resubmit loop. v1 makes that trade-off explicit through stress-overlay discipline rather than through an admission-time probability estimate.

### Cancel/re-submit budget (fee-trap control)

The "no modify, single-shot cancel/re-submit" policy avoids the modify/overnight fee trap but exposes a different one:  
because IBKR documents that modified or overnight-persisting orders may be treated as new orders for commission-minimum purposes,  
and because a cancel-and-re-submit is likewise treated as a new order, **each re-submit incurs a fresh minimum-commission charge**.  
At a EUR 3 minimum, two re-submits on a EUR 1,500 ticket turn a 20 bps floor into a 60 bps floor.

To contain this without reintroducing modify/replace, v1 imposes:

* a **per-decision re-submit cap** (pinned as a governance parameter under §6),
* a **decision-level fee pre-check** that includes the worst-case commission charge under the re-submit cap when evaluating `N_min` and the per-trade admission rule (§6),
* **mandatory recording** of every re-submit on the execution-event ledger (§10), with its own `strategy_intent_id` linkage so that decision-level realized cost is reconstructable,
* a **cumulative re-submit budget per rolling window**, audited at reconciliation; exceeding the budget freezes further live admission pending review.

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

### Sequencing note

The reference-data store, evidence registry, and as-of versioning are **supporting infrastructure** for the core execution kernel (§10, §11), not a precondition for it. In delivery sequence (§16), the append-only journal, the execution-event ledger, `broker_perm_id`-anchored identity, and correction-safe replay are built and validated **first**; the evidence registry and dated-reference-data machinery are built alongside, at the minimum scope the live universe actually requires (2–4 ISINs, one funded position typical), and expanded only as that scope expands. Governance metadata must not precede proof that the kernel can reconcile a small number of live trades.

---

## 15) Simulation and backtest rules

### One conservative simulator with scenario overlays

The simulator is a **single** artifact bound to the pinned v1 live profile (§5), not a two-mode architecture. The rationale for a two-mode design disappears once the production route is pinned at v1 start (§5) rather than being a Gate-2 output: there is no longer a structural gap between "pre-Gate-2 exploratory" and "post-Gate-2 production" simulator configurations to bridge. A two-mode design formalises uncertainty rather than reducing it, and creates a separate class of runs (Mode A) that is definitionally inadmissible for the gates, which is net negative for the project.

The simulator therefore runs in **one canonical configuration** plus a bounded set of **scenario overlays**:

**Canonical configuration.**

* binds to the **pinned** production route, fee path, and fee model,
* uses governance parameters from §6 — **pinned values where available, and documented conservative placeholders otherwise** (upper end of plausible range, per §6 "Governance parameters and pinning"); placeholder usage is recorded per run so that any run whose evidence role depends on pinned values can be identified,
* applies the **exact** live admission logic, including the per-trade fill-conditional admission rule (§6), the ticket-size-native liquidity gate (§13), and the re-submit budget (§13); the §13 fill model is present in the simulator for telemetry and stress-overlay use, but — consistent with v1 admission — does **not** enter the admission scalar,
* uses dated fund metadata, dated tax state, and the currently active settlement regime (§11),
* is tagged `simulator_run_kind = "canonical"` on every output, together with the snapshot identifiers required by §14 "As-of versioning" and a flag indicating which §6 governance parameters were pinned vs placeholder at run time.

**Scenario overlays.** A canonical run may be re-executed under a named overlay that perturbs a bounded, named set of inputs — for example, alternate-route fee schedules, stress fee/spread/slippage profiles, stress fill-probability profiles, settlement-delay stress, or stale-reference-data scenarios. Overlay runs are tagged `simulator_run_kind = "overlay:<name>"` and carry a reference to the canonical run they perturb. **Overlay runs are research-only artifacts** and are not admissible evidence for any gate; they feed sensitivity analysis and risk surfacing, not certification.

**Gate evidence rule.** Only canonical runs whose §6 governance parameters are **all pinned** (not placeholders) at run time may feed Gate 2 cost-closure arguments and Gate 3 strategy-level LCB evidence. A canonical run using any placeholder value is research-grade output usable for internal decision-making but not gate evidence.

### Simulator validity rule

A backtest is invalid if it admits any order that live v1 would reject under:

* settled cash (under the Green/Amber/Red data-state model from §4 Rule 6),
* reserved cash,
* supported session,
* supported order type,
* fee classification under the pinned production fee model,
* compliance policy (tradable-line level per §8),
* the per-trade admission rule (§6), including fill-conditional `E[Δα | fill]` — `p_fill` does not enter the admission scalar in v1 and enters simulation only through the stress overlays below,
* strategy-level LCB certification status (a run that admits trades from a strategy without a current Gate-3 certification is a research run, not a production simulation),
* ticket-size-native liquidity gate (spread cap, window, observed-slippage reserve; XLM enters only through universe admission and the conservative reserve per §13, not as a per-ticket budget).

Overlay parameters may be varied within a single overlay run; a single run may not mix inputs from two different named overlays.

### Backtest realism requirements

The canonical simulator must use:

* the exact production fee model for the pinned live profile (post Gate 2 for gate-admissible runs),
* the same `N_min` and tier rules,
* the same per-trade admission rule from §6 with pinned parameters (for gate-admissible runs) or documented placeholders (for research runs),
* the **bootstrap (or exemplar-calibrated) fill model** from §13 — canonical runs produce fill rate, mean time-to-fill, and foregone-alpha on no-fill as first-class output metrics, but `p_fill` does **not** enter admission in v1 (it enters only through the fill-probability stress overlay and as simulator telemetry),
* the same threshold/churn policy,
* the currently active settlement regime (§11) — forward-dated regimes are v2 scope,
* the same session gating and ticket-size-native liquidity gate,
* conservative spread assumptions when live exemplars are missing,
* dated fund metadata and tax state with evidence-registry pointers (§9, §14),
* the pinned re-submit budget and its fee consequences.

### Mandatory stress tests

Before paper trading, run the canonical simulator plus the following scenario overlays:

* fee stress
* spread stress
* **fill-probability stress** (tightened `p_fill` relative to bootstrap, to surface strategies whose Gate-3 LCB depends on optimistic fill assumptions)
* settlement-delay stress
* stale-KID / stale-classification freeze tests
* broker-correction replay tests (verifying `exec_correction_group_id` semantics)
* order-partial / reject / cancel path tests
* re-submit-budget exhaustion tests
* Green/Amber/Red data-state transition tests (§4 Rule 6): Amber behaviour under Flex-vs-TWS divergence, Red escalation, and recovery paths.

Settlement-regime-transition stress (including the 2027 T+1 cutover) is **deferred** to pre-cutover hardening or v2 and is not a v1 mandatory stress test (§11, §18).

---

## 16) Paper, micro-live, and scale path

### Phase 0 — Economic feasibility pre-screen

Before any of the Phase-A build is industrialised, v1 runs a cheap, deterministic **economic-feasibility pre-screen** using only known broker fixed costs and the published venue fee schedule.  
Its purpose is to avoid building a full production-control stack around a strategy whose economics cannot clear the fixed-cost floor at the available capital.

The pre-screen computes, on current IBKR Germany public pricing:

* the **implied one-way floor** in bps at each candidate operational ticket size (e.g., EUR 1,500 / EUR 3,000 / EUR 5,000), from the minimum commission alone, for the pinned production route (§5),
* the **implied round-trip floor** including venue/clearing/regulatory fees under conservative assumptions,
* the **theoretical minimum strategy-level LCB excess benefit** any v1 alpha must deliver to clear `Ĉ_RT + B_model + B_ops` under exploratory placeholders.

**Falsifiability requirement.** A pass criterion stated as "at least one plausible alpha specification exists" is narrative, not falsifiable. To be finance-grade and serve as a stop-loss on project effort, the pre-screen must **freeze, in evidence, before Phase A engineering begins**, the following four items:

1. **Research protocol.** The specific feature construction, ranking rule, rebalance trigger, and universe the pre-screen's candidate alpha is evaluated on — fixed in writing, not chosen after seeing results.
2. **Out-of-sample evaluation window.** The calendar span, the in-sample / out-of-sample split, and the decision cadence inside that out-of-sample window — fixed before any out-of-sample numbers are computed.
3. **Required net effect size.** The minimum strategy-level net excess benefit, **after the implied round-trip floor and under exploratory-placeholder buffers `B_model` and `B_ops` at their upper-end values**, that the candidate alpha must deliver to pass. This is a single numeric threshold, pinned in the evidence registry (§14) at the time the protocol is frozen, not negotiated afterwards.
4. **Pre-registered decision rule.** A single pass/fail statement of the form "candidate alpha passes iff its out-of-sample net excess benefit, computed per (1)–(2), clears the threshold in (3)," signed and timestamped before the out-of-sample window is evaluated.

Pre-screen **pass criterion:** the frozen candidate alpha's out-of-sample net excess benefit, computed under the frozen protocol and window, clears the frozen required effect size.

Pre-screen **fail criterion:** (i) the frozen candidate alpha does not clear the frozen threshold, or (ii) the implied floor dominates any plausible ETF-native net excess benefit at the available capital before evaluation is even attempted, or (iii) the protocol, window, or threshold cannot be frozen in advance (which is itself a fail).

On fail, v1 is **re-scoped before further build** — by raising capital, narrowing to a single buy-and-hold instrument with no rotation, or closing v1 without an execution engine. Re-running a failed pre-screen against a **new** candidate alpha is permitted only as a **fresh pre-screen** with its own frozen protocol, window, and threshold recorded in evidence; silently re-evaluating the same data against a relaxed threshold is forbidden.

The pre-screen is not a gate — it is a stop-loss on project effort. Its four frozen inputs, the out-of-sample result, and the pass/fail verdict are all journalled under the evidence registry and referenced by Gate 3's cost-stack argument. A Gate-3 LCB certification (§17) may not use a required effect size looser than the one pinned at the pre-screen; tightening is permitted, loosening is a governance exception.

### Phase A — build and offline validation

Deliver:

* append-only journal, execution-event ledger, and `broker_perm_id`-anchored identity (§10)
* immutable broker/event archive
* derived reconciliation views
* reference-data store and evidence registry at the scope the live universe requires (2–4 ISINs), built alongside the kernel per §14 sequencing note
* canonical simulator bound to the pinned live profile (§15), using pinned §6 parameters where available and documented conservative placeholders otherwise; scenario overlays enabled for sensitivity analysis

Exit only when historical and synthetic replay passes, including correction-group replay.

### Phase B — paper trading

Run the exact production profile in paper with:

* same admission logic (per-trade fill-conditional rule from §6, ticket-size-native liquidity gate from §13; §13 fill model operating in its telemetry/stress role only)
* same fee model
* same journal
* same reconciliation harness
* same Green/Amber/Red data-state handling (§4 Rule 6)

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

The project uses **four** sequential acceptance gates (see §1 "Sequencing rationale"). A later gate cannot be claimed passed while an earlier gate has not closed, and no gate may require as an input any artifact that only a later gate can produce.

### Gate 1 — Operational correctness

The system may trade micro-live (Tier 0) only when:

* Phase 0 economic-feasibility pre-screen (§16) has passed or been explicitly waived under a signed exception,
* broker events, trade confirmations, and activity statements reconcile under the Green/Amber/Red data-state model (§4 Rule 6); Amber divergences within the tolerated band and grace window are expected and not Gate-1 blockers; only Red breaks block this gate,
* no unexplained cash or fee residuals remain after Amber-window resolution,
* the **pinned** live profile (§5) has a complete, signed definition — route, session, order type, pricing plan, and pinning rationale — in the evidence registry; the pinning decision is a v1-start governance output, not a Gate-2 output,
* compliance and tax evidence records exist for every live **tradable line** (§8) and for every fund-level record they reference,
* the canonical simulator (§15) and the live admission engine share identical admission logic across the per-trade fill-conditional rule (§6), the ticket-size-native liquidity gate (§13), and the re-submit budget (§13); the §13 fill model is present in the simulator in its telemetry/stress role only, consistent with v1 admission,
* deterministic replay in the economic-state-invariance sense (§10), including broker corrections under `exec_correction_group_id`, is demonstrated on the archived event stream.

Gate 1 does **not** require pinned §6 governance parameters (those are a Gate-2 input) nor any strategy-level LCB certification (that is Gate 3). Gate 1 certifies that the kernel reconciles exactly; it does not certify economics.

### Gate 2 — Cost-model closure

The pinned profile is production-supported only when:

* the **§6 governance parameters** (`b_1w`, `R` with FX component fixed at zero per §6, `cash_buffer`, `B_model`, `B_ops`, strategy-level LCB confidence-margin calibration, churn-budget window and cap, per-decision re-submit cap, tolerated divergence band, reconciliation-grace window) are **pinned** under documented governance decision — a Gate-2 verdict against exploratory placeholders is not admissible,
* every fee dimension *that the pinned production profile actually touches* (per the narrowed scope in §12) has at least one observed live exemplar,
* modelled fees reproduce posted broker charges within governance tolerance across the ticket-size distribution the live profile actually generates,
* realised slippage tracks the conservative spread/liquidity model within tolerance at that ticket-size distribution,
* realised fill rate at the pinned limit-price construction is within the conservative bootstrap fill model's expected range (§13); systematic deviation triggers a re-calibration of the fill model in its **telemetry/stress role** (not its admission role — `p_fill` is not an admission input in v1 per §6/§13) before Gate 2 can close. A miscalibrated bootstrap would propagate into the Gate-3 fill-probability stress overlay (§15, §17 Gate 3) and invalidate the stress discipline there, which is why fill-rate tracking is still a Gate-2 closure item,
* no fee or fill-rate assumptions are being "forgiven" in reconciliation.

Exemplars for lifecycle states excluded by §5 and §12 (modified, overnight-persisting) are not required and their absence does not block this gate. Route-comparison evidence against alternative candidate routes is explicitly **not** a Gate-2 input in v1 (§12, §18).

### Gate 3 — Strategy-level alpha evidence

Live rotation (beyond Tier 0 plumbing trades) is allowed only when:

* **Evidence source.** Out-of-sample strategy-level LCB evidence is produced primarily by **off-platform shadow evaluation** over the research universe, applying the canonical simulator (§15) with **all** §6 governance parameters pinned, the pinned production fee model, the ticket-size-native liquidity gate, fill-conditional admission per §6 with the bootstrap (or exemplar-calibrated) fill model from §13 operating in telemetry/stress roles only, and the pinned re-submit budget. Scenario overlay runs (§15) are not admissible evidence for this gate, with one explicit exception: the **fill-probability stress overlay** is a mandatory pass requirement for Gate 3 (§6 strategy-level LCB certification; §15 mandatory stress tests).
* **Rationale.** At Tier 0 the live universe intentionally suppresses decision frequency (2–4 ISINs, 1 funded position, threshold-first low-turnover rebalancing), so live trades alone cannot carry the statistical burden of establishing any LCB in a reasonable horizon. Shadow decisions at the **strategy/decision-category level** carry that burden; live trades verify path consistency. Per-trade LCB is explicitly out of scope (§6, §18).
* out-of-sample shadow decisions produce positive strategy-level LCB excess benefit over do-nothing after full modelled costs (`LCB[Δα_vs_do_nothing | fill, strategy, horizon] > Ĉ_RT + B_model + B_ops`),
* the evaluation horizon is long enough to be credible against the strategy's natural decision cadence,
* the result survives the mandatory fill-probability stress overlay (§15); a Gate-3 pass that fails under tightened `p_fill` is not a pass,
* no single route/session/regime assumption is carrying the result,
* **ordering constraint.** Gate 3 may not be claimed passed until Gate 2 has closed, because a strategy-level LCB computed against an uncalibrated cost stack has no meaning.

### Gate 4 — Capital sufficiency

Position count `k` may be raised only when:

* `AUM_min(k, r, t)` is met for the pinned route,
* the cash buffer covers expected tax postings and the fee-misclassification reserve (FX component fixed at zero under v1 EUR-only scope per §6),
* observed live ticket sizes are consistent with `N_min(r, t)`.

### Scope-expansion rule

Adding a second live route or session is permitted only after **all four gates** have passed for the first profile and the new profile has independently cleared its own Gate 1 and Gate 2. Introducing a second route for the purpose of **comparing routes** is a v2 activity (§18), not a v1 scope-expansion pathway.

---

## 18) v2 scaffold

v2 is a future direction and requires **full-success v1** — all four acceptance gates (§17) closed — as a precondition. v2 is **not** reachable from a narrow-success v1 terminal state (§1 "Narrow-success state") directly: the continuation programme that closes Gates 2–4 under capital scale-up must complete first. The continuation programme itself is v1-scoped work (same kernel, same gates, more capital and more exemplars), not v2 work.

v2 may add:

* event-aware overlays
* portfolio-reactive state machines
* intraday risk/event handling
* wider ETF universe
* alternate execution profiles
* event-driven simulation
* NLP/LLM or holdings-propagated signals

**v2 also takes on the items explicitly deferred from v1 (§2 "Explicitly deferred to v2"):**

* **endogenous route-comparison science** — statistically powered comparison of realized all-in cost across candidate routes (Xetra directed vs SmartRouting Fixed and any further candidates), enabled by the larger position count and richer exemplar flow available at v2 scope,
* **forward-dated settlement regime entries and cutover testing** — including the EU T+1 cutover on 11 October 2027, with transition test cases for orders straddling the cutover and explicit in-flight behaviour,
* **per-trade LCB gating** — a per-trade confidence-bounded admission rule, enabled only when the position count and decision frequency support a credible per-trade calibration sample; v2 per-trade LCB is additive to, not a replacement for, the strategy-level LCB certification,
* **two-mode (or multi-configuration) simulator** — if and only if v2 scope creates a genuine structural gap between research and production configurations that a single canonical simulator cannot bridge; introducing multiple simulator modes is a deliberate governance event, not a default.

v2 may not weaken any of the following v1 disciplines:

* cash-account admissibility
* versioned reference data
* effective-dated tax state and two-ledger tax reconciliation, with evidence-registry provenance for every dated entry
* deterministic replay under the canonical identity model (economic-state invariance)
* broker-first reconciliation under the Green/Amber/Red data-state model
* **strategy-level LCB certification** as a precondition for live rotation (per-trade LCB, if introduced in v2, is additive)
* **fill-probability discipline**: the v1 rule that `p_fill` does **not** enter the per-trade admission scalar may be relaxed in v2 only via a governance decision conditional on a calibrated exemplar-based fill model (§13); if v2 promotes `p_fill` into admission, the mandatory fill-probability stress overlay (§15, §17 Gate 3) must still survive — joint-outcome admission in v2 is additive to, not a replacement for, fill-conditional LCB certification under stress
* tradable-line-centric compliance (fund + listing venue + currency + broker contract + date), not ISIN-only compliance
* fee-closure before any new profile goes production
* settlement regime-id abstraction with regime-bounded postings (v2 extends this to forward-dated entries; v2 may not remove the abstraction)