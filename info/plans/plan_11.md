# Implementation plan — v1 Germany-tax-aware UCITS ETF execution, accounting, and allocation-research kernel

## 0) Plan conventions

This plan is derived directly from `idea_11.md` and is to be executed by a team of experienced engineers. To make execution and acceptance unambiguous, every item below uses the same shape:

- **Do** — one-sentence intent.
- **In scope / Out of scope** — explicit boundary, used to refuse scope creep.
- **How** — concrete deliverables: code, schemas, configuration, runbooks, evidence artifacts.
- **Depends on** — predecessor items that must be done (or skeleton-done) before this item is meaningful. Items with no dependency are foundational.
- **Phase** — `Phase 0` (pre-screen), `Phase A` (build/offline validation), `Phase B` (paper), `Phase C` (Tier-0 micro-live), `Phase D` (controlled scale-up). An item may be required at the start of its phase or available throughout.
- **Acceptance** — testable conditions that close the item. Capability statements ("the system can do X") are insufficient; acceptance is stated as observable, repeatable tests against fixtures, replay corpora, or signed artifacts.
- **Gate role** — which acceptance gate (§17) this item supplies evidence to (Gate 1 / 2 / 3 / 4) or whether it is infrastructure with no direct gate role.

The plan adds one workstream (B, foundations) and one workstream split (broker ingestion separated from the kernel) compared to the original draft. It also decomposes four conflated items (former U17, U23, U29, U36) and adds items the idea requires but the original plan elided (alpha protocol, tier engine, source precedence, truth-hierarchy logic, Ĉ_RT assembler, retrospective issuer changes, shadow-route paper rig, gate evidence-pack production, narrow-success continuation programme). Workstream and item IDs are renumbered to remain stable across the plan.

The §14 sequencing rule — "the append-only journal, the execution-event ledger, `broker_perm_id`-anchored identity, and correction-safe replay are built and validated **first**; the evidence registry and dated-reference-data machinery are built alongside, at the minimum scope the live universe actually requires" — is enforced by the dependency graph below, not by prose.

---

## 1) Workstream and dependency overview

| Workstream | Purpose | Drives |
|---|---|---|
| **A** Scope, governance, economics stop-loss | Lock the v1 profile, run the Phase-0 stop-loss, register all governance parameters and approvals | Phase 0; gates 1–4 governance inputs |
| **B** Foundations and cross-cutting | Configuration, secrets, time authority, schemas, observability, test harness, signed-artifact mechanism | Every other workstream |
| **C** Broker integration and source ingestion | TWS/IB Gateway client, Flex Query polling, immutable raw archive, idempotent ingest, truth-hierarchy projector | Kernel, cash, fee, tax workstreams |
| **D** Core kernel — journal, identity, replay, reconciliation | The append-only journal, immutable event ledger, canonical and fallback identity, deterministic replay, residual engine, Green/Amber/Red controller | Gate 1 |
| **E** Cash, settlement, and live admission | Cash-state decomposition, reserved-cash control, settlement-regime engine, broker-first admission engine | Gate 1; admission for Gates 2–4 |
| **F** Reference data, evidence registry, universe, compliance | Effective-dated reference data, standalone evidence registry, as-of binder, research/live universe, tradable-line compliance | Gate 1; data provenance for Gates 2–3 |
| **G** German tax — ledgers, precedence, reconciliation, close | Statutory Ledger A, broker-cash Ledger B, precedence resolver, exception workflow, annual-close checklist, retrospective-change reopen | Gate 1; cash-buffer sizing for Gate 4 |
| **H** Economic admission, fees, liquidity, execution policy | Pinned-route fee model, exemplar capture, capital schedule, ticket-size-native liquidity gate, deterministic limit-price construction, fill telemetry, re-submit budget, cost-stack `Ĉ_RT` assembler, per-trade admission engine | Gate 2; per-trade admission for Gates 3–4 |
| **I** Alpha, simulator, paper/micro-live, gate evidence | Alpha protocol (threshold-first rebalancing), canonical simulator, scenario overlays and mandatory stress suite, off-platform shadow-evaluation rig, Gate-3 strategy-level LCB certification, paper harness, Tier-0 runbook, gate evidence packs, narrow-success continuation programme, controlled scale-up | Gates 3 and 4 |

Critical-path summary (predecessors required before any live trade is admitted, in order):

`A → B → C → D → E → F (minimum scope) → G (minimum scope) → H (admission engine) → I (paper, Tier-0 runbook, Gate-1 evidence)`.

Gate-2 evidence requires H exemplar capture under live load. Gate-3 evidence requires I shadow-evaluation rig with all §6 governance parameters pinned. Gate-4 evidence requires E/H/G capacity outputs against pinned `cash_buffer`.

---

## 2) Workstream A — scope, governance, and economics stop-loss

This workstream locks the v1 profile, registers governance parameters, runs the Phase-0 economic stop-loss, and operationalizes the gate, tier, exception, waiver, and re-pinning workflows. Workstream A artifacts are inputs to every other workstream; they are produced first but maintained throughout v1.

### U01 — Encode the v1 scope and pinned production profile

**Do:** Encode the signed v1 scope and exactly one production execution profile from §5 as governed configuration enforced by the runtime.
**In scope:** IBKR cash account; EUR-funded; long-only UCITS ETF; SmartRouting Fixed; continuous trading 09:00–17:30 CET; DAY-limit only; broker contract metadata captured. Out-of-scope list (extended retail, auctions, MOC/MOO/IOC, market orders, modify-as-normal, overnight persistence, multiple live routes/sessions) encoded as explicit denial rules.
**Out of scope:** Any route comparison, second live route, or session expansion. Those are the §17 scope-expansion rule and §18 v2 territory.
**How:** A versioned profile descriptor with a signed pinning-rationale document referenced in the evidence registry; a runtime gate that rejects every order whose route, session, order class, currency line, or lifecycle state does not match the pinned profile descriptor; shadow profiles encoded as research-only reference rows that cannot be promoted without an §17 governance step.
**Depends on:** B-stream (config, signed-artifact, evidence registry skeleton).
**Phase:** Phase 0 / Phase A.
**Acceptance:** A runtime test corpus of 100+ deliberately-malformed order intents (wrong route, wrong session, wrong order type, wrong currency line, modified, overnight) is rejected 100%; profile-descriptor versioning is end-to-end traceable from runtime decision to evidence registry record; promotion of any shadow profile is impossible except through a recorded §17 governance event.
**Gate role:** Gate 1 (operational correctness).

### U02 — External-assumption registry (effective-dated, dated reference constants)

**Do:** Register every external assumption from §3 (broker policy, market microstructure, German fund-tax law, retail eligibility, broker reporting cadence) as effective-dated, evidence-backed reference data rather than as code constants.
**In scope:** IBKR cash-account buying-power policy; settlement T+N values per regime; Xetra core hours; Extended Xetra Retail Service definition (recorded but disabled in v1); IBKR pricing-plan API restrictions; modified/overnight-as-new-order rule; XLM standardized reference size; InvStG paragraph references; PRIIPs KID effective date; Flex Query refresh cadences.
**Out of scope:** Forward-dated regimes including the EU T+1 cutover on 11 October 2027 — recorded as v2 artifacts only (§11, §18).
**How:** A typed reference-data table with `effective_from`, `effective_to`, source URI, retrieval timestamp, document hash, parser version, and reviewer identity per row, indexed by an external-assumption key namespace; runtime accessors take a `(key, as_of)` tuple, never a literal constant.
**Depends on:** B-stream (config, evidence registry skeleton, schemas).
**Phase:** Phase A.
**Acceptance:** Grepping the codebase for any of the §3 numeric or textual constants returns zero hits outside the registry loader; every accessor enforces `as_of` lookup; a synthetic test that mutates an effective-dated row and re-runs an admission decision against the same `as_of` produces the original decision unchanged (immutability of historical lookup).
**Gate role:** Gate 1 (provenance), Gate 2 (fee-policy effective dating), Gate 3 (simulator dated inputs).

### U03 — Phase 0 economic-feasibility pre-screen

**Do:** Run the §16 Phase-0 stop-loss before Phase A engineering is industrialized.
**In scope:** Implied one-way and round-trip floors at EUR 1,500 / EUR 3,000 / EUR 5,000 ticket sizes for the pinned profile; theoretical minimum strategy-level LCB excess benefit needed to clear `Ĉ_RT + B_model + B_ops` under exploratory placeholders; the four pre-frozen items from §16 (research protocol, OOS window, required net effect size, pre-registered decision rule).
**Out of scope:** The pre-screen does not gate anything by itself — it is a stop-loss on project effort. Re-running against the same data with a relaxed threshold is forbidden.
**How:** A reproducible pre-screen pack consisting of (i) frozen protocol document, (ii) frozen OOS window definition, (iii) numeric required-effect-size threshold, (iv) signed pre-registered pass/fail rule — all four written and timestamped in the evidence registry **before** the OOS window is evaluated; a one-shot evaluation script that reads only the frozen inputs and produces a pass/fail verdict; a verdict record signed and timestamped under the evidence registry; a re-scope branch documented for the fail path (raise capital, narrow to single buy-and-hold, close v1).
**Depends on:** U02 (external assumptions for cost floor inputs), B-stream (evidence registry, signed-artifact mechanism).
**Phase:** Phase 0.
**Acceptance:** All four §16 frozen items exist in the evidence registry with hashes and timestamps that precede the OOS evaluation; the pass/fail verdict is reproducible from frozen inputs; on a synthetic fail, the project state machine refuses to advance to Phase A engineering without a governance waiver.
**Gate role:** Provides the cost-stack lower bound that Gate 3 LCB hurdle may not loosen below.

### U04 — Governance-parameter register

**Do:** Create effective-dated governed storage for every §6 governance parameter, with explicit pinned-vs-placeholder status per row.
**In scope:** `b_1w`; `R(r,t)` itemized per route with FX component fixed at zero for v1 EUR-only scope; `cash_buffer(t)` decomposition (working-cash floor + tax-posting upper bound); `B_model`; `B_ops`; strategy-level LCB confidence-margin calibration procedure (sampling assumption, calibration window, refresh cadence, Gate-3 horizon); churn-budget window and cap; per-decision re-submit cap; per-channel tolerated divergence band and reconciliation-grace window.
**Out of scope:** Per-trade LCB calibration parameters — those are v2 (§2, §18).
**How:** A typed table keyed by `(parameter_key, route, as_of)` with `pinned: bool`, `placeholder_value`, `pinned_value`, `pinning_decision_id`, signature, and reviewer; runtime parameter access goes through a single resolver that returns the pinned value when present and the placeholder otherwise, and tags every consuming computation with the placeholder/pinned status of each parameter it consumed.
**Depends on:** B-stream (signed-artifact mechanism, evidence registry).
**Phase:** Phase A (placeholders); Phase B–C (pinning events as evidence accumulates).
**Acceptance:** No Gate-2 cost-closure claim and no Gate-3 LCB claim can be produced from any computation tagged as having consumed a placeholder; pinning events are recorded as evidence-registry rows; transition from placeholder to pinned is a versioned event, not a mutation.
**Gate role:** Inputs to Gate 2 closure and Gate 3 LCB certification.

### U05 — Gate, tier, waiver, exception, and re-pinning workflow

**Do:** Operationalize the four §17 sequential gates, the §6 tier transitions (Tier 0/1/2), the narrow-success terminal state, the signed-exception mechanism (including Phase-0 waiver and emergency overrides), and the §5 re-pinning procedure for the production route.
**In scope:** Gate-claim objects; tier-transition approval objects; waiver/exception records; route re-pinning procedure that re-opens Gate 2 for the new profile; scope-expansion rule (a second live route or session is permitted only after all four gates pass for the first profile).
**Out of scope:** v2 multi-profile Gate-2 comparisons; per-trade LCB gating workflows.
**How:** Workflow objects with predecessor checks (Gate N+1 cannot be claimed while Gate N is open); state-machine enforcement that ties tier transitions to (a) `AUM_min` evidence and (b) the relevant gate having closed; waiver/exception records that carry a reviewer signature, justification, and an explicit expiry date; an audit log readable from the evidence registry.
**Depends on:** U01, U04, B-stream.
**Phase:** Phase A onward.
**Acceptance:** A red-team attempt to claim Gate 3 with Gate 2 not closed is refused at the workflow layer; a Tier-1 promotion attempt without `AUM_min(1, r, t)` evidence is refused; every waiver expires automatically; route re-pinning is a single signed event that immediately re-opens Gate 2 for the new profile.
**Gate role:** All four gates.

---

## 3) Workstream B — foundations and cross-cutting concerns

This workstream provides the substrate the rest of the system stands on. It is small, but every item below is a hard predecessor of items in C–I.

### U06 — Configuration, secrets, and environment topology

**Do:** Provide one configuration model for the runtime, a secrets store for IBKR API credentials and Flex tokens, and a clearly separated environment topology (paper, micro-live, replay-only).
**In scope:** Per-environment configuration loading; secrets isolation; explicit environment tag on every persisted record; immutable per-environment archive partitioning.
**Out of scope:** Multi-tenant or multi-account topologies — v1 is one account.
**How:** Typed configuration schema with environment tag; secret retrieval through a single broker-API factory; persistence keyed by environment so paper and live archives never collide.
**Depends on:** None.
**Phase:** Phase A.
**Acceptance:** A live runtime cannot start without a present, valid IBKR credential bundle; a paper runtime cannot write to the live archive partition (and vice versa); environment tag is non-null on every persisted row.
**Gate role:** Infrastructure (Gate 1 prerequisite).

### U07 — Schemas and identity namespace

**Do:** Define the persisted schemas for all economic, execution, reference, evidence, and governance records, and the canonical identity namespace from §10.
**In scope:** Journal posting schema (with account codes); execution-event ledger schema; identity types `strategy_intent_id`, `admission_decision_id`, `api_order_id`, `broker_perm_id`, `parent_perm_id`, `exec_id`, `exec_correction_group_id`, `commission_report_id`, `statement_posting_id`, plus `fallback_identity` composite; evidence-registry record schema; reference-data row shape.
**Out of scope:** v2 schema extensions for forward-dated regime entries, per-trade LCB calibration, multi-route comparison.
**How:** Strongly-typed schema definitions with explicit forward/backward compatibility rules; an identity-types module that provides constructors and validators (forbids constructing a `broker_perm_id` of value 0 except via the §10 fallback path).
**Depends on:** U06.
**Phase:** Phase A.
**Acceptance:** Schema round-trip tests pass on representative records; identity validators refuse malformed IDs; schema-evolution test confirms historical records remain readable after a non-breaking change.
**Gate role:** Infrastructure (Gate 1 prerequisite).

### U08 — Time authority, calendar, fiscal-year and effective-date arithmetic

**Do:** Provide one time/clock authority for the system covering UTC, CET/CEST, the Xetra trading calendar, IBKR settlement business-day calendar, and the German fiscal year.
**In scope:** Single source of "now" for all admission and posting decisions; calendar lookup for trading days, settlement days, and fiscal-year boundaries; explicit `as_of` propagation through every admission and posting path.
**Out of scope:** Forward-dated regime cutover support (v2).
**How:** A clock provider that is the only legal source of `now`, with a deterministic test override for replay; calendar tables loaded from U02 reference data; an `as_of` parameter required on every reference-data and admission call (no implicit "now" fallback).
**Depends on:** U02.
**Phase:** Phase A.
**Acceptance:** No code path may bypass the clock provider (enforced by static check); replay against a frozen `as_of` produces identical decisions across runs; Xetra calendar correctly identifies non-trading days for the v1 horizon.
**Gate role:** Infrastructure (Gate 1 replay determinism).

### U09 — Signed-artifact mechanism, evidence-registry primitives, and audit log

**Do:** Provide the signature, hashing, and audit primitives used by the evidence registry, governance parameters, gate claims, exceptions, waivers, and re-pinning events.
**In scope:** Document hashing algorithm and storage (content-addressed); signature scheme used for governance approvals and exceptions; tamper-evident audit log of every governance event; reviewer-identity registry.
**Out of scope:** External public-key infrastructure or third-party trust anchors — v1 is single-operator and the trust model is internal.
**How:** A content-addressed evidence store; a signing service that binds (artifact hash, reviewer identity, timestamp, decision text); an append-only audit log keyed off the evidence store. The evidence-registry table from F-stream consumes these primitives.
**Depends on:** U06, U07.
**Phase:** Phase A.
**Acceptance:** Tampering with a stored evidence document changes its hash and breaks every reference to it; a signature can be re-verified offline from the artifact and reviewer registry; the audit log is append-only by storage policy, not by convention.
**Gate role:** Infrastructure (every gate requires signed evidence).

### U10 — Observability, structured logging, metrics, alerts

**Do:** Provide the structured logging, metrics, and alerting layer used by every other item.
**In scope:** Structured logging keyed by canonical identity (strategy intent, admission decision, broker perm, correction group); metrics for admission throughput, reject reasons, divergence-state transitions, reconciliation residuals, fallback-identity volume; alert rules for Red-state transitions, fallback-identity spikes, re-submit-budget-window breaches, reconciliation residual breaks, evidence-staleness on a live tradable line.
**Out of scope:** Trade-performance dashboards — v1 is not a strategy-monitoring product.
**How:** Standard structured-log emitter; metrics collected per canonical identity; alert rules expressed as code, not as runtime configuration.
**Depends on:** U06, U07.
**Phase:** Phase A; alert rules sharpened in B/C.
**Acceptance:** Every admission decision and every reconciliation residual is queryable by canonical identity within seconds; alert rules fire under fixture replays of (Red-state, fallback-spike, residual break, stale-evidence) scenarios; log structure is stable enough for post-hoc forensic replay.
**Gate role:** Infrastructure (Gate 1 reconciliation evidence, Gate 2 exemplar discovery).

### U11 — Test harness, fixtures, replay corpus, and synthetic-broker simulator

**Do:** Provide the unit, integration, replay, and end-to-end testing harness, plus a synthetic-broker simulator used for replay-correctness tests independent of live broker traffic.
**In scope:** Unit-test framework; fixture fabrication for broker callbacks (acks, fills, partials, cancels, rejects, expiries, corrections, restatements); replay corpus management (versioned archives of broker events for replay determinism testing); a synthetic-broker harness that emits realistic IBKR callback sequences including correction groups and `permId=0` edge cases.
**Out of scope:** Property-based testing of strategy alpha — that is research-side and lives in I-stream.
**How:** A fixture library with named scenarios (clean fill, partial sequence, cancel-after-partial, broker-side correction, restatement, manual non-API trade, reject); the synthetic-broker simulator emits the same callback shapes the live IBKR adapter consumes; a replay-corpus manifest with hashes, used by U17 (replay engine) acceptance.
**Depends on:** U07, U09.
**Phase:** Phase A.
**Acceptance:** Every fixture deterministically reproduces; the synthetic-broker simulator's callback shapes are byte-compatible with the live adapter's input model; replay-corpus manifest hashes match across rebuilds.
**Gate role:** Infrastructure (Gate 1 replay determinism evidence).

---

## 4) Workstream C — broker integration and source ingestion

This workstream owns every byte that crosses the system boundary from IBKR. It is separated from the kernel because (a) the kernel is supposed to be source-agnostic given canonical identity, and (b) the integration layer has its own concurrency, idempotency, and rate-limit discipline that does not belong inside the journal.

### U12 — TWS / IB Gateway client adapter

**Do:** Provide a thin, idempotent adapter to TWS / IB Gateway as the intraday execution-truth source.
**In scope:** Connection lifecycle; subscription to order, execution, and account-summary callbacks; capture of `permId`, `orderId`, `execId`, commission-report fields, and account-cash fields; mapping to internal identity namespace per U07; idempotent handling of repeated callbacks.
**Out of scope:** TWS scripting; market-data subscriptions for non-pinned routes; extended-session subscriptions.
**How:** A typed callback handler that emits canonical events into the C-stream raw archive (U15) and projects normalized events for the kernel (U16); explicit handling of `permId=0` non-API origin callbacks; explicit handling of `execDetails` correction callbacks (same `permId`, same `parentExecId`, new `execId`).
**Depends on:** U06, U07, U08, U10.
**Phase:** Phase A (paper); Phase B (paper validated); Phase C (live).
**Acceptance:** A reconnection mid-session does not produce duplicate journal effects (idempotency proven by fixture replay); a synthetic correction callback sequence is captured and grouped under `exec_correction_group_id` automatically; non-API callbacks are flagged `origin="non_api"` automatically per §10.
**Gate role:** Gate 1 (intraday truth ingestion).

### U13 — Flex Query poller (Trade Confirmation Flex and Activity Flex)

**Do:** Poll IBKR Flex Queries on their documented cadences and project them into the raw archive and the truth-hierarchy projector.
**In scope:** Trade Confirmation Flex with the 5–10 minute delay window; Activity Flex daily at close; per-Flex-channel cadence configuration; idempotent ingest with content-addressed deduplication; explicit retrieval-timestamp and parser-version annotation.
**Out of scope:** Custom Flex schemas not in the v1 production profile; Flex history beyond the v1 retention requirement.
**How:** A scheduled poller per Flex type; each pull is hashed and deduplicated; every parsed record carries source channel, retrieval timestamp, parser version, and the immutable raw-archive pointer; the poller is rate-limit-aware and respects IBKR's documented limits.
**Depends on:** U06, U08, U10, U15.
**Phase:** Phase A.
**Acceptance:** Re-polling the same Flex window produces zero new persisted records (dedup proven); a forced re-parse with a new parser version produces a new normalized projection without mutating the raw archive; the poller stops cleanly on rate-limit signals and resumes.
**Gate role:** Gate 1 (delayed and end-of-day truth ingestion).

### U14 — Truth-hierarchy projector (TWS → Trade Confirm Flex → Activity Flex → corrections)

**Do:** Project the §10 truth hierarchy into a single consumable projection used by reconciliation and the Green/Amber/Red controller.
**In scope:** Per-channel projection that respects the §10 ranking (TWS as intraday execution truth; Trade Confirm Flex as delayed execution confirmation; Activity Flex as end-of-day economic-reconciliation truth; later broker corrections as explicit replay exceptions handled via correction groups); per-channel divergence detection input (consumed by U22).
**Out of scope:** Cross-channel automated reconciliation — that is U21 (residual engine).
**How:** A projection that, for any `(broker_perm_id, exec_correction_group_id)`, exposes the most authoritative known state per channel and the divergence between channels; explicit annotation of which channel is authoritative for which field per §10.
**Depends on:** U12, U13, U15.
**Phase:** Phase A.
**Acceptance:** A scenario where TWS and Trade Confirm Flex disagree transiently produces an Amber state without mutating either source; later Activity Flex closes the divergence; a broker correction supersedes prior records via correction group, never by mutation.
**Gate role:** Gate 1 (truth hierarchy operationalized).

### U15 — Raw broker archive (immutable)

**Do:** Persist every byte received from IBKR (callback payloads and Flex pulls) into an immutable, content-addressed archive.
**In scope:** Raw payload storage; retrieval timestamp; channel and parser-version annotation; environment-partitioned storage; retention policy.
**Out of scope:** Mutating or compacting historical raw bytes.
**How:** Content-addressed object storage; one row per received payload; archive-pointer references in normalized projections; periodic integrity checks comparing stored hashes to recomputed hashes.
**Depends on:** U06, U09.
**Phase:** Phase A.
**Acceptance:** A test that mutates archived bytes is detected by integrity check; every normalized record can be re-derived from the archive plus a parser version; retention policy is enforced by storage rules, not application code.
**Gate role:** Gate 1 (replay determinism, governance auditability).

---

## 5) Workstream D — core kernel: journal, event ledger, identity, replay, reconciliation

This is the kernel. It is the first thing built that produces business value, and the rest of the system depends on it. Workstream D items are §14-sequenced ahead of evidence-registry richness in F-stream.

### U16 — Append-only canonical accounting journal

**Do:** Build the sole economic source of truth.
**In scope:** Immutable postings for fills, commissions, exchange/clearing/regulatory fees, tax cashflows (both Ledger A statutory and Ledger B broker-cash post here under distinct account codes), cash movements, FX, distributions, settlement releases, broker corrections, year-end tax adjustments. Account-code chart that distinguishes statutory vs broker-posted tax under §9.
**Out of scope:** Any economic state stored outside the journal. Reserved cash is an operational view (§11), not a journal entity.
**How:** Append-only persistence; every posting carries its canonical-identity references; every posting is signed by a posting source (kernel, reconciliation, manual exception); a chart-of-accounts module consumed by every poster.
**Depends on:** U07, U09.
**Phase:** Phase A.
**Acceptance:** A red-team attempt to mutate or delete an existing posting is refused at the storage layer; every posting traces back to either a broker event, a settlement release, a reconciliation correction, or a manual signed exception; account codes are exhaustive against the §10 enumeration.
**Gate role:** Gate 1.

### U17 — Immutable execution-event ledger

**Do:** Build the causal ledger from strategy intent to broker outcome.
**In scope:** Immutable records for strategy intent, admission-decision input snapshot, order submission, broker ack, partial fill, full fill, cancel, reject, expiry, correction/restatement; linkage to journal postings where applicable; record of the bootstrap `p_fill` telemetry on each admission decision per §6/§13 (recorded but not used as admission input).
**Out of scope:** Per-trade LCB calibration data — v2.
**How:** Append-only persistence keyed by canonical identity per U07; structured to support efficient query by intent, decision, perm, correction group; admission-decision records snapshot all governance-parameter values consumed at decision time, with placeholder/pinned tag.
**Depends on:** U07, U09, U16.
**Phase:** Phase A.
**Acceptance:** Every live or paper action is reconstructable end-to-end from intent through admission, submission, broker outcome, correction (if any), and journal posting; bootstrap `p_fill` is queryable per admission decision; admission decisions made under placeholder governance parameters are queryable as such.
**Gate role:** Gate 1.

### U18 — Canonical identity and correction-group semantics

**Do:** Implement canonical identity rules so that downstream state keys off canonical identity and prior history is never mutated.
**In scope:** `broker_perm_id` as canonical order identity; `exec_correction_group_id` as supersession grouping; identity coalescing with §10 fallback identity for `permId=0`; explicit refusal to bind unrelated events to the same identity by mistake.
**Out of scope:** Cross-account identity (v1 is one account).
**How:** A canonical-identity resolver that, given any inbound event, produces the canonical key and the correction-group key; supersession semantics where a correction does not mutate but creates a new event in-group; downstream state always reads the latest in-group event.
**Depends on:** U07, U17.
**Phase:** Phase A.
**Acceptance:** A fixture sequence (initial fill → broker correction → re-correction) produces three in-group events and a single canonical economic state matching the latest correction; an attempt to mutate an in-group event is refused.
**Gate role:** Gate 1.

### U19 — Fallback identity for non-API-originated activity

**Do:** Implement the §10 fallback composite identity used only when `broker_perm_id` is 0 or absent.
**In scope:** Composite key `(account_id, trade_date, contract_id, side, cumulative_qty, avg_fill_price, statement_posting_id)`; `origin="non_api"` flag on every fallback-identity event; linkage to a governance-exception record (manual exception) or broker posting (broker-side adjustment); fallback corrections grouped via `statement_posting_id` when no `broker_perm_id` is available across the correction.
**Out of scope:** Promoting fallback identity to a competing canonical key when `broker_perm_id` exists — fallback is only used when canonical is absent.
**How:** Fallback resolver invoked by U18 only when the canonical key resolves to 0/absent; mandatory linkage to either an exception record (U05) or a broker posting (U13/U15); fallback-identity volume metric emitted to U10 with an alerting threshold for spikes.
**Depends on:** U05, U18, U10.
**Phase:** Phase A.
**Acceptance:** A scenario where a manual non-API trade is entered, then later corrected by the broker, produces a coherent in-group fallback supersession sequence; a spike in fallback volume triggers the U10 alert; fallback events without a linked exception or broker posting are refused at the resolver.
**Gate role:** Gate 1.

### U20 — Deterministic replay engine (economic-state invariance)

**Do:** Replay the archived event stream and reference-data snapshots to reproduce identical economic state under canonical ordering.
**In scope:** Canonical ordering keyed by `(coalesce(broker_perm_id, fallback_identity), exec_correction_group_id)`; reproduction of position, settled cash, reserved cash, fee, and tax state; tolerance for byte-non-identity in journal records that does not affect economic state (per §10 Rule 4).
**Out of scope:** Byte-identical replay (explicitly forbidden by §10 Rule 4 because it would be brittle under API-order-ID remapping and rebinding).
**How:** A replay driver that consumes the U15 raw archive, the U17 event ledger, and the F-stream as-of reference snapshots; produces an in-memory economic state and compares to the persisted journal state on the invariant set; uses U11 fixtures and the synthetic-broker harness to construct test cases including correction supersessions and fallback-identity events.
**Depends on:** U11, U15, U16, U17, U18, U19, U28 (as-of binder).
**Phase:** Phase A.
**Acceptance:** Replay over the U11 corpus reproduces invariant state across 100% of fixtures; replay across a fixture with three correction supersessions and one fallback-identity event reproduces invariant state; any divergence in invariant state is reported as a replay failure with the divergent fields named.
**Gate role:** Gate 1 (deterministic replay is the headline Gate-1 requirement).

### U21 — Derived reconciliation views and residual engine

**Do:** Produce reconciliation views and surface residuals deterministically per channel.
**In scope:** Reconciliation views for positions, settled cash, fees, broker-posted tax cashflows; per-channel residual computation (journal-vs-broker); explicit explained-vs-unexplained delta classification; propagation of correction supersessions into reconciled state; output to U10 metrics and to U22 controller.
**Out of scope:** Automated correction of residuals — those are journalled as new postings or escalated to exceptions, never silently absorbed.
**How:** Deterministic SQL/views or equivalent computation; a residual engine that classifies deltas as (explained-by-correction, explained-by-known-timing-difference, explained-by-bridge-adjustment, unexplained); unexplained residuals trigger Red-state escalation per U22.
**Depends on:** U14, U16, U17, U20.
**Phase:** Phase A.
**Acceptance:** A fixture with a known timing difference (Trade Confirm Flex behind TWS) produces an explained delta that closes within the grace window; a fixture with an unexplained residual produces a Red state and a U10 alert; reconciliation views are queryable by `as_of` for historical investigation.
**Gate role:** Gate 1 (no unexplained residuals).

### U22 — Green / Amber / Red divergence controller

**Do:** Implement the §4 Rule 6 data-state model so that stale and transiently divergent data are handled, not frozen.
**In scope:** Per-channel state machine over (broker cash, broker positions, fees, tax postings); pinned per-channel divergence band and grace window from U04; degraded-but-safe Amber rules (use the more conservative value, log the divergence, schedule re-check); Red transitions that freeze admission on the affected channel; transition events emitted to U10 and the audit log.
**Out of scope:** Per-trade divergence checks — divergence is per-channel, not per-order.
**How:** A controller that consumes U21 residuals and U14 truth projections, applies per-channel band/window thresholds from U04, and exposes per-channel state to U25 (admission engine) so that admission can read the operative cash value per channel state.
**Depends on:** U04, U10, U14, U21.
**Phase:** Phase A.
**Acceptance:** A fixture sequence (Green → Amber by Flex lag → Green after refresh) is reproduced by the controller without freezing admission; a Red transition freezes admission on the affected channel and only on the affected channel; transitions are logged and queryable by `as_of`.
**Gate role:** Gate 1.

---

## 6) Workstream E — cash, settlement, and live admission

This workstream makes live admission broker-first and cash-account-correct under the divergence model.

### U23 — Cash-state decomposition engine

**Do:** Maintain the §11 four-way cash decomposition (trade-date, settled, reserved, withdrawable) as derived state.
**In scope:** Derived-state computation from journal postings, settlement-regime forecasting (U24), and reservation events (U24a); explicit forbiddance of category-mixing in admission and withdrawal logic.
**Out of scope:** Multi-currency decomposition — v1 is EUR-only.
**How:** Pure computation over journal state, settlement-regime forecasts, and reservations; admission and withdrawal callers must specify the category they are gating against (compile-time enforced where possible).
**Depends on:** U16, U24.
**Phase:** Phase A.
**Acceptance:** A fixture sequence with overlapping unsettled sales and pending buys produces correct settled-cash forecasts; an admission attempt against `withdrawable` (instead of `settled+reserved`) is refused at the type level.
**Gate role:** Gate 1.

### U24 — Settlement regime engine (current-regime only) and broker-settlement-policy versioning

**Do:** Implement effective-dated settlement modeling for the currently active regime only, with broker-settlement-policy assumptions versioned separately and validated against observed broker records.
**In scope:** `settlement_regime_id` abstraction; current-regime range (open, close), jurisdiction/venue scope, instrument-class scope, instrument/venue overrides, T+N rule under the applicable calendar, cash-availability rule for sale proceeds; broker-settlement-policy versioning that is reconciled against U13 Activity Flex postings to detect undeclared policy drift.
**Out of scope:** Forward-dated regime entries (including the EU T+1 cutover on 11 October 2027); transition test cases for orders straddling a cutover; in-flight unsettled-trade behaviour at regime change. All v2.
**How:** The regime-id abstraction is preserved in storage so v2 additions are additive; v1 ships exactly one current-regime row; broker-settlement-policy assumptions are versioned reference-data rows reconciled against observed broker postings, with divergences flagged via U22.
**Depends on:** U02, U08, U16.
**Phase:** Phase A.
**Acceptance:** Current-regime settlement forecasting reproduces observed broker settlement dates within tolerance over the U11 fixture corpus; an attempt to add a forward-dated regime entry in v1 is refused (storage allows the abstraction; runtime refuses to consume forward-dated rows); divergence between modelled settlement and broker-posted settlement triggers a U22 channel state transition.
**Gate role:** Gate 1.

### U24a — Reserved-cash control module

**Do:** Implement reserved cash as an operational control view that does not compete with the journal.
**In scope:** Reservation on order admission; release on terminal cancel/reject/expiry, full fill, explicit quantity reduction, or reconciliation-driven correction; reserved-cash view consumed by U23 and U25.
**Out of scope:** Reserved cash as a journal entity (forbidden by §11).
**How:** Pure derived state from execution-event ledger lifecycle events; the reservation set is always reconcilable to outstanding non-terminal admissible orders.
**Depends on:** U17, U25.
**Phase:** Phase A.
**Acceptance:** Reserved cash always equals the sum of headroom on outstanding non-terminal orders (invariant verified by replay over U11); no path mutates a reservation outside the four allowed release events.
**Gate role:** Gate 1.

### U25 — Live admission engine with broker-authority enforcement

**Do:** Enforce broker-first cash admissibility at the §11 admission gate.
**In scope:** Admission only when broker-reported settled cash and buying power (read via U12 from TWS/IB Gateway) plus local controls prove principal, commissions, fee allowance, spread reserve, tax/FX allowance where relevant, and `N_min` per U33; per-channel state from U22 governs the operative cash value (Amber → use the more conservative of broker vs forecast); hard-block when broker says cash is insufficient regardless of local forecast.
**Out of scope:** Strategy-level admission (handled by U41 per-trade admission engine); compliance admission (handled by U32 tradable-line compliance).
**How:** A pure admission function that consumes (intent, broker cash state, channel state, settlement forecast, reservation state, fee allowance, `N_min`) and returns admit/reject with structured reason; orchestration that calls in this order: profile gate → compliance gate → cash gate → economic gate → tier/churn/Gate-3 gate.
**Depends on:** U01, U12, U22, U23, U24, U24a, U33, U41.
**Phase:** Phase A.
**Acceptance:** A red-team test where the local forecast says cash is sufficient but the broker says otherwise is refused (broker authority wins); a Red-state cash channel freezes admission immediately; reject reasons are structured and queryable for governance review.
**Gate role:** Gate 1.

---

## 7) Workstream F — reference data, evidence registry, universe, compliance

This workstream makes decisions reproducible and tradable-line-centric. Per the §14 sequencing note, F-stream is built **alongside** D-stream at the minimum scope the live universe requires (2–4 ISINs, 1 funded position typical) and expanded as scope expands.

### U26 — Effective-dated reference data subsystem

**Do:** Build the dated subsystem for live profile, calendars, settlement regime, broker policy, fee schedules, eligibility state, tax classification state, Teilfreistellung regime, Basiszins, corporate-action rules.
**In scope:** Storage shape `(key, effective_from, effective_to, value, evidence_pointer)`; runtime accessors take `(key, as_of)` only.
**Out of scope:** The evidence registry itself (separate item U27); as-of binding (separate item U28).
**How:** Typed reference-data tables; access through a single resolver that enforces `as_of`; loaders that pull from external sources via Workstream C where available and from manual reviewer entry otherwise.
**Depends on:** U02, U07, U08, U09.
**Phase:** Phase A.
**Acceptance:** A reference lookup at a historical `as_of` returns the row that was effective at that timestamp, even if newer rows have superseded it; mutating a historical row is refused.
**Gate role:** Gate 1 (provenance), Gate 2 (fee dating), Gate 3 (simulator dated inputs).

### U27 — Standalone evidence registry

**Do:** Build the evidence registry as a standalone subsystem (not embedded in U26) so that Ledger A statutory tax state, compliance decisions, governance approvals, and pre-screen artifacts can all reference it independently.
**In scope:** Per §14: source URI or document identity; retrieval timestamp; document hash; parser/version identifier; extracted fields; stated validity interval; reviewer status. Backed by the U09 signed-artifact mechanism.
**Out of scope:** Public-key infrastructure beyond U09.
**How:** Content-addressed storage; per-record reviewer signature; query API consumed by U26 (reference data), U21 (statutory ledger), U32 (compliance), U05 (gates/exceptions), U03 (pre-screen).
**Depends on:** U07, U09.
**Phase:** Phase A.
**Acceptance:** Every reference-data row, every statutory ledger entry, every compliance decision, and every gate claim has at least one resolvable evidence pointer; an attempt to write a statutory ledger entry without an evidence pointer is refused (per §9 data-provenance requirement).
**Gate role:** Gate 1 (provenance), Gates 2–4 (evidence per claim).

### U28 — As-of binder for backtests and live decisions

**Do:** Make every backtest and every live decision bind, immutably, to the §14 as-of snapshot set (market-data, reference-data, instrument-evidence, session profile, fee profile, settlement regime id, identity-namespace version).
**In scope:** Snapshot identifier minted per decision/backtest; persisted with the decision/backtest output; replay reads only the bound snapshot.
**Out of scope:** Snapshot diffing for governance review (separate observability concern handled by U10).
**How:** A snapshot composer that captures all required as-of inputs at decision time; a snapshot resolver that re-reads the same snapshot for replay; integration with U17 admission-decision records and U35/U43 simulator outputs.
**Depends on:** U07, U08, U26, U27.
**Phase:** Phase A.
**Acceptance:** A live decision replayed against its bound snapshot reproduces identical inputs and identical decision; a backtest replayed against its bound snapshot reproduces identical inputs and identical decision; a snapshot cannot be mutated after binding.
**Gate role:** Gate 1 (replay), Gate 3 (simulator reproducibility).

### U29 — Source-precedence resolver for eligibility (§7)

**Do:** Implement the §7 eligibility source hierarchy as an explicit resolver so that disagreements between issuer documents, exchange/instrument metadata, broker contract metadata, BaFin database, and manual review records are resolved deterministically.
**In scope:** The §7 ordering (issuer legal docs → exchange/instrument metadata → broker contract metadata → BaFin → manual review); BaFin-as-corroborating-only (never sole authority); date-aware PRIIPs KID validity.
**Out of scope:** Tax-side precedence (separate U37, in G-stream).
**How:** A resolver function that consumes evidence pointers from U27 and applies §7 ordering; emits a structured eligibility verdict per tradable line.
**Depends on:** U26, U27.
**Phase:** Phase A.
**Acceptance:** A red-team scenario where BaFin disagrees with the issuer is resolved in favour of the issuer with a logged disagreement record; a tradable line whose PRIIPs KID is expired at `as_of` is rejected.
**Gate role:** Gate 1 (compliance correctness).

### U30 — Research / live universe manager

**Do:** Maintain a 5–10 ETF research universe and a governed 2–4 ISIN live universe, with XLM in the §7 admission and monitoring roles only.
**In scope:** Research/live lists; promotion workflow (research → live) requiring U05 approval; XLM at the nearest standardized size as an ISIN-level admission check; ongoing-XLM-deterioration manual review trigger; explicit refusal of XLM as a per-order reject.
**Out of scope:** Hard XLM-driven freezes (deferred per §2); per-order XLM gating.
**How:** Universe tables with effective dating; promotion workflow tied to U05; XLM ingestion as reference data per U26; manual-review trigger as a workflow event.
**Depends on:** U05, U26.
**Phase:** Phase A.
**Acceptance:** A live-universe addition without an approval is refused; an XLM deterioration on a live ISIN raises a manual-review event without freezing buying automatically; an attempt to use XLM as a per-order reject is refused at the API level.
**Gate role:** Gate 1.

### U31 — Tradable-line compliance engine

**Do:** Make compliance line-centric per §8, not ISIN-only.
**In scope:** Tradable line keyed by `(ISIN, listing venue, trading currency, broker contract metadata, effective date)`; per-line signed decision record with all §8 fields; fund-level evidence shared once and referenced by listing-line records to avoid duplication; freeze rules (line-level freeze on stale or contradictory evidence; fund-level staleness freezes all lines for that fund).
**Out of scope:** Multi-broker compliance (v1 is IBKR only).
**How:** Compliance records consumed by U25 admission gate; freeze rules implemented as derived state from U26 reference data and U27 evidence pointers; reviewer workflow tied to U05.
**Depends on:** U05, U26, U27, U29.
**Phase:** Phase A.
**Acceptance:** A new live tradable line cannot go live without a complete signed compliance record; staleness on a fund-level evidence record freezes every listing line referencing it; a contradictory evidence pointer (e.g., issuer disclosure vs broker classification) triggers a freeze and a U05 exception record.
**Gate role:** Gate 1.

---

## 8) Workstream G — German tax: ledgers, precedence, reconciliation, close

The original plan collapsed precedence, exception workflow, annual reconciliation, retrospective changes, and manual close into a single item. They are decomposed here so that each artifact has a single owner and a single acceptance test. The two-ledger structural shape is built day one; the operational close is deliberately minimal per §9.

### U32 — Statutory tax state ledger (Ledger A)

**Do:** Build Ledger A per §9.A with the §9 data-provenance requirement enforced at write time.
**In scope:** Effective-dated fund classification, Teilfreistellung regime, candidate Vorabpauschale state with Basiszins input, deemed-sale/deemed-repurchase state under §22 InvStG, tax lots (acquisition cost, date, quantity, accumulated notional gains/losses), distributions and statutory tax treatment. Every dated entry carries a U27 evidence pointer naming the authoritative external source.
**Out of scope:** Always-on regime-change detection across the broader research universe — v1 operates Ledger A on event cadence only (buy, sell, distribution, regime change, annual close).
**How:** Append-only ledger keyed by `(fund identity, effective date)`; a write-time check refuses entries without a resolved U27 pointer; integration with U16 journal under distinct statutory account codes.
**Depends on:** U16, U27.
**Phase:** Phase A.
**Acceptance:** A statutory ledger write without an evidence pointer is refused; a Teilfreistellung regime transition produces both a Ledger A entry and a candidate U16 deemed-sale/deemed-repurchase posting (the latter requiring U35 manual sign-off).
**Gate role:** Gate 1 (statutory correctness), Gate 4 (cash-buffer sizing).

### U33 — Broker tax-cash ledger (Ledger B)

**Do:** Build Ledger B per §9.B as the as-posted record of broker tax cashflows.
**In scope:** Withholding postings, tax-adjustment postings, distribution-tax postings, annual-close adjustments, timing-difference bridge entries; correction-group supersession via U18 for broker restatements.
**Out of scope:** Statutory derivation of these cashflows — that is Ledger A's responsibility; Ledger B records what the broker did.
**How:** Append-only ledger keyed by canonical identity per U07; integration with U16 journal under distinct broker-cash tax account codes; broker restatements posted as in-group supersessions.
**Depends on:** U13, U16, U18, U21.
**Phase:** Phase A.
**Acceptance:** A broker tax restatement is captured as a correction-group supersession, not as a mutation; Ledger B reconciles to the broker-posted tax cashflow set per fiscal year before any §9 reconciliation residual is computed.
**Gate role:** Gate 1.

### U34 — Tax authority precedence resolver and exception workflow

**Do:** Implement the §9 precedence rules and the cross-ledger disagreement exception workflow.
**In scope:** Ledger A precedence (issuer legal documents → BMF → broker classification as corroborating only); Ledger B precedence (broker statement postings → broker corrections via U18 supersession); cross-ledger residual exception records that block year-end close until resolved; reviewer-driven closure paths (correct Ledger A with new evidence, book bridge adjustment, declare within signed de-minimis tolerance).
**Out of scope:** Silent absorption of any residual under any closure path.
**How:** A resolver consumed at every Ledger A write where multiple sources disagree; an exception-record subsystem tied to U05 and U27; a year-end close gate that refuses to close while an exception is open.
**Depends on:** U05, U27, U32, U33.
**Phase:** Phase A; exercised in B/C as live tax events occur.
**Acceptance:** A red-team scenario with broker-vs-issuer fund-classification disagreement resolves in favour of the issuer with a logged disagreement record; a cross-ledger residual exceeding the tolerance produces an exception record that blocks year-end close until reviewer-signed.
**Gate role:** Gate 1.

### U35 — Annual tax-close reconciliation, retrospective-change reopen, and manual checklist

**Do:** Produce the annual reconciliation artifact, support retrospective broker and issuer changes per §9 by reopening prior years, and operationalize the manual annual tax-close checklist.
**In scope:** Per-year reconciliation artifact enforcing `statutory_tax_delta(year) = Σ broker_posted_tax_cashflows(year) + Σ explicit_bridge_adjustments(year)`; reviewer checklist over Basiszins source, fund classification, Teilfreistellung, regime changes, broker postings vs statutory model, bridge adjustments; reopen logic for retrospective broker restatements (Ledger B correction group, prior-year exception opens automatically) and retrospective issuer changes (Ledger A entry posted under effective date, prior-year exception opens automatically); deemed-sale/deemed-repurchase recomputations under §22 InvStG handled as explicit manually-approved events.
**Out of scope:** Auto-blessing of ambiguous tax transitions — every ambiguous case requires U05 manual sign-off per Rule 5.
**How:** A reconciliation artifact generator that runs at year-end; an exception-reopen handler triggered by retrospective Ledger A or Ledger B writes; a checklist workflow that requires reviewer sign-off before close.
**Depends on:** U05, U27, U32, U33, U34.
**Phase:** Phase A (skeleton); exercised at first year-end in B/C.
**Acceptance:** A synthetic year-end with one bridge adjustment and one open exception refuses to close; a synthetic broker restatement of a prior year automatically opens a prior-year exception; the original prior-year reconciliation artifact remains preserved unchanged when a re-opened reconciliation is journalled.
**Gate role:** Gate 1 (annual close defensibility), Gate 4 (cash-buffer sizing).

---

## 9) Workstream H — economic admission, fees, liquidity, execution policy

This workstream implements the conservative economics and deterministic execution policy used by Gates 2 and 4.

### U36 — Pinned-route fee model

**Do:** Model fees at exactly the §12 production-profile dimensions touched in v1.
**In scope:** Parameterization by route (pinned), venue, order class (DAY limit), session (continuous only), retail-vs-direct path, lifecycle state (new orders only — modified, replaced, overnight-persisting are out of the production matrix), effective date; conservative higher-cost path for any ambiguous classification; Xetra-specific fee treatments where applicable to the pinned route; explicit non-application of retail Xetra fee waivers unless live broker postings demonstrate the account is on that fee path.
**Out of scope:** Fee modeling for excluded lifecycle states (modified, overnight) — these are research-only artifacts in shadow environments and are not collected as live exemplars.
**How:** A fee table per `(route, venue, order_class, session, retail_path, lifecycle, effective_date)`; a pricer that consumes `(intent, current fee table)` and emits a fee estimate plus the dimension list it consumed; ambiguity flag when the dimension list is incomplete.
**Depends on:** U02, U26.
**Phase:** Phase A (model); Phase B–C (exemplar-validated).
**Acceptance:** The fee model prices a representative DAY-limit order on the pinned route; an order whose dimensions are incomplete falls back to the conservative higher-cost path with a flag; an attempt to price a modified or overnight order is refused (those are out of the production matrix).
**Gate role:** Gate 2.

### U37 — Fee exemplar capture and Gate-2 closure analytics

**Do:** Collect live exemplars and test fee/slippage closure on the actual live ticket-size distribution.
**In scope:** Exemplar coverage near `N_min(r,t)`, mid-distribution, and upper bound of the expected ticket-size distribution; modeled-vs-posted fee comparison per dimension; realized-vs-conservative slippage comparison; Gate-2 closure report; refusal to close on placeholders or forgiven residuals.
**Out of scope:** Exemplars for excluded lifecycle states; route-comparison analytics across multiple candidate routes (v2 per §18); Gate-2 closure on placeholder governance parameters (refused per U04).
**How:** Exemplar capture from U13/U16 for every live fill; closure analytics that compute per-dimension residuals and per-ticket-size-bucket residuals; Gate-2 closure report consumable by U05 gate workflow.
**Depends on:** U04, U13, U16, U36.
**Phase:** Phase C (exemplar collection); Gate 2 evaluation triggered when exemplar depth meets governance threshold.
**Acceptance:** A simulated fee residual outside tolerance is reported as a Gate-2 blocker per dimension and per bucket; an attempt to claim Gate 2 closure with any §6 governance parameter on placeholder is refused.
**Gate role:** Gate 2.

### U38 — `N_min`, `AUM_min`, `cash_buffer` calculator

**Do:** Compute route-specific economic ticket minima and tier capital requirements per §6.
**In scope:** `N_min(r, t) = [F_min(r,t) + V_min(r,t) + R(r,t)] / b_1w`; `AUM_min(k, r, t) = k · N_min(r, t) + cash_buffer(t)`; FX component of `R` fixed at zero for v1 EUR-only; `cash_buffer(t)` sized as the larger of (working-cash floor) and (tax-posting upper bound from U32 statutory ledger including expected Vorabpauschale withholdings, distribution-tax postings, worst-case §22 InvStG deemed-sale cash effect for live tax lots whose Teilfreistellung could plausibly transition within the year); annual refresh hook at fiscal-year start and at every regime-transition event in U32.
**Out of scope:** Multi-route `AUM_min` comparison (v1 has one route).
**How:** Pure computation over U02, U04, U32, U36 inputs; capital-sufficiency pack producer for Tier 0/1/2; a cash_buffer-below-tax-upper-bound exception path tied to U05.
**Depends on:** U02, U04, U32, U36.
**Phase:** Phase A (skeleton); Phase B–C (exercised against pinned governance parameters).
**Acceptance:** Capital-sufficiency packs reproduce on identical inputs; a `cash_buffer` below the tax-posting upper bound requires a signed U05 exception; a regime transition in Ledger A automatically refreshes the upper-bound contribution.
**Gate role:** Gate 4 (capital sufficiency); inputs to Gate 2 (cost stack).

### U39 — Ticket-size-native liquidity gate

**Do:** Per §13, gate orders on quoted spread, expected realized slippage anchored to live exemplars when available, and liquidity-window eligibility — never on XLM at retail tickets.
**In scope:** Quoted-spread cap calibrated to order notional; expected-realized-slippage check against the one-way cost budget, anchored to U13/U17 live exemplars at comparable ticket sizes when available, conservative book-derived assumption otherwise; liquidity-window eligibility (excludes first/last minutes of continuous trading unless empirical fill quality justifies it).
**Out of scope:** XLM as a per-order accept/reject gate (forbidden per §7, §13).
**How:** Pre-trade gate function consumed by U25 admission engine; live-exemplar cache joined on `(ISIN, ticket-size bucket)`; conservative book-derived assumption when no exemplar is available.
**Depends on:** U13, U17, U30, U36.
**Phase:** Phase A.
**Acceptance:** An order with acceptable XLM but failing quoted-spread cap is rejected; an order in the first 60 seconds of continuous trading is rejected unless empirical fill quality on U13 exemplars overrides; an attempt to consume XLM as a per-order accept signal is refused at the API level.
**Gate role:** Gate 1 (admission correctness), Gate 2 (slippage modelling).

### U40 — Deterministic limit-price construction and cancel/re-submit policy

**Do:** Make live and simulation execution comparable by construction per §13.
**In scope:** Anchor limit price to live top-of-book at decision timestamp; cap aggression via quoted-spread-derived budget at the operational ticket size; XLM does not enter limit-price construction directly; one documented cancel/re-submit policy (single-shot, time-boxed; re-submission counts as a new order for fee accounting); discretionary manual repricing forbidden except under a signed U05 emergency-override.
**Out of scope:** Adaptive or discretionary limit-price construction.
**How:** Pure deterministic function consuming `(book snapshot, ticket size, ISIN, policy version)`; one-shot cancel/re-submit handler that emits a new admission decision per re-submit; emergency-override path requires U05 signed exception and emits a `permId=0` non-API origin event handled by U19.
**Depends on:** U05, U17, U19.
**Phase:** Phase A.
**Acceptance:** Identical book snapshot and identical policy version produce identical limit price across runs; a cancel/re-submit emits a new admission decision and a new fee accounting; an attempt at manual discretionary repricing without an emergency-override exception is refused.
**Gate role:** Gate 1 (admission determinism), Gate 2 (cost-stack consistency).

### U41 — Fill telemetry, bootstrap fill model, and exemplar calibration

**Do:** Implement the §13 fill model in its v1 telemetry/stress role only — never as an admission-scalar input.
**In scope:** Bootstrap `p_fill` model as monotone-decreasing in limit aggression inside the spread, anchored to a conservative prior from venue order-book snapshots (queue depth, replenishment rate); recording of bootstrap `p_fill` on every admission decision per U17; exemplar-calibrated replacement of the bootstrap as live fills/no-fills accumulate, with calibration parameters pinned via U04; explicit refusal to feed `p_fill` into the per-trade admission scalar in v1.
**Out of scope:** v2 promotion of `p_fill` into the admission scalar (deferred per §13, §18).
**How:** Bootstrap model in the simulator and the live admission decision recorder; exemplar calibration runs as a scheduled job; calibrated model replaces bootstrap for telemetry and for the U43 fill-probability stress overlay only.
**Depends on:** U04, U13, U17.
**Phase:** Phase A (bootstrap); Phase C (exemplar calibration).
**Acceptance:** Every admission decision in U17 carries a bootstrap `p_fill` telemetry value; an attempt to wire `p_fill` into U41/U42 admission scalar is refused at the type level; a calibrated fill model replaces the bootstrap for telemetry/stress without entering admission.
**Gate role:** Gate 2 (fill-rate tracking), Gate 3 (fill-probability stress overlay input).

### U42 — Cancel/re-submit budget controller

**Do:** Contain the fee trap from cancel/re-submit per §13 without reintroducing modify/replace.
**In scope:** Per-decision re-submit cap from U04; decision-level fee pre-check that includes the worst-case commission charge under the re-submit cap when computing `Ĉ_RT` (consumed by U43); mandatory recording of every re-submit on U17 with its own `strategy_intent_id` linkage; cumulative re-submit budget per rolling window from U04; freeze-on-budget-exhaustion behaviour.
**Out of scope:** Modify/replace — explicitly forbidden as normal operation.
**How:** A controller invoked at every cancel/re-submit decision; budget-window state computed from U17; freeze action invokes U05 exception workflow.
**Depends on:** U04, U05, U17.
**Phase:** Phase A.
**Acceptance:** A re-submit beyond the per-decision cap is refused; a cumulative-budget exhaustion freezes further admission until reviewer sign-off; the decision-level fee pre-check correctly inflates `Ĉ_RT` to the worst-case under the cap.
**Gate role:** Gate 1 (admission correctness), Gate 2 (cost-stack inclusion).

### U43 — Cost-stack `Ĉ_RT` assembler and per-trade economic admission engine

**Do:** Assemble the `Ĉ_RT` cost stack and enforce the §6 per-trade fill-conditional admission rule.
**In scope:** `Ĉ_RT` = expected commission (including worst-case re-submit charge from U42) + venue/clearing/regulatory fees from U36 + spread reserve + tax friction (FX = 0 under v1 EUR-only); per-trade admission rule `E[Δα_vs_do_nothing | fill] > Ĉ_RT + B_model + B_ops`; ticket-size check `≥ N_min(r, t)`; tier check (current tier permits the proposed position count); churn cap check; Gate-3 strategy certification check, **except for Tier-0 plumbing trades explicitly allowed by §6/§16** which require Phase-0 pre-screen pass or signed waiver but do not require Gate-3 certification.
**Out of scope:** Per-trade LCB gating (deferred per §6 to v2); admission against the joint fill/no-fill outcome space (§6 specifies fill-conditional in v1).
**How:** A pure admission function consuming all stated inputs; integration with U25 live admission engine as the economic-admission stage; explicit Tier-0 plumbing-trade exception path that records the bypass under U17 and U05.
**Depends on:** U04, U05, U17, U30, U33, U36, U38, U41, U42, U45 (Gate-3 certification status).
**Phase:** Phase A; exercised in Phase B onward.
**Acceptance:** Every admission decision is deterministic given inputs and bound to the U28 snapshot; an admission attempt by a strategy without a current Gate-3 certification is refused except for Tier-0 plumbing trades; the Tier-0 plumbing-trade path records its bypass under U17.
**Gate role:** Gate 1 (admission correctness), Gate 3 (admission consistency with certification).

---

## 10) Workstream I — alpha, simulator, paper/micro-live, gate evidence

This workstream keeps simulator and live logic aligned, proves path consistency in paper and micro-live, and produces the gate evidence packs.

### U44 — Alpha protocol: threshold-first low-turnover rebalancing

**Do:** Implement the §13 default v1 alpha form — threshold-first, low-turnover rebalancing — as a research-and-live module that emits strategy intents.
**In scope:** ETF-native features only; slow horizon only; threshold-first rebalance trigger (trade only when threshold breach is large enough to clear the per-trade admission hurdle); decision benchmarks against do-nothing and against a strategic reference basket with no turnover; configurable but fixed-for-v1 cadence (research evaluated monthly, live strategy review quarterly).
**Out of scope:** Issuer-event propagation, NLP, intraday reactivity, holdings-propagated signals, scheduled monthly rotation (v1 is threshold-first, not scheduled).
**How:** A typed strategy module that consumes dated reference data and current holdings, evaluates the threshold-first rule, and emits a strategy intent under a `strategy_intent_id` per U07; benchmark computation against do-nothing and reference basket recorded per intent.
**Depends on:** U17, U26, U28.
**Phase:** Phase A.
**Acceptance:** A simulation over the research universe produces strategy intents that match the threshold-first rule deterministically; identical inputs produce identical intents; the alpha module emits intents that flow through U43 admission unchanged in interface.
**Gate role:** Gate 3 (strategy under certification).

### U45 — Canonical simulator bound to the pinned production profile

**Do:** Build one canonical simulator per §15, sharing admission logic with the live engine.
**In scope:** Binds to the pinned production route, fee path, and fee model; uses §6 governance parameters from U04, with placeholder/pinned tag per run; applies the exact U25/U43 admission logic including U39 ticket-size-native liquidity gate and U42 re-submit budget; uses dated fund metadata (U26), dated tax state (U32), and the currently active settlement regime (U24); §13 fill model from U41 present in telemetry/stress role only — does not enter admission scalar; tagged `simulator_run_kind = "canonical"` with U28 snapshot identifiers and a per-parameter pinned/placeholder flag.
**Out of scope:** Two-mode exploratory/production simulator architecture (deferred per §2/§15).
**How:** A simulator driver that reuses the U25/U43 admission code path against synthetic broker behaviour; per-run output includes `simulator_run_kind`, snapshot bindings, and per-§6-parameter pinned/placeholder flag.
**Depends on:** U04, U24, U25, U26, U28, U32, U36, U41, U42, U43, U44.
**Phase:** Phase A (skeleton); Phase B–C (exercised against live exemplars).
**Acceptance:** Canonical runs and live decisions produce identical admission outcomes given identical inputs; a canonical run consuming any placeholder governance parameter is tagged ineligible for gate evidence.
**Gate role:** Gates 2 and 3.

### U46 — Scenario overlays and mandatory stress suite

**Do:** Implement the §15 scenario overlays as research-only artifacts, with the §15/§17 fill-probability stress overlay made mandatory for Gate 3.
**In scope:** Named overlays — fee stress, spread stress, fill-probability stress (tightened `p_fill` relative to bootstrap), settlement-delay stress, stale-KID/classification freeze, broker-correction replay (verifying `exec_correction_group_id` semantics), order-partial/reject/cancel paths, re-submit-budget exhaustion, Green/Amber/Red transition tests; each overlay tagged `simulator_run_kind = "overlay:<n>"` referencing a canonical-run base; explicit refusal of overlay outputs as gate evidence except for the Gate-3-required fill-probability stress pass.
**Out of scope:** Settlement-regime-transition stress including the 2027 T+1 cutover (deferred per §15).
**How:** Per-overlay parameter perturbation against a canonical base run; overlay outputs persisted with their tag; gate-evidence intake refuses overlay outputs except as the §17 Gate-3 fill-probability stress pass.
**Depends on:** U45.
**Phase:** Phase A (skeleton); exercised throughout B/C.
**Acceptance:** A Gate-3 claim that fails under tightened `p_fill` overlay is rejected as not a Gate-3 pass per §17; an attempt to submit any other overlay output as gate evidence is refused; overlay-vs-canonical lineage is traceable per run.
**Gate role:** Gate 3 (fill-probability stress); supporting infrastructure for risk surfacing.

### U47 — Off-platform shadow-evaluation rig and Gate-3 strategy-level LCB certification

**Do:** Run the canonical simulator at strategy/decision-category scale over the research universe to produce Gate-3 strategy-level LCB evidence per §17.
**In scope:** Off-platform shadow evaluation over the research universe with all §6 governance parameters pinned; strategy-level LCB computation `LCB[Δα_vs_do_nothing | fill, strategy, horizon] > Ĉ_RT + B_model + B_ops`; mandatory survival under U46 fill-probability stress overlay; Gate-3 evaluation horizon long enough to be credible against the strategy's natural decision cadence; explicit grant/revoke/expiry of certification with a recorded re-certification cadence.
**Out of scope:** Per-trade LCB (v2); Gate-3 admission of strategies whose evidence rests on placeholder parameters.
**How:** A scheduled shadow-evaluation job over a frozen OOS window per certification event; LCB calibration per the U04 procedure; certification objects persisted under U05 with explicit grant/revoke/expiry; loss of certification immediately blocks new rotation trades for the affected strategy in U43 (existing positions are not force-liquidated).
**Depends on:** U04, U05, U44, U45, U46.
**Phase:** Phase A (rig); Phase C/D (certification claims after Gate 2 closes).
**Acceptance:** A Gate-3 certification cannot be claimed before Gate 2 closes (predecessor enforced by U05); a strategy whose LCB clears at bootstrap `p_fill` but fails at stress `p_fill` is refused certification; a strategy losing certification immediately stops emitting admissible rotation trades through U43.
**Gate role:** Gate 3.

### U48 — Paper-trading harness

**Do:** Run the exact production profile in paper to prove path consistency and failure handling per §16 Phase B.
**In scope:** Same admission engine (U25/U43), same fee model (U36), same journal (U16), same reconciliation harness (U21), same Green/Amber/Red controller (U22); coverage of reject, cancel, expiry, partial fill, correction, restatement, and Red-state escalation paths.
**Out of scope:** Paper trading as alpha proof — paper proves path consistency and failure handling, not alpha (per §16).
**How:** A paper environment per U06 with isolated archive partition; paper runs go through the identical admission and reconciliation pipeline as live; failure-handling fixtures from U11 exercised against the paper environment.
**Depends on:** U06, U11, U16, U21, U22, U25, U43.
**Phase:** Phase B.
**Acceptance:** Paper results reconcile through the same pipeline as live; every failure-handling path (reject, cancel, expiry, partial, correction, restatement, Red escalation) is exercised at least once; no paper write reaches a live archive partition.
**Gate role:** Gate 1 (path-consistency evidence).

### U49 — Tier-0 micro-live runbook and exception handling

**Do:** Operationalize Tier 0 exactly as scoped per §16 Phase C.
**In scope:** Maximum 1 funded position; no same-day rotation; no extended-session trading; no unsupported instruments; no route changes; no normal modify/replace; emergency manual exception path with evidence-backed exception logging via U05 and non-API origin handling via U19; fee-closure exemplar collection in progress through U37.
**Out of scope:** Tier 1+ behaviour (post-Gate-2); discretionary route or session changes.
**How:** Runbook documenting Tier-0 operating procedures; runtime guards enforcing all Tier-0 constraints derived from U04 and U05; emergency-exception path documented step-by-step with U05 signoff and U19 fallback identity handling.
**Depends on:** U05, U19, U37, U43, U48.
**Phase:** Phase C.
**Acceptance:** A Tier-0 attempt to fund a second position is refused; an emergency manual trade is admitted only with a signed U05 exception, recorded under U19 fallback identity, and surfaces in U10 metrics; fee-closure exemplar accumulation is observable in U37 reporting.
**Gate role:** Gate 1 (Tier-0 operability), Gate 2 (live exemplar source).

### U50 — Gate evidence packs, narrow-success continuation programme, and controlled scale-up

**Do:** Produce formal evidence packs per §17 for Gates 1–4, the narrow-success continuation programme (Gate 1 closed, Gates 2–4 deferred under documented capital-scale-up programme per §1), and the controlled scale-up procedure for Tiers 1 and 2.
**In scope:** Per-gate evidence pack consumable by U05: Gate 1 (reconciliation proof from U21, replay determinism from U20, fallback-identity audit from U19, Green/Amber/Red transition tests from U22 and U46, signed compliance/tax/profile artifacts from U26/U27/U31/U32, Phase-0 pre-screen verdict from U03, deterministic admission from U25/U43); Gate 2 (pinned-governance proof from U04, fee/slippage closure from U37, fill-rate tracking calibration check from U41); Gate 3 (off-platform shadow LCB evidence from U47, fill-probability stress survival from U46, Gate-2-closed predecessor verified by U05); Gate 4 (`AUM_min` proof from U38, observed live-ticket sizes consistent with `N_min` from U17/U37, cash-buffer check from U38). Narrow-success continuation programme: a documented programme that schedules the live-exemplar accumulation, capital scale-up, and Gate-2/3/4 evaluation events that close the deferred gates. Controlled scale-up: explicit Tier-1 and Tier-2 promotion procedures tied to U05 governance.
**Out of scope:** v2 scope expansion (multi-route, event-driven runtime, etc.) — explicitly excluded per §18.
**How:** Per-gate evidence-pack composers that read from the named items above and produce a signed pack referenced by U05; a narrow-success continuation programme document with scheduled review checkpoints; Tier-1 and Tier-2 promotion procedures encoded in U05 workflow.
**Depends on:** U03, U04, U05, U17, U19, U20, U21, U22, U25, U26, U27, U31, U32, U37, U38, U41, U43, U46, U47, U48, U49.
**Phase:** Gate 1 evidence at end of Phase B / start of Phase C; Gate 2 evidence during Phase C as exemplar depth meets threshold; Gate 3 evidence after Gate 2 closes; Gate 4 evidence as capital and live ticket sizes meet `N_min` / `AUM_min`.
**Acceptance:** A reproducible, signed evidence pack exists per closed gate; a narrow-success terminal state is reachable with Gate 1 evidence pack signed plus a signed continuation programme; full-success terminal state requires all four packs signed; scope expansion to a second route is refused unless all four packs are signed and the new route has its own Gate-1 and Gate-2 packs.
**Gate role:** All four gates.

---

## 11) Phase plan summary

| Phase | Triggered by | Required items at start | Exits to |
|---|---|---|---|
| **Phase 0** | Project start | U02 (assumptions registry skeleton), U03 (pre-screen pack), U04 (parameter register skeleton with placeholders), U06–U09 (foundations) | Phase A on pre-screen pass; project re-scope on pre-screen fail |
| **Phase A** | Pre-screen pass | All A-stream items skeletoned; B-stream complete; C/D-stream complete; E-stream complete; F-stream at minimum live-universe scope; G-stream skeleton; H-stream model and admission engine; I-stream simulator skeleton, alpha module, paper harness | Phase B on offline replay and synthetic-broker fixtures passing |
| **Phase B** | Offline replay passing | Paper environment under U06; full pipeline exercised end-to-end; reject/cancel/expiry/partial/correction/restatement/Red paths exercised | Phase C on paper-trading path-consistency verified and Phase-0 verdict still standing |
| **Phase C** | Paper validated | Tier-0 runbook (U49); exemplar collection (U37) in progress; Gate-1 evidence pack (U50) being composed | Phase D on Gate 1 closed; or narrow-success continuation if Gates 2–4 cannot close in v1 horizon |
| **Phase D** | Gate 1 closed | Tier-1 promotion procedure (U05/U50) ready; Gate 2 closure analytics (U37); Gate-3 certification rig (U47); `AUM_min` capital schedule (U38) | Full-success v1 on all four gates closed; or narrow-success v1 with continuation programme |

Re-pinning of the production route (§5) at any point during v1 re-opens Gate 2 for the new profile and may require partial replay of Phase B / Phase C for the new profile (per §17 scope-expansion rule).

---

## 12) Gate-readiness matrix

| Gate | Required closed items |
|---|---|
| **Gate 1 — operational correctness** | U01, U02, U05, U06–U11 (foundations), U12–U15 (broker ingestion and archive), U16–U22 (kernel), U23–U25 (cash and admission), U26–U31 (reference, evidence, universe, compliance), U32–U35 (tax ledgers and close), U36, U39, U40, U42, U43, U44, U45 (skeletons), U48 (paper) |
| **Gate 2 — cost-model closure** | All §6 governance parameters in U04 pinned (no placeholders consumed); U36 (fee model); U37 (exemplar capture across the ticket-size distribution the live profile generates); U41 (fill-rate tracking calibration); U42 (re-submit budget proven in production); U45 canonical-simulator runs against pinned parameters |
| **Gate 3 — strategy-level alpha evidence** | Gate 2 closed (predecessor enforced by U05); U47 (off-platform shadow evaluation with all §6 parameters pinned, fill-conditional LCB above hurdle, mandatory U46 fill-probability stress overlay survived); strategy under certification holds a current grant from U47 |
| **Gate 4 — capital sufficiency** | Gate 3 closed; U38 (`AUM_min` for the pinned route at the proposed `k`); U17/U37 (observed live ticket sizes consistent with `N_min`); U32/U38 (cash-buffer covers expected tax postings and fee-misclassification reserve) |
| **Scope expansion to a second route** | All four gates closed for the first profile; a fresh Gate-1 and Gate-2 pack independently produced for the new profile |

---

## 13) Out-of-scope reminders (do not build in v1)

- Multi-venue live routing, route-comparison science across multiple candidate routes (deferred to v2 per §2, §18).
- Forward-dated settlement-regime entries including the EU T+1 cutover on 11 October 2027 (deferred to pre-cutover hardening / v2 per §11, §18).
- Per-trade LCB gating and joint-outcome `(fill, no-fill)` admission (deferred to v2 per §6, §13, §18).
- Hard XLM-driven universe freeze; XLM remains a monitoring and review-trigger signal only (per §7, §13).
- Two-mode exploratory/production simulator (deferred to v2 only if a structural gap emerges; per §15, §18).
- Holdings-propagated issuer-event alpha; NLP/LLM-driven alpha; intraday reactive trading; extended retail sessions; auctions; market orders; modify/replace as normal operating pattern; overnight order persistence (per §2, §5).
- Automated handling of ambiguous tax and compliance edge cases without human sign-off (per §2, §5 Rule 5).

These exclusions are enforced by the runtime guards in U01 and U05, by the type-level admission constraints in U25/U43, and by the v2 scaffold preservation in §18 of the idea — no v1 item builds against any of them.