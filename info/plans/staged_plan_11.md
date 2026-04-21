## Staged plan

### Execution graph

`Stage 1 + Stage 2 bootstrap/contract freeze → Stage 3 pre-screen → Stage 4 + Stage 5 + Stage 6 (min scope) in parallel → Stage 7 → Stage 8 + Stage 9 in parallel → Stage 10 phase A (pure libraries + live admission path with "no strategy certified" default) → Stage 11 (canonical simulator + certification) → Stage 10 phase B (consume certification status for non-Tier-0 live rotation) → Stage 12`

Stage 10 ships in two calendar phases within the same stage. Phase A delivers `AdmissionKernel`, `FeeModel`, and `ExecutionPolicy` as pure libraries plus the live admission path with `StrategyCertificationStatus` defaulting to `none` (so only Tier-0 plumbing trades are admissible). Stage 11 imports the libraries and issues Gate-3 certifications. Stage 10 phase B then consumes those certifications to admit non-Tier-0 live rotation. The Stage 10 ↔ Stage 11 relationship is therefore ordered in calendar time even though the two stages share interface contracts. No numbering change — the stage remains Stage 10 with two phases.

Gate 2 closure is a Stage-10 responsibility (pinned-route fee-model closure on observed live exemplars). Gate 3 certifications may only be issued by Stage 11 **after** Gate 2 has closed (§17 ordering constraint). Gate 1 evidence is assembled by Stage 12 from upstream artifacts and requires, among other items, a **simulator/live admission conformance artifact** produced jointly by Stages 10 and 11 under canonical semantic equivalence (see `AdmissionConformanceReport` in cross-stage contracts).

Stage boundaries are chosen so different senior engineers can work semi-independently while handing off through stable persisted contracts. Every implied cycle is broken by a named contract in the cross-stage contracts section; no stage depends on a downstream stage except through (i) a library import from a concretely earlier stage, or (ii) a read-only contract shipped with a documented conservative default at upstream-stage delivery time.

**Truth-store writer discipline.** `JournalStore` and `ExecutionEventStore` are **single-writer** under Stage 5. Every other stage that needs to post economic or causal truth submits a typed append request through Stage 5's `JournalPostingGateway.append(...)` or `ExecutionEventAppender.append(...)`. This enforces the idea's §10 Rule 4 "Journal is sole economic truth" structurally rather than by convention and eliminates the multi-writer contention that ledger-style systems fail on most often.

**Conformance discipline.** All conformance checks (adapter conformance, simulator/live admission conformance, replay conformance) use **canonical semantic equivalence** under a single Stage-2-owned canonical serializer, not literal byte identity. This is consistent with the idea's §10 Rule 4 definition of deterministic replay as economic-state invariance, which explicitly rejects literal byte-identity as brittle under API-order-ID remapping, rebinding, and callback-ordering differences.

---

### Stage 1 — Governance bootstrap, pinned profile, workflow shell, reviewer signing workflow, and registries

**Goal / responsibility**
Encode the exact v1 live profile, the out-of-scope deny list, the gate/tier/waiver/re-pinning workflow, the governed parameter registry (including the pinning-event ingress — this is the **sole** write path for parameter status transitions), the reviewer signing workflow used everywhere a "signed decision" is required, and the registries that later stages write into (evidence registry schema, exception/override store schema, compliance decision store schema). This is the control plane for the whole programme.

**Reads**
Signed project charter; external broker / market / tax / legal assumptions (§3 of the idea); reviewer identities; starting public pricing (IBKR Germany commission minima per the §1 context note).

**Writes**
- `governance.production_profile` — the pinned v1 live profile (§5): broker, instrument type, currency, route, session, hours, order type, routing/pricing plan, plus the signed pinning rationale.
- `governance.shadow_profiles` — routes/profiles runnable in paper/simulation only.
- `governance.deny_list` — the §2 out-of-scope enumeration as an executable deny list (market orders, extended sessions, auctions, intraday reactive, NLP-driven alpha, more than one live profile, etc.) that Stages 4, 10, and 11 import as a config, not a convention.
- `governance.parameters` — the §6 parameter registry. Each parameter has `{current_value, status ∈ {placeholder, pinned}, placeholder_rationale, pinning_decision_ref?}`. Covers:
  - `b_1w`, `R(r,t)` with itemised composition (FX component fixed at zero for v1 EUR-only scope), `cash_buffer(t)` sizing rule, `B_model`, `B_ops`.
  - Strategy-level LCB confidence-margin calibration procedure (sampling distribution, calibration window, refresh cadence, Gate-3 evaluation horizon), certification validity interval, certification degradation thresholds.
  - Churn-budget window and cap, per-decision re-submit cap.
  - Per-channel tolerated divergence band, per-channel reconciliation-grace window.
  - **Gate-2 closure tolerances** (Stage 10 / §12 / §17 Gate 2 consumer): modelled-vs-posted-fee tolerance, realised-slippage-vs-conservative-model tolerance, realised-fill-rate-vs-bootstrap tolerance, minimum exemplar coverage by ticket-size bucket.
  - `xlm_admission_threshold` and sustained-deterioration threshold (Stage 6 consumer).
  - Fill-model calibration parameters (Stage 10 consumer: minimum exemplar counts, calibration window, refresh cadence).
- `governance.parameter_snapshots` — immutable snapshot rows with a `parameter_snapshot_id`; every downstream decision and every simulator run references a snapshot id, not a mutable live row. Snapshots are created at every pinning event and on an explicit scheduled cadence; this is how §14 "As-of versioning" is realised for governance state.
- `governance.pinning_workflow` — the single CLI/service entry point for status transitions (`placeholder → pinned`, `pinned → re-pinned`); every transition is an audited, signed event that produces a new parameter snapshot and ties to a specific evidence-registry record. **No other stage may mutate `governance.parameters` directly.**
- `governance.gate_state` — per-gate state with permitted transitions (`not_started → in_progress → closed`, never backward without a signed re-open event); blocks impossible transitions (e.g., Gate 3 cannot be claimed passed while Gate 2 is not closed, per §17). The only write ingress is Stage 12's `GateEvidencePackStore`; Stage 1 owns the transition rules and the re-open event shape.
- `governance.tier_state` — current tier with the per-tier admission rules from §6 (Tier 0 plumbing-only, Tier 1 single-position economic, Tier 2 two positions), transition rules.
- `governance.exceptions` — the exception/override workflow schema **and** instance store: exception kind (tax exception, compliance freeze override, emergency manual repricing, cash-buffer-under-tax-bound override, pre-screen waiver, reconciliation-break manual close), reviewer, signed decision, expiry, linkage keys for later stages. All exception writes route through Stage 1's signing workflow; other stages supply instance data but cannot bypass the signing step.
- `governance.repin_events` — signed re-pinning decision records; re-pinning the production route reopens Gate 2 on the new profile (§5, §12) by resetting `governance.gate_state` for Gate 2.
- `governance.reviewer_roster` — reviewer identities referenced everywhere a "signed decision" is required.
- `governance.signing_workflow` — the shared reviewer-sign component (key binding, signature format, roster lookup, audit emission to `evidence.audit_log`). Stages 3, 6, 9, 10, 12 call this; they do not implement their own signing.

**Success criteria / executive result**
Unsupported route / session / order / lifecycle cases are rejected by configuration, not convention; every §6 parameter has explicit `placeholder` vs `pinned` status at all times and only Stage 1's pinning workflow can flip the status; every downstream decision artifact references a `parameter_snapshot_id` (mutable live rows cannot leak into historical artifacts); impossible gate transitions are blocked by code, not by custom; every exception path has a typed record with a reviewer and a single signing path; re-pinning is a signed state change that reopens Gate 2 for the new route. Executive result: the project is constrained to the intended v1 envelope before any trading logic is trusted, and there is exactly one place to look to answer "is this parameter pinned at decision time D", "who signed off on this exception", or "which gate is currently open".

---

### Stage 2 — Foundations: environments, schemas, contract freeze, canonical serialization, time authority, signing primitives, observability, test harness, secrets, market-data capture and replay

**Goal / responsibility**
Provide the common substrate: paper/live environment separation, schema registry with versioned migrations, a canonical-serialization contract used by every conformance check, single time/calendar authority, cryptographic signing primitives (consumed by Stage 1's signing workflow), content-addressed storage, structured logs/metrics/alert bus, deterministic test/replay fixtures, secrets management for broker credentials, and the market-data stores that Stages 3, 6, 10, and 11 depend on — split into decision-time snapshots and historical research series.

**Reads**
Stage 1 profile and workflow requirements; reviewer roster (for signature validation).

**Writes**
- `config.environments` — paper vs. live vs. research environment config; the Stage-4 broker adapter refuses to connect to the live broker outside the live environment.
- `schema_registry.*` — typed schemas for every persisted entity with a `schema_version` field and forward-compatible migration rules; includes `canonical_identity_namespace_version` referenced by §14.
- `contracts.schema_bundle` — the versioned bundle of cross-stage schemas published to every consuming team.
- `contracts.canonical_serialization` — the single canonical serializer used by adapter-conformance, admission-conformance, replay, and gate-pack hashing. This is what makes semantic equivalence checkable: the same logical event always produces the same canonicalized bytes, independently of field ordering or serializer library version. All conformance reports downstream cite `canonical_serialization` version.
- `contracts.test_vectors` — reference test vectors that pin the canonical serialization behaviour against known inputs, regression-tested on every contract bundle change.
- `clock.calendars` — XETR trading calendar and settlement business-day calendars (sole time authority; no stage may read wall-clock time directly).
- `evidence.artifacts` — content-addressed blob store for raw documents (KID, prospectuses, BMF publications, broker PDF statements, market-data raw responses).
- `evidence.signing_primitives` — cryptographic primitives (keys, signature verification) that Stage 1's `governance.signing_workflow` builds on; every signed artifact, decision, and gate-pack entry resolves against these.
- `evidence.audit_log` — tamper-evident append-only audit of governance state changes (parameter pinning, re-pinning, exception open/close, tier promotion, gate transitions).
- `obs.logs` / `obs.metrics` / `obs.alerts` — structured logging, metrics, and alert bus with typed alert classes the later stages subscribe to. Every stage that needs to emit an alert publishes here; Stage 7 owns the alert-policy taxonomy.
- `test.fixtures` / `test.synthetic_broker` / `test.replay_corpus` — deterministic broker fixtures semantically compatible with the Stage-4 adapter (semantic compatibility **certified by Stage 4** under the canonical serializer, not here), including synthetic `execDetails` correction sequences and `permId=0` non-API cases; replay corpus covers every mandatory stress test in §15.
- `secrets.broker_credentials` — encrypted broker credential store; access is logged and scoped to the Stage-4 adapter runtime only.
- `marketdata.raw_captures` — content-addressed raw market-data payloads (Xetra top-of-book snapshots at decision timestamps, published XLM values, corporate-action feeds). Stored once, referenced many times.
- `marketdata.decision_snapshots` — immutable `market_data_snapshot_id` records bound to individual live decisions and simulator runs (Stage 6's `snapshots.asof_bindings` point at these).
- `marketdata.research_series_raw` — historical research/replay market data: top-of-book, quote history, trade history, corporate-action-adjusted price series, published XLM time series. Versioned by `market_data_series_id` and bindable into the same as-of mechanism as decision snapshots.
- `marketdata.research_series_views` — derived views of the raw research series (canonical bars, adjusted closes, book-depth reconstructions) with their generation parameters recorded for reproducibility. Owned here so Stage 11 does not invent its own dataset.

**Success criteria / executive result**
Paper and live cannot collide; all persisted entities are typed and versioned; time comes only from one authority; signing primitives are tamper-evident and used only by Stage 1's signing workflow (stages do not sign directly); canonical serialization is deterministic and regression-tested, making every downstream semantic conformance check feasible; broker fixtures are replayable; secrets cannot leak into logs or test output; every decision-time market-data read resolves against a `market_data_snapshot_id`; every historical simulator run resolves against a `market_data_series_id`. Executive result: later stages can be built independently without hidden local clocks, mutable files, ad hoc logging, per-stage signing code, per-stage market-data capture, or shared credential files — and they share a single canonical serializer that makes conformance evidence robust.

---

### Stage 3 — Phase-0 economic feasibility pre-screen (isolated from canonical simulator)

**Goal / responsibility**
Run the one-shot economic stop-loss before industrializing the rest of Phase A. This is not a gate, but it is a hard programme-control checkpoint (§16). It uses a throwaway minimal backtest harness that is *not* the canonical simulator — the canonical simulator is Stage 11 and is downstream. The pre-screen is **structurally isolated** from the canonical simulator: Stage 3 outputs are not admissible as simulator evidence at any gate, with the explicit exception of the frozen threshold/verdict/inputs artifacts that §16 and §17 require as references.

**Reads**
- `governance.production_profile` — the pinned route for fee-floor calculation.
- `governance.parameters` with all values at their `placeholder` status set to upper-end conservative values per §6; **binds to a specific `parameter_snapshot_id`** before OOS evaluation.
- External inputs: IBKR Germany public pricing (minimum commissions by route), published venue/clearing/regulatory fee schedules, historical price series for the frozen research universe — **captured as raw evidence artifacts** into `evidence.artifacts` (Stage 2) before OOS computation, so the pre-screen is reproducible without the Stage-6 reference-data layer that does not yet exist at pre-screen time (§14 sequencing note).
- `marketdata.research_series_raw` (Stage 2) — the pre-screen binds to a frozen `market_data_series_id` so OOS re-runs are reproducible.
- `evidence.artifacts` / `evidence.signing_primitives` / `governance.signing_workflow` — for freezing and signing the four inputs below before the OOS window is evaluated.

**Writes**
- `evidence.prescreen.engine_manifest` — the specific pre-screen harness version, its dependency pins, and an explicit declaration that this harness is not the canonical simulator. Prevents accidental promotion of pre-screen logic into Stage-11 code paths.
- `evidence.prescreen.input_bundle` — the full input bundle frozen before OOS: protocol, window, threshold, decision rule, pricing snapshot, parameter snapshot id, market-data series id. Single artifact, content-hashed.
- `evidence.prescreen.protocol` — research protocol: feature construction, ranking rule, rebalance trigger, research universe (§16 item 1).
- `evidence.prescreen.oos_window` — calendar span, in-sample / out-of-sample split, decision cadence (§16 item 2).
- `evidence.prescreen.threshold` — required minimum strategy-level net excess benefit after round-trip floor and upper-end placeholder buffers (§16 item 3); a single signed number.
- `evidence.prescreen.decision_rule` — pre-registered pass/fail statement signed before OOS evaluation (§16 item 4).
- `evidence.prescreen.implied_floors` — implied one-way and round-trip floor at candidate ticket sizes (EUR 1,500 / 3,000 / 5,000) at the pinned route's public pricing.
- `evidence.prescreen.pricing_snapshot` — the raw captured public-pricing inputs with retrieval timestamp and document hash, resolving against `evidence.artifacts`.
- `evidence.prescreen.oos_result` — the single computed OOS net excess benefit, computed strictly per the frozen protocol/window.
- `evidence.prescreen.verdict` — pass/fail, with a hash-link to the four frozen inputs, the input bundle, the engine manifest, and the OOS result.
- `governance.exceptions` entry (via Stage 1's signing workflow) if the programme elects to waive a fail; otherwise Phase-A build is blocked.

**Success criteria / executive result**
The four frozen inputs exist in the evidence registry, with timestamps strictly earlier than the OOS computation; the verdict is reproducible from those frozen inputs, the pricing snapshot, the parameter snapshot, and the market-data series id; the engine manifest prevents the pre-screen harness from leaking into Stage 11; a fail blocks industrialized Phase-A build unless explicitly waived under a signed exception, and re-running against a new candidate alpha requires a *fresh* pre-screen with its own frozen inputs (§16). Executive result: either continue with a finance-grade falsifiable alpha path, or rescope to buy-and-hold / higher capital / project close, with the decision defensible in evidence.

**Downstream linkage.** The pinned threshold here is a ceiling on the required effect size used at Gate 3 — §16 forbids loosening it at Gate 3 — so Stage 11 must read `evidence.prescreen.threshold` when issuing certifications. The pre-screen's OOS numbers themselves are **not** admissible Gate-3 evidence (only the threshold and verdict are).

---

### Stage 4 — Broker integration, immutable raw archive, idempotent order submission, and adapter conformance

**Goal / responsibility**
Own the broker boundary: TWS/IB Gateway adapter, Trade Confirmation Flex polling, Activity Flex polling, connection lifecycle (reconnect, heartbeat, resubscription), truth-hierarchy projection (§10), idempotent order submission keyed by a stage-assigned `client_order_token`, and immutable raw archival. Explicitly supports `permId=0` non-API-originated activity so that Stage 5's fallback identity (§10) has a clean input. Certifies **semantic normalization equivalence** of Stage 2's `test.synthetic_broker` against the live adapter's wire-level parser under the canonical serializer — *not* byte-level equivalence, which §10 Rule 4 explicitly rejects as brittle.

**Reads**
- `config.environments` / `secrets.broker_credentials` (live vs. paper, credentials).
- `schema_registry.*` / `contracts.canonical_serialization` / `clock.calendars` / `test.synthetic_broker` (Stage 2).
- `governance.deny_list` (refuses to submit orders in out-of-scope session/order-type/route combinations at the adapter boundary as a defence-in-depth layer; rejects before submission, never after).
- `OrderSubmissionRequest` instances via the `OrderSubmissionGateway` contract — written by Stage 10 as the sole live-order caller.

**Writes**
- `broker.raw_payloads` — content-addressed raw bytes from TWS/IB Gateway callbacks (including `openOrder`, `orderStatus`, `execDetails`, `commissionReport`, `updatePortfolio`, `accountSummary`), with retrieval timestamp and parser version.
- `broker.flex_raw_pulls` — content-addressed Trade Confirmation Flex and Activity Flex pulls, tagged by Flex channel (daily-close vs. intraday-delayed per §3).
- `broker.poll_watermarks` — per-channel polling watermarks (last-pulled timestamp, next-expected-pull, detected gaps) so Stage 7's Green/Amber/Red controller can reason about staleness operationally rather than by heuristic.
- `broker.normalized_events` — typed events derived from raw payloads with `canonical_identity_namespace_version` stamped and canonical-serialization applied; includes `permId=0` events flagged `origin="non_api"` with the statement posting identity attached for Stage 5 fallback-identity use. Emitted as typed append requests to Stage 5's `ExecutionEventAppender.append(...)` — Stage 4 does not write to `ExecutionEventStore` directly.
- `broker.correction_callbacks` — explicit record of `execDetails` corrections under the same `execId`/`permId` except for the new `execId` (§3, §10), with `exec_correction_group_id` assigned at ingestion so Stage 5 does not have to re-derive it. Submitted to Stage 5's appender as typed correction requests.
- `broker.truth_projection` — three separate truth-channel projections: (i) TWS/Gateway intraday, (ii) Trade Confirmation Flex delayed-intraday, (iii) Activity Flex end-of-day (§10 truth hierarchy). These are never merged.
- `broker.account_snapshots` — periodic authoritative snapshots of broker-reported settled cash and buying power, tagged by source channel (for Stage 8's "broker is authoritative for live admission" rule).
- `broker.connection_state` — connection lifecycle audit: connects, disconnects, heartbeat gaps, resubscription events (consumed by Stage 7's Green/Amber/Red controller as a Red-state trigger when the authoritative source is unreachable, §4 Rule 6).
- `broker.submission_log` — every submitted order keyed by `client_order_token`; duplicate submissions with the same token are idempotent no-ops returning the original result; maps `client_order_token → api_order_id → broker_perm_id` as the broker assigns them.
- `broker.submission_attempts` — per-submission operational detail (attempt count, retry reason, final outcome) so live order retries are debuggable.
- `broker.adapter_conformance_report` — round-trip certification that `test.synthetic_broker` produces events semantically equivalent to live-adapter normalization under the canonical serializer (same logical event set, same correction-group assignments, same identity mappings). Required artifact for Gate 1.

**Required interface contract produced here (see cross-stage contracts)**
- `OrderSubmissionGateway.submit(request, client_order_token) → SubmissionAck` — idempotent submission; Stage 10 is the sole caller in v1; `governance.exceptions`-backed emergency overrides flow through the same gateway with the exception reference attached.

**Success criteria / executive result**
Reconnects and re-polls are idempotent (the same raw payload never creates duplicate normalized events); order submission is idempotent under `client_order_token`; all raw bytes are content-addressed and immutable; `permId=0` and correction callbacks are explicitly represented as first-class objects; TWS, Trade Confirmation Flex, and Activity Flex are preserved as three separate truth channels with no silent merging; unreachable authoritative source surfaces as a typed state Stage 7 can respond to; synthetic-broker semantic equivalence is certified under the canonical serializer, not assumed; polling watermarks and submission attempts make live operation debuggable. Executive result: a clean source boundary that later replay and reconciliation can trust without knowing the shape of IB's wire protocol, and a single idempotent order-submission path.

---

### Stage 5 — Core economic kernel: journal and execution-event append gateways, canonical identity (incl. fallback), correction semantics, derived read models

**Goal / responsibility**
Build the economic source of truth and the causal event path, with correction-safe identity semantics including the §10 fallback-identity path for `permId=0` activity. This is the **sole commit authority** for `JournalStore` and `ExecutionEventStore` — every other stage that needs to post economic or causal state submits a typed append request through Stage 5's gateways. This enforces the idea's §10 Rule 4 "Journal is sole economic truth" structurally. Resolves fallback identity against governance exceptions so every `permId=0` event traces to an authorising record. Produces derived read models so downstream readers do not reconstruct state themselves.

**Reads**
- `schema_registry.*` / `contracts.canonical_serialization` / `evidence.signing_primitives` / `clock.calendars` (Stage 2).
- Typed append requests from Stages 4, 8, 9, 10, 12 via `JournalPostingGateway.append(...)` and `ExecutionEventAppender.append(...)`.
- `governance.exceptions` (Stage 1) — for resolving `origin="non_api"` fallback-identity events to their authorising exception record (emergency manual repricing, explicit broker-side adjustments).
- `test.synthetic_broker` (Stage 2) — for deterministic test coverage, including synthetic correction groups and synthetic fallback-identity events.

**Writes**
- `ledger.journal_postings` — append-only canonical accounting journal; fills, commissions, venue/clearing/regulatory fees, cash movements, distributions, settlement releases, broker corrections; tax cashflows post here under **distinct account codes** via Stage 9's submissions. Stage 5 is the sole committer.
- `ledger.account_codes` — the enumerated journal account-code taxonomy (economic cash, reserved cash, commissions, venue fees, tax withholding, tax distribution, bridge adjustments, etc.).
- `ledger.execution_events` — immutable causal path (strategy intent, admission decision, order submission, broker ack, partial fill, cancel, reject, expiry, correction, restatement). Stage 5 is the sole committer.
- `ledger.identity_links` — mapping `(api_order_id ↔ broker_perm_id ↔ exec_id ↔ commission_report_id ↔ statement_posting_id ↔ client_order_token)`; `broker_perm_id` is canonical when populated, `client_order_token` provides the bridge back to Stage 10's strategy intent for idempotency reconciliation.
- `ledger.correction_groups` — `exec_correction_group_id` groups; prior events in a group are never mutated, only superseded.
- `ledger.fallback_identity_links` — composite-identity records used only when `broker_perm_id = 0 or absent` (§10): `(account_id, trade_date, contract_id, side, cumulative_qty, avg_fill_price, statement_posting_id)`; every such record carries `origin="non_api"` and links to **either** a `governance.exceptions` record (manual override) **or** a broker-posted statement entry (broker-side adjustment, no exception required but logged); non-API volume is surfaced as a governance signal at reconciliation.
- `portfolio.position_views` — derived read-only position state reconstructed from the canonical journal under the canonical ordering key. Downstream readers (Stage 11 holdings, Stage 7 reconciliation, Stage 12 gate packs) consume this rather than reconstructing position state themselves.
- `portfolio.cash_views_raw` — derived read-only economic cash state from journal postings, for Stage 8 to layer its operational cash-state decomposition on top of (without reconstructing journal state).

**Required interface contracts produced here (see cross-stage contracts)**
- `JournalPostingGateway.append(posting_batch, source_ref) → CommitAck` — **sole** write path to `JournalStore`. Source-ref maps each posting to the originating stage and supporting artifact (broker payload id, tax reconciliation id, etc.) so provenance is inspectable.
- `ExecutionEventAppender.append(event_batch, source_ref) → CommitAck` — **sole** write path to `ExecutionEventStore`. Supports supersession-on-correction semantics; prior events are never mutated.
- `CanonicalOrderingKey(event) = (coalesce(broker_perm_id, fallback_identity), exec_correction_group_id)` — the single canonical ordering key used by Stage 7 replay.

**Success criteria / executive result**
Every journal posting and every execution event enters the system through one of Stage 5's two gateways; no other stage mutates ledger truth directly; journal is append-only; execution events never mutate; `broker_perm_id` is canonical when present; every fallback-identity event resolves to either a signed exception or a broker posting and is auditable end-to-end; corrections supersede within groups rather than rewriting history; the canonical ordering key is a single pure function any downstream stage can call; derived read models are reproducible from the canonical stores. Executive result: downstream stages consume one economic truth and one causal truth via append gateways, with structurally enforced single-writer discipline and no ambiguity about which event is "authoritative" inside a correction group.

---

### Stage 6 — Minimum-scope dated reference data (including settlement), evidence registry, as-of snapshot binder, universe, and compliance

**Goal / responsibility**
Provide the dated evidence and reference-data layer needed for the live universe, at minimum v1 scope (2–4 ISINs, 1 funded typical), built in parallel with Stage 5. Owns **all** effective-dated reference data including settlement regime entries and broker settlement policy — Stage 8 operates on these references but does not own them. Owns the compliance decision store (§8) and the universe admission decisions (§7) including the ISIN-level XLM admission check. Owns the evidence-ingestion reviewer workflow (calls Stage 1's `governance.signing_workflow` for every signed entry). Owns the `AsOfSnapshotBinder` service used by Stage 10 and Stage 11 to create immutable live-decision and simulator-run bindings.

**Reads**
- External documents: KIDs, prospectuses, issuer tax-transparency publications, BaFin database snapshots, BMF publications (Basiszins), Deutsche Börse XLM publications, broker contract metadata, broker settlement policy documents.
- `evidence.artifacts` / `evidence.signing_primitives` / `evidence.audit_log` / `marketdata.decision_snapshots` / `marketdata.research_series_raw` (Stage 2).
- `governance.production_profile` / `governance.deny_list` / `governance.reviewer_roster` / `governance.parameters` / `governance.parameter_snapshots` (Stage 1) — specifically `xlm_admission_threshold` and `xlm_deterioration_threshold`.
- `governance.signing_workflow` (Stage 1) — every signed reviewer decision routes through this path.
- Manual reviewer ingestion — documents are entered through a signed reviewer workflow; this stage owns the ingestion UI/CLI.

**Writes**
- `refdata.values` — all effective-dated reference data from §14: supported live execution profile, trading calendar references, **settlement regime entries (§11, v1 ships only the currently active regime but preserves the regime-id abstraction)**, **broker settlement policy** (the dated document that Stage 8 forecasts against), commission schedule, third-party venue fees, order class / session profile, instrument eligibility state, tax classification state, Teilfreistellung regime, Basiszins inputs (from BMF), corporate-action rules. **Sole owner** of settlement reference data — Stage 8 reads these and never mirrors them.
- `refdata.snapshots` — derived immutable snapshots of `refdata.values` keyed by `refdata_snapshot_id`; every live decision and simulator run binds to one (via `AsOfSnapshotBinder`).
- `evidence.registry` — per §14: source URI or document identity, retrieval timestamp, document hash (resolved against `evidence.artifacts`), parser/version, extracted fields, stated validity interval, reviewer status. Every statutory tax entry and every compliance line entry resolves against a registry record.
- `evidence.fund_level_records` — deduplicated fund-level evidence (classification, KID validity interval, retail marketability, tax-transparency publication) referenced by each listing-line record (§8).
- `evidence.stale_alerts` — stale-evidence detection: records whose validity interval has lapsed or whose source document has been superseded; **emitted to Stage 2's `obs.alerts` bus under Stage 7's alert-policy taxonomy**, consumed by Stage 10 admission to freeze affected tradable lines. Stage 6 emits; Stage 7 defines the policy.
- `snapshots.asof_bindings` — immutable bindings for every live decision and sim run: `market_data_snapshot_id` (decision) or `market_data_series_id` (sim), `refdata_snapshot_id`, `parameter_snapshot_id`, instrument-evidence snapshot, session profile, fee profile, settlement regime id, canonical identity namespace version, canonical serialization version (§14).
- `universe.research` — the 5–10 ETF research set with baseline structural requirements (§7).
- `universe.live` — the strict 2–4 ISIN subset with signed entry decisions.
- `universe.xlm_admission_decisions` — ISIN-level XLM admission check at the nearest standardized size, with the threshold read from `governance.parameters`; also tracks sustained XLM deterioration for manual-review triggers (§7, §13). XLM does **not** freeze any live tradable line automatically; sustained deterioration emits a manual-review alert via Stage 7's alert-policy taxonomy.
- `compliance.tradable_line_decisions` — per §8, tradable-line-level decision records keyed by `(ISIN, listing venue, trading currency, broker contract metadata, effective date)`, each referencing a `evidence.fund_level_records` row and listing-line-level evidence; includes signed reviewer identity (signed via Stage 1's signing workflow).
- `compliance.freeze_state` — per-tradable-line freeze state (fresh / stale / contradictory / admin-frozen); freezing a fund-level record freezes all tradable lines for that fund.

**Required interface contract produced here (see cross-stage contracts)**
- `AsOfSnapshotBinder.create(decision_kind, snapshot_components) → asof_snapshot_id` — service called by Stage 10 (at live admission) and Stage 11 (at simulator-run start) to create an immutable binding over all relevant snapshot ids. Stage 6 owns the service; other stages do not mint snapshot ids themselves.

**Success criteria / executive result**
All lookups are effective-dated; all settlement reference data lives in one place (no duplicate encoding in Stage 8); every live tradable line has signed fund-level and line-level evidence with the fund-level record referenced once; stale or contradictory evidence freezes the affected line (and, if fund-level, every line under that fund); snapshots bind live decisions and sim runs immutably including market-data, refdata, and parameter snapshot ids via one binder service; XLM enters only as a universe admission check and manual-review trigger, never as an automatic live freeze; every reviewer signature uses Stage 1's shared signing workflow. Executive result: decisions become reproducible and legally/tax/compliance-defensible without overbuilding v2 scope, and compliance can answer "why was this line admissible on date D" by dereferencing one `asof_snapshot_id`.

---

### Stage 7 — Replay, reconciliation, residual engine, Green/Amber/Red controller, alert policies, investigation cases, and freeze broadcast

**Goal / responsibility**
Turn the archived broker stream and kernel postings into replayable, reconcilable economic state with explicit divergence-state handling per §4 Rule 6, and own the kernel-wide alert-policy taxonomy (Red-state freezes, stale-evidence freezes, re-submit-budget exhaustion, XLM sustained-deterioration manual review, connection-loss escalation, non-API volume spike, reconciliation-grace-window expiration). Broadcasts freeze scope via `ChannelStateView` with an enumerated `freeze_scope` so downstream admission stages (Stage 8 cash-leg, Stage 10 full admission, Stage 9 annual close) enforce it structurally. Owns reconciliation-investigation case management so every Red-state event is human-investigable without ad hoc workflows.

**Reads**
- `broker.raw_payloads` / `broker.truth_projection` / `broker.connection_state` / `broker.poll_watermarks` (Stage 4).
- `ledger.journal_postings` / `ledger.execution_events` / `ledger.correction_groups` / `ledger.fallback_identity_links` / `portfolio.position_views` (Stage 5).
- `snapshots.asof_bindings` (Stage 6).
- `governance.parameters` / `governance.parameter_snapshots` — specifically the per-channel `tolerated_divergence_band` and `reconciliation_grace_window` (Stage 1).
- `evidence.stale_alerts` (Stage 6).
- `CanonicalOrderingKey` (Stage 5, library import) — for replay ordering.
- `contracts.canonical_serialization` (Stage 2) — replay uses canonical semantic equivalence (§10 Rule 4), not byte identity.

**Writes**
- `replay.run_results` — replay output under the canonical ordering key from Stage 5, evaluated under **canonical semantic equivalence** (§10 Rule 4); replay reproduces invariant position/cash/fee/tax state under the canonical ordering key.
- `recon.position_views` / `recon.cash_views` / `recon.fee_views` / `recon.tax_views` — derived reconciliation views per channel.
- `recon.residuals` — per-channel residuals with source-pair identification (e.g., TWS-vs-Activity-Flex delta for cash on a given date).
- `recon.investigation_cases` — case-management artifacts for Red-state events: case id, linked Red event, freeze scope, investigation owner, hypotheses, evidence references, close rationale. Closure requires a signed `governance.exceptions` record of kind `reconciliation-break-manual-close`; Stage 7 does not close cases without it.
- `ops.channel_states` — Green/Amber/Red state per reconciliation channel (§4 Rule 6) with state-transition audit; Amber-band rule applies the more conservative of divergent values as the operative input, Red freezes admission on the affected channel.
- `ops.reconciliation_break_events` — Red-state events with **enumerated `freeze_scope`** (e.g., `cash_channel_account`, `tradable_line`, `tax_channel`, `all_admission`), investigation-case link, clear-conditions. Clear requires a signed governance exception via Stage 1's signing workflow.
- `ops.alert_policies` — typed alert definitions consumed by later stages and by Stage 2's `obs.alerts` bus: Red-state freeze, re-submit-budget exhaustion (from Stage 10), XLM sustained-deterioration review (from Stage 6), stale-KID / stale-classification freeze (from Stage 6), broker connection unreachable (from Stage 4), non-API volume spike (from Stage 5 fallback-identity records), reconciliation-grace-window expiration.

**Required interface contract produced here (see cross-stage contracts)**
- `ChannelStateView(channel, as_of) → {Green | Amber(conservative_value) | Red(freeze_scope ∈ enum)}` — the broadcast state read by Stage 8 and Stage 10 admission, Stage 9 annual-close gating, and Stage 12 gate packs.

**Success criteria / executive result**
Replay reproduces invariant position/cash/fee/tax state under the canonical ordering key using canonical semantic equivalence (not byte identity); explained timing differences stay Amber inside the grace window and recover; unexplained residuals go Red and freeze the affected channel with an enumerated scope; Amber is a normal operating state, Red is not; every Red-state event has an enumerated freeze scope that downstream admission stages enforce structurally and an investigation case that is human-investigable upstream; alert policies exist as one coherent artifact, not as scattered prose. Executive result: Gate 1 can rely on economic-state invariance and explicit divergence handling instead of brittle byte-level replay or silent balance adjustments, and freezes propagate to admission without any stage implementing its own freeze-detection logic.

---

### Stage 8 — Cash-state decomposition, settlement forecasting and validation, reservations, and cash-leg admission

**Goal / responsibility**
Model the cash account correctly at runtime and expose a deterministic cash-admission service where broker-reported cash is authoritative and the local settlement engine is predictive/reconciliation only (§11). Expose `ReservationStore` as a service (with enumerated release reasons and expiry) between cash control and Stage 10's full economic admission. Enforces cash-channel freezes broadcast via `ChannelStateView`. **Does not own settlement reference data** — that lives in Stage 6's `refdata.values`; Stage 8 reads dated settlement policy and forecasts against it.

**Reads**
- `broker.account_snapshots` (Stage 4) — **authoritative** cash input for live admission.
- `ChannelStateView(cash_channel_account, as_of)` (Stage 7) — Red on the cash channel blocks admission regardless of projection; Amber uses the conservative value as operative.
- `ledger.journal_postings` / `ledger.execution_events` / `portfolio.cash_views_raw` (Stage 5).
- `refdata.values` / `refdata.snapshots` (Stage 6) — currently active settlement regime and broker settlement policy; Stage 8 does not mirror these.
- `compliance.tradable_line_decisions` / `compliance.freeze_state` (Stage 6).
- `governance.parameters` / `governance.parameter_snapshots` — `cash_buffer(t)` sizing rule, tolerated divergence band on the cash channel.
- `TaxPostingProjection` (Stage 9 contract) — forward-looking expected annual tax posting stream used in `cash_buffer(t)` sizing (§6). Ships with a **conservative placeholder** at Stage 8 delivery (upper-bound heuristic based on `governance.parameters` placeholder values) so Stage 8 can complete before Stage 9; the placeholder usage is flagged on each `cash.state_view` read and is gate-ineligible the same way placeholder parameter usage is.

**Writes**
- `cash.state_view` — trade-date cash, settled cash, reserved cash, withdrawable cash (§11); the four decomposition categories are never mixed.
- `settlement.forecast_runs` — runs of forecasted settlement dates for pending fills under the current regime from `refdata.values`; feeds Stage 7 reconciliation; never overrides broker authority.
- `settlement.validation_runs` — back-validations of forecasted-vs-observed settlement postings against broker-reported cash; surfaces forecast drift.
- `risk.reservations` — reservation records (managed by the `ReservationStore` service below); fields include `reservation_id`, dedupe key, reserve/release state, release reason code ∈ enumerated set per §11 (`terminal_cancel`, `terminal_reject`, `terminal_expiry`, `full_fill`, `explicit_qty_reduction`, `recon_driven_correction`), expiry timestamp. Reservation lifecycle is reconstructible from order/event flow, not manually inferred.
- `risk.cash_gate_decisions` — per-order cash-leg admission decisions (pre-emptive of Stage 10 full admission); rejects all when cash channel is Red, uses conservative value when Amber.

**Stage 8 submissions to Stage 5 gateway**
- Settlement releases and any cash-state postings that need to enter the journal are submitted via `JournalPostingGateway.append(...)`. Stage 8 does not write to `ledger.journal_postings` directly.

**Required interface contract produced here (see cross-stage contracts)**
- `ReservationStore` service: `reserve(order_ref, amount, dedupe_key) → reservation_id`, `release(reservation_id, reason ∈ enum) → ReleaseAck`, `outstanding(as_of) → [Reservation]` — the boundary between reservations and Stage 10's full economic admission.

**Success criteria / executive result**
Cash is decomposed into the four §11 categories and they are never mixed; settlement reference data is read from Stage 6, not duplicated; current-regime forecasting matches broker postings within the tolerated divergence band, surfaced by `settlement.validation_runs`; broker-insufficient cash always blocks buys even if the local engine disagrees; reserved cash is operational state only with enumerated release reasons, released only on the allowed lifecycle events; the `cash_buffer` sizing rule honours the larger of (floor, tax-posting upper bound from `TaxPostingProjection`) with a signed exception path if it is overridden; cash-channel Red-state propagates from Stage 7 into hard admission blocks without Stage 8 re-implementing freeze detection. Executive result: cash-account admissibility becomes broker-first and auditable, reservation lifecycle is reconstructible, and Stage 10 can consume one `ReservationStore` service rather than reimplement settlement.

---

### Stage 9 — German tax subsystem: Ledger A, Ledger B, precedence, annual close, retrospective changes, and tax-posting projection

**Goal / responsibility**
Implement the v1 tax model as two explicit ledgers plus annual reconciliation, at deliberately minimal event-cadence operation with reviewer-backed statutory state (§9). Owns the authority-precedence resolver and the retrospective-change handler. Produces the `TaxPostingProjection` contract Stage 8 consumes for `cash_buffer` sizing. Blocks annual close on open tax exceptions or unresolved Red-state tax-channel events. **All bridge-adjustment and close-adjustment postings to the journal are submitted via Stage 5's `JournalPostingGateway.append(...)`** — Stage 9 does not write to `ledger.journal_postings` directly.

**Reads**
- `ledger.journal_postings` — specifically the broker-posted tax cashflows under the distinct tax account codes from Stage 5.
- `ledger.correction_groups` — retrospective broker restatements post under the correction-group mechanism; prior-year exceptions are auto-opened.
- `broker.flex_raw_pulls` — for activity-statement tax line items.
- `evidence.registry` / `evidence.fund_level_records` — for authoritative issuer and BMF sources.
- `refdata.values` / `refdata.snapshots` — Teilfreistellung regime dates, Basiszins values.
- `governance.exceptions` / `governance.reviewer_roster` / `governance.signing_workflow` — reviewer sign-off workflow.
- `ChannelStateView(tax_channel, as_of)` (Stage 7) — Red on the tax channel blocks annual close.

**Writes**
- `tax.ledger_a` — statutory tax state ledger (§9.A): dated fund classification, Teilfreistellung regime, candidate Vorabpauschale state with Basiszins input used, deemed sale / deemed repurchase state under §22 InvStG (explicit, manually approved via Stage 1 signing workflow), tax lots. Every entry carries an evidence-registry pointer (§9 data-provenance requirement); entries without a resolved pointer are rejected.
- `tax.ledger_b` — broker-posted tax cashflows as booked (derived from `ledger.journal_postings` under the tax account codes).
- `tax.authority_precedence_resolutions` — per §9 precedence: issuer legal docs authoritative for Ledger A classification; BMF authoritative for Basiszins; broker classification is corroborating only; disagreements generate disagreement-exception records.
- `tax.exceptions` — tax exception records under the §9 cross-ledger disagreement workflow: statutory state, posted cashflow set, unexplained delta, candidate hypotheses, reviewer assignment. Open tax exceptions block annual close. Persisted via `governance.exceptions` with `kind=tax_exception`.
- `tax.bridge_adjustments` — explicit statutory-vs-cashflow timing-difference bridge entries; the corresponding journal postings are submitted via `JournalPostingGateway.append(...)`.
- `tax.retrospective_changes` — retrospective broker restatements (Ledger B-driven, identified via Stage 5's correction groups) and retrospective issuer changes (Ledger A-driven) logged with effective-date semantics.
- `tax.reopened_years` — explicit artifact for prior-year re-opens: which fiscal year, which change triggered the re-open, which downstream artifacts (annual reconciliations) are now re-opened, without mutating the prior close artifacts (§9).
- `tax.annual_reconciliations` — per-fiscal-year artifact enforcing `statutory_tax_delta(year) = Σ broker_posted_tax_cashflows(year) + Σ explicit_bridge_adjustments(year)`; unexplained residuals block year-end close.
- `tax.year_close_artifacts` — signed annual close artifacts.

**Required interface contract produced here (see cross-stage contracts)**
- `TaxPostingProjection(as_of) → AnnualTaxStream` — forward-looking expected annual tax posting stream used by Stage 8 in `cash_buffer(t)` sizing: expected Vorabpauschale withholdings, distribution-tax postings, worst-case §22 deemed-sale/deemed-repurchase cash effect per live tax lot whose regime could transition within the year. Until Stage 9 ships, Stage 8 consumes a conservative placeholder.

**Success criteria / executive result**
Every statutory entry has evidence-registry provenance and is rejected without one; broker-posted tax cashflows remain separate from statutory state under distinct account codes; the precedence resolver produces a single answer for every authority disagreement and logs the disagreement as an exception; unresolved cross-ledger residuals block close; retrospective broker or issuer changes appear explicitly in `tax.reopened_years` and reopen affected reconciliations without mutating prior close artifacts; every journal posting from Stage 9 flows through Stage 5's gateway; `TaxPostingProjection` drives a correctly sized `cash_buffer`. Executive result: the system is tax-defensible in the narrow v1 sense, the prior close immutability rule is structurally enforced, and `cash_buffer` / capital policy can be updated from tax reality rather than from convenience.

---

### Stage 10 — Fee model, ticket-size-native liquidity gate, deterministic execution policy, admission kernel library, full economic admission, and Gate-2 closure

**Goal / responsibility**
Build the cost stack, the deterministic order-construction policy (§13 limit-price construction), fee/slippage exemplar capture, the re-submit budget, the emergency-override entry point, and the final trade-admission engine. Ship three pure libraries — `AdmissionKernel`, `FeeModel`, `ExecutionPolicy` — that Stage 11's canonical simulator imports verbatim, so no duplicate runtime logic exists and the §15 simulator-validity rule is structurally guaranteed. **Purity discipline:** all three libraries take explicit inputs; they do not reach into stores. **Gate 2 closes here**, on the pinned route only, once §6 governance parameters are pinned by Stage 1 and observed live exemplars meet §12 closure criteria across the ticket-size distribution.

**Phase A (pre-Gate-2, pre-certification):** Ship the three libraries plus the live admission path. `StrategyCertificationStatus` defaults to `none`, so only Tier-0 plumbing trades admit. Collect fee and slippage exemplars. Close Gate 2.

**Phase B (post-Gate-2, post-certification):** Consume Stage 11's `StrategyCertificationStatus` to admit non-Tier-0 live rotation trades from certified strategies.

**Reads**
- `cash.state_view` / `ReservationStore` (Stage 8).
- `ChannelStateView` / `ops.alert_policies` (Stage 7) — Red on tradable-line channel or cash channel blocks admission; Red on any relevant channel propagates.
- `ledger.journal_postings` / `ledger.execution_events` / `portfolio.position_views` (Stage 5).
- `tax.ledger_a` — for tax-friction component of `Ĉ_RT`.
- `governance.parameters` / `governance.parameter_snapshots` — `b_1w`, `R(r,t)` composition, `B_model`, `B_ops`, per-decision re-submit cap, churn-budget window and cap, fill-model calibration parameters, Gate-2 closure tolerances. Reads the pinned/placeholder status and binds each admission decision to a specific `parameter_snapshot_id`.
- `refdata.values` / `refdata.snapshots` — fee schedules, session / order class profiles, settlement regime (read-only; Stage 6 owns).
- `universe.xlm_admission_decisions` — XLM feeds the conservative reserve `R`, never a per-ticket gate.
- `compliance.tradable_line_decisions` / `compliance.freeze_state` — no admission on frozen lines.
- `governance.exceptions` — emergency manual override entry authorisation; every override must reference an open signed exception.
- `marketdata.decision_snapshots` (Stage 2) — top-of-book at decision timestamp for limit-price construction.
- `AsOfSnapshotBinder.create(...)` (Stage 6 service) — bind each live admission decision to an immutable `asof_snapshot_id`.
- `StrategyCertificationStatus(strategy_id, as_of)` — **read-only contract** written by Stage 11; a strategy without a current Gate-3 certification may emit only Tier-0 plumbing trades (§6 rule 5). Ships with a "no strategy certified" default so Stage 10 Phase A can complete before Stage 11.

**Writes**
- `execution.fee_tables` — profile-specific effective-dated fee model (§12).
- `execution.fee_exemplars` — observed live exemplars for every fee dimension the pinned production profile touches (§12); required for Gate 2. Covers the ticket-size distribution (near `N_min`, mid, upper bound).
- `execution.slippage_exemplars` — realised slippage anchored to live fills at operational ticket sizes (§13).
- `execution.fill_model` — bootstrap or exemplar-calibrated fill model (§13) — present for telemetry and stress-overlay use only; **not** an admission-scalar input in v1. Calibration uses `governance.parameters` fill-model calibration parameters.
- `execution.resubmit_budget_state` — per-decision and per-rolling-window re-submit budget state; exceeding the budget emits an alert via Stage 7's alert-policy taxonomy and freezes further live admission pending review.
- `execution.emergency_overrides` — signed manual-override events (§13) that reference an open `governance.exceptions` record and mark the resulting order submission with an override flag so Stage 5's fallback-identity resolution can link back.
- `risk.n_min_aum_min` — derived `N_min(r, t)` and `AUM_min(k, r, t)` at current parameter state (§6).
- `risk.trade_admission_decisions` — per-trade admission records recording: `asof_snapshot_id`, point-estimate fill-conditional `E[Δα | fill]`, full modelled `Ĉ_RT` (including worst-case re-submit commission under the cap), `B_model`, `B_ops`, ticket-size gate result, tier check, churn-budget check, strategy certification status, the telemetry `p_fill` from the fill model (recorded but not in the admission scalar), pinned-vs-placeholder parameter flags, and admit/reject decision.
- `evidence.gate2_closure_pack` — the Gate-2 closure evidence: exemplar coverage per fee dimension vs `minimum_exemplar_coverage` governance parameter, modelled-vs-posted-broker-charge residuals within `gate2_fee_tolerance`, realised-slippage-vs-conservative-model fit within `gate2_slippage_tolerance`, realised-fill-rate-vs-bootstrap fit within `gate2_fill_rate_tolerance`. Packaged for Stage 12's gate-pack assembly.

**Stage 10 submissions to Stage 5 gateway**
- Admission decisions, order-intent events, and override events are submitted via `ExecutionEventAppender.append(...)`. Stage 10 does not write to `ledger.execution_events` directly.
- Fee and commission postings arising from fills are ultimately posted by Stage 4 normalization through Stage 5's journal gateway; Stage 10 owns the fee model, not the posting.

**Order submission path**
- Calls `OrderSubmissionGateway.submit(request, client_order_token)` (Stage 4) as the sole live-order caller. Passes the `governance.exceptions` reference for emergency overrides.

**Required interface contracts produced here (see cross-stage contracts)**
- `AdmissionKernel(intent, asof_snapshot, cash_view, reservation_view, channel_states, parameter_snapshot, compliance_state, strategy_cert, tier_state, churn_window_state, resubmit_window_state, execution_policy_version) → Decision` — pure, side-effect-free. All inputs are passed explicitly; the kernel does not reach into any store. Stage 11 imports this verbatim so the canonical simulator cannot admit an order live v1 would reject (§15 validity rule).
- `FeeModel.evaluate(order, profile, asof_snapshot) → FeeBreakdown` — pure, used by both Stage 10 live admission and Stage 11 simulation.
- `ExecutionPolicy` (library): deterministic limit-price construction rules (§13) — anchor to live top-of-book at decision timestamp (resolved against a `market_data_snapshot_id`), spread-derived aggression cap, single-shot cancel/re-submit policy, no discretionary repricing. Versioned as `execution.limit_price_policy_versions`; Stage 11 imports this verbatim so simulator order construction matches live.

**Success criteria / executive result**
Fee model is profile-specific and conservative under ambiguity; XLM is never a per-order gate; `ExecutionPolicy` is a pure library with deterministic limit-price construction that Stage 11 can run without live-store access; re-submit budget is priced into `Ĉ_RT` and audited against its rolling-window cap; per-trade admission remains fill-conditional and excludes `p_fill` from the admission scalar; every admission decision binds to a single `asof_snapshot_id` that references `parameter_snapshot_id`, `refdata_snapshot_id`, and `market_data_snapshot_id`; emergency overrides are only reachable via a signed `governance.exceptions` record and flow through the idempotent `OrderSubmissionGateway`; `AdmissionKernel`, `FeeModel`, and `ExecutionPolicy` are importable by Stage 11 as pure functions with no live-store dependency; Gate 2 is **closure on observed exemplars** for the pinned profile only. Executive result: either the pinned live profile becomes economically supported (Gate-2 closure pack signed) and non-Tier-0 rotation admits from certified strategies, or it remains Tier-0 / narrow-success or is formally re-pinned, with no drift between simulated and live admission behaviour.

---

### Stage 11 — Alpha protocol, canonical simulator, scenario overlays, Gate-3 certification, certification dossiers, and admission conformance testing

**Goal / responsibility**
Implement the threshold-first, low-turnover, ETF-native alpha (§13), the single canonical simulator bound to the pinned live profile (§15), the bounded scenario-overlay set, and off-platform Gate-3 strategy certification with reproducible certification dossiers. The canonical simulator imports `AdmissionKernel`, `FeeModel`, and `ExecutionPolicy` from Stage 10 verbatim so no duplicate runtime logic exists. Jointly with Stage 10, produces the `AdmissionConformanceReport` required by Gate 1 under **canonical semantic equivalence** (§10 Rule 4 — not byte identity).

**Reads**
- `AsOfSnapshotBinder.create(...)` (Stage 6 service) — bind every simulator run to an immutable `asof_snapshot_id`.
- `snapshots.asof_bindings` (Stage 6).
- `universe.research` / `universe.live` / `universe.xlm_admission_decisions` (Stage 6).
- `portfolio.position_views` (Stage 5) — holdings state for threshold-first rebalancing decisions (read from derived view, not reconstructed).
- `refdata.values` / `refdata.snapshots` — dated metadata and the currently active settlement regime.
- `tax.ledger_a` — dated tax state for post-tax return computation.
- `AdmissionKernel` / `FeeModel` / `ExecutionPolicy` (library imports from Stage 10) — live admission, fee, and order-construction logic used verbatim.
- `execution.fill_model` / `execution.fee_exemplars` / `execution.slippage_exemplars` (Stage 10) — bootstrap or exemplar-calibrated fill model, exemplars for simulator realism.
- `governance.parameters` / `governance.parameter_snapshots` — all §6 parameters with their `pinned`/`placeholder` status; every run binds to a `parameter_snapshot_id`.
- `evidence.prescreen.threshold` (Stage 3) — Gate-3 certification may not use a required effect size looser than the pre-screen threshold (§16).
- `governance.gate_state` (Stage 1) — Gate-3 certification may not be issued while Gate 2 is not closed (§17 ordering constraint).
- `marketdata.research_series_raw` / `marketdata.research_series_views` (Stage 2) — historical market data for horizon-based simulation. Every canonical run binds to a `market_data_series_id`.
- `contracts.canonical_serialization` (Stage 2) — for semantic conformance evidence.

**Writes**
- `research.strategy_intents` — strategy definitions (feature set, ranking rule, rebalance trigger, decision cadence).
- `research.canonical_runs` — canonical-simulator runs tagged `simulator_run_kind = "canonical"`, with `asof_snapshot_id` including `parameter_snapshot_id`, `market_data_series_id`, `refdata_snapshot_id`, and the `pinned`/`placeholder` flag for every §6 parameter used (§15 gate-evidence rule: only all-pinned canonical runs feed Gate 2 / Gate 3 evidence).
- `research.overlay_runs` — overlay runs tagged `simulator_run_kind = "overlay:<n>"` referencing their canonical run; includes the mandatory **fill-probability stress** overlay (§15); overlay runs are research-only and are not admissible Gate evidence, with the explicit exception that the fill-probability stress overlay is a mandatory **pass** requirement for Gate 3 (§17 Gate 3).
- `research.stress_results` — results for each mandatory stress test enumerated in §15 (fee stress, spread stress, fill-probability stress, settlement-delay stress, stale-KID/classification freeze, broker-correction replay, order-partial/reject/cancel paths, re-submit-budget exhaustion, Green/Amber/Red transitions).
- `research.shadow_decision_logs` — per-decision shadow-decision records from canonical runs, supporting the strategy-level LCB calibration sample (§6 requires a calibration sample that a one-position live programme cannot produce; shadow decisions carry the statistical burden).
- `research.lcb_results` — strategy-level LCB computations of `LCB[Δα_vs_do_nothing | fill, strategy, horizon]` against `Ĉ_RT + B_model + B_ops`.
- `research.certification_dossiers` — per-certification reproducible dossier: strategy intent id, `parameter_snapshot_id`, list of canonical-run ids with their `asof_snapshot_id`s, pre-screen threshold reference, LCB result, fill-probability stress pass evidence, certification start/end dates, expiry/degrade reason, reviewer signature. This is the single artifact Gate 3 pack references.
- `governance.strategy_certifications` — Gate-3 certifications issued off-platform from pinned-parameter canonical runs after Gate 2 closes; certifications expire and degrade per §6. Issuance is blocked if `governance.gate_state` has Gate 2 not closed. Every certification points at its `research.certification_dossiers` entry.
- `research.admission_conformance_report` — the joint Stage-10/11 artifact demonstrating that canonical-simulator admission decisions on a curated test corpus match live-admission decisions under **canonical semantic equivalence** (Stage 2's canonical serializer applied to admission decision records; same logical decision, same reject/admit verdict, same `asof_snapshot` binding). Stage 10 supplies the live decision corpus; Stage 11 re-runs it through its imported `AdmissionKernel`. This is the §15 simulator-validity rule evidenced concretely; it is the structural input Stage 12 Gate-1 pack requires.

**Required interface contract produced here (see cross-stage contracts)**
- `StrategyCertificationStatus(strategy_id, as_of) → {certified | expired | none}` — read by Stage 10 admission. Ships with a `none` default at Stage 10 Phase A.

**Success criteria / executive result**
The simulator is single-mode canonical plus bounded research overlays; canonical runs share live admission, fee, and order-construction logic exactly via the imported libraries (not re-implementations), as evidenced by the `admission_conformance_report` under canonical semantic equivalence; the simulator runs without live-store access because `AdmissionKernel`/`FeeModel`/`ExecutionPolicy` take all inputs explicitly; placeholder-using runs are flagged and ineligible for gate evidence; overlay outputs are not gate evidence except the mandatory fill-probability stress pass for Gate 3; Gate-3 certification is issued only from pinned-parameter off-platform shadow evaluation after Gate 2 closes and never with a required effect size looser than the pre-screen threshold; each certification has a reproducible dossier that names exactly which immutable datasets and parameter snapshots produced it. Executive result: live rotation becomes strategy-certified rather than ad hoc, and "the simulator admits only what live would admit" is a structural guarantee evidenced by a reproducible conformance artifact rather than a review convention.

---

### Stage 12 — Paper trading, Tier-0 micro-live, gate packs with reproducibility manifests, narrow-success continuation, and controlled scale-up

**Goal / responsibility**
Exercise the full production path in paper, operate the proving account under Tier 0, produce signed evidence packs per §17 each accompanied by a reproducibility manifest, and manage the legitimate terminal outcomes: narrow-success (Gate 1 closed, 2–4 deferred) or full-success (all four closed). Gate packs are the **sole** mechanism that flips `governance.gate_state` to closed.

**Reads**
- All prior stage outputs, especially: `replay.run_results`, `ops.channel_states`, `ops.reconciliation_break_events`, `recon.investigation_cases`, `recon.residuals`, `execution.fee_exemplars`, `execution.slippage_exemplars`, `risk.trade_admission_decisions`, `execution.resubmit_budget_state`, `evidence.gate2_closure_pack`, `governance.strategy_certifications`, `research.certification_dossiers`, `research.stress_results`, `research.admission_conformance_report`, `broker.adapter_conformance_report`.
- `evidence.prescreen.verdict` (Stage 3) — Gate 1 requires pre-screen pass or a signed waiver (§17 Gate 1).
- `governance.gate_state` / `governance.tier_state` / `governance.exceptions` / `governance.signing_workflow` / `governance.parameter_snapshots` (Stage 1).
- `contracts.canonical_serialization` / `schema_registry.*` (Stage 2) — for reproducibility manifests.

**Stage 12 submissions to Stage 5 gateway**
- Any journal postings needed for paper-to-live transition bookkeeping (Tier-0 exception bookings, etc.) are submitted via `JournalPostingGateway.append(...)`.

**Writes**
- `ops.paper_run_logs` — Phase B paper trading output; paper trading proves path consistency and failure handling, not alpha (§16 Phase B).
- `ops.tier0_runbook` — live operating procedures for the proving account (§16 Phase C): max 1 funded position, no same-day rotation, no extended-session, no route changes, no order modification except signed manual exception, fee-closure collection under way.
- `ops.tier0_exception_log` — the plumbing-only exceptions permitted at Tier 0, each linked to `governance.exceptions`.
- `evidence.reproducibility_manifests` — per-gate-pack manifest listing: exact artifact refs, schema versions, `canonical_serialization` version, `parameter_snapshot_id`s, `refdata_snapshot_id`s, `market_data_snapshot_id`s / `market_data_series_id`s, library versions for `AdmissionKernel`/`FeeModel`/`ExecutionPolicy`, unresolved-issue register. Attached to every gate pack.
- `evidence.gate_packs` — one pack per gate, each signed via `governance.signing_workflow` and each carrying a reproducibility manifest:
  - **Gate 1** pack references: `evidence.prescreen.verdict` (pass or signed waiver), `recon.residuals` explained within Amber band or cleared via closed investigation cases, `governance.production_profile` with signed pinning rationale, `compliance.tradable_line_decisions` for every live tradable line, `research.admission_conformance_report` (simulator/live admission identity under canonical semantic equivalence), `replay.run_results` including correction-group replay, `broker.adapter_conformance_report`.
  - **Gate 2** pack references: `governance.parameters` fully pinned (all §6 parameters plus Gate-2 closure tolerances), `evidence.gate2_closure_pack` (produced by Stage 10), exemplar coverage, fill-rate tracking within bootstrap expectation.
  - **Gate 3** pack references: `research.certification_dossiers` for each certified strategy, `research.lcb_results` on pinned-parameter canonical runs post-Gate-2, fill-probability stress overlay pass, no single-regime dependency, ordering constraint satisfied.
  - **Gate 4** pack references: `AUM_min(k, r, t)` satisfaction, `cash_buffer` coverage of `TaxPostingProjection` and fee-misclassification reserve, observed live ticket sizes consistent with `N_min`.
- `governance.continuation_programme` — for narrow-success: the signed deferred-gates continuation plan (capital-scale-up conditions, exemplar accumulation targets, certification timelines), per §1 "Narrow-success state" and §18 v2-precondition rule.
- `governance.tier_promotions` — signed Tier 0 → Tier 1 → Tier 2 promotions, gated on the relevant gates (§6, §17).

**Required interface contract produced here (see cross-stage contracts)**
- `GateEvidencePackStore` — the **sole** write ingress to `governance.gate_state` for gate-closure transitions. Every pack carries a reproducibility manifest.

**Success criteria / executive result**
Paper proves path consistency and failure handling, not alpha; Tier 0 stays within one funded position and plumbing-only exceptions with each exception tied to a signed record; each gate pack is reproducible from its manifest and signed; the reproducibility manifest means "gate passed, and anyone can re-derive the result six months later"; narrow-success is representable as Gate-1-closed plus a signed continuation programme (§1, §18); full-success requires all four packs closed in sequence per §17; no gate can be flipped closed except via a Stage-12-authored signed pack with manifest. Executive result: the programme ends in a governed, reproducible terminal state instead of an ambiguous partial rollout, and the narrow-success outcome is a first-class terminal state with a documented path forward, not a project failure.

---

## Cross-stage storage contracts (explicit from day one)

These contracts are the minimum needed to keep the stages independent and eliminate the plan's implied cycles. Each is named, single-writer, multi-reader, and has a schema-registry entry from Stage 2.

**Single-writer truth stores.** `JournalStore` and `ExecutionEventStore` have exactly one commit authority (Stage 5); all other stages submit typed append requests via the gateways. This enforces §10 Rule 4 structurally.

**Conformance discipline.** All conformance reports (`adapter_conformance_report`, `admission_conformance_report`, replay) use canonical semantic equivalence under `contracts.canonical_serialization`, not literal byte identity (§10 Rule 4 is explicit on this).

- **`RawBrokerArchive`** — immutable raw bytes + parser version + retrieval timestamp + Flex channel tag. **Written by:** Stage 4. **Read by:** Stages 7, 9, 12.

- **`JournalStore`** — sole economic truth; append-only; **single-writer**. **Commit authority:** Stage 5 via `JournalPostingGateway`. **Appenders (submit typed requests):** Stages 4, 8, 9, 10, 12. **Read by:** everywhere else, preferably via `portfolio.position_views` / `portfolio.cash_views_raw`.

- **`ExecutionEventStore`** — causal path from intent to broker outcome; immutable, supersession-on-correction; **single-writer**. **Commit authority:** Stage 5 via `ExecutionEventAppender`. **Appenders (submit typed requests):** Stages 4, 8, 10, 12. **Read by:** replay, fees, certification, evidence packs.

- **`JournalPostingGateway`** (service) — `append(posting_batch, source_ref) → CommitAck`. **Owned by:** Stage 5. **Called by:** Stages 4, 8, 9, 10, 12. Sole write path to `JournalStore`.

- **`ExecutionEventAppender`** (service) — `append(event_batch, source_ref) → CommitAck`. **Owned by:** Stage 5. **Called by:** Stages 4, 8, 10, 12. Sole write path to `ExecutionEventStore`.

- **`OrderSubmissionGateway`** (service) — `submit(request, client_order_token) → SubmissionAck`; idempotent under `client_order_token`. **Owned by:** Stage 4. **Sole caller in v1:** Stage 10. Eliminates the risk of duplicate submissions on retry and is the single choke point for live-broker writes.

- **`CanonicalOrderingKey`** (function) — `(coalesce(broker_perm_id, fallback_identity), exec_correction_group_id)`. **Produced by:** Stage 5 as a pure function. **Consumed by:** Stage 7 replay.

- **`CanonicalSerializer`** (library, contract) — deterministic serialization used by every conformance check and every gate-pack hashing step. **Owned by:** Stage 2 (`contracts.canonical_serialization`). **Consumed by:** Stages 4, 7, 10, 11, 12.

- **`MarketDataDecisionSnapshotStore`** — immutable decision-time market-data captures with `market_data_snapshot_id`. **Written by:** Stage 2. **Read by:** Stages 6 (as-of binding), 10 (decision-timestamp top-of-book).

- **`MarketDataReplayStore`** — historical research/replay market data (bars, quotes, corporate-action-adjusted series, XLM time series) with `market_data_series_id`. **Written by:** Stage 2. **Read by:** Stages 3 (pre-screen reproducibility), 11 (canonical simulator horizon).

- **`AsOfSnapshotBinder`** (service) — `create(decision_kind, snapshot_components) → asof_snapshot_id`. **Owned by:** Stage 6. **Called by:** Stages 10 (at live admission) and 11 (at simulator-run start). Other stages do not mint snapshot ids themselves.

- **`AsOfSnapshotStore`** — immutable decision/run bindings including `market_data_snapshot_id` or `market_data_series_id`, `refdata_snapshot_id`, `parameter_snapshot_id`, canonical identity namespace version, canonical serialization version (§14). **Written by:** Stage 6 (via the binder). **Read by:** Stages 7, 10, 11, 12.

- **`GovernanceParameterRegistry`** + **`GovernanceParameterSnapshots`** — every §6 parameter (including Gate-2 tolerances, `xlm_admission_threshold`, `xlm_deterioration_threshold`, fill-model calibration parameters, certification validity interval, degradation thresholds) with `{value, status ∈ {placeholder, pinned}, pinning_decision_ref?}` plus immutable snapshots keyed by `parameter_snapshot_id`. **Sole writer:** Stage 1 (via `governance.pinning_workflow`; re-pinning events route through the same workflow and produce new snapshots). **Read by:** Stages 3, 6, 7, 8, 9, 10, 11, 12 — always via `parameter_snapshot_id` for historical decisions.

- **`ChannelStateView`** — `(channel, as_of) → {Green | Amber(conservative_value) | Red(freeze_scope ∈ enum)}` where `freeze_scope ∈ {cash_channel_account, tradable_line, tax_channel, all_admission}`. **Written by:** Stage 7. **Consumed by:** Stages 8 (cash), 9 (annual close), 10 (admission), 12 (gate packs).

- **`ReservationStore`** (service) — `reserve(order_ref, amount, dedupe_key)`, `release(reservation_id, reason ∈ enum {terminal_cancel, terminal_reject, terminal_expiry, full_fill, explicit_qty_reduction, recon_driven_correction})`, `outstanding(as_of)`. **Owned by:** Stage 8. **Called by:** Stage 10. Reservation lifecycle is reconstructible from order/event flow via the typed reasons.

- **`TaxPostingProjection`** — forward-looking expected annual tax posting stream for `cash_buffer` sizing. **Written by:** Stage 9. **Read by:** Stage 8. Ships with a conservative placeholder at Stage 8 delivery so Stage 8 can complete before Stage 9; placeholder usage is flagged as gate-ineligible on each read.

- **`AdmissionKernel`** (library) — pure function implementing the §6 per-trade rule and §13 liquidity/re-submit budget logic; all inputs passed explicitly, no store access. **Owned by:** Stage 10. **Imported by:** Stage 11 canonical simulator. Enforces the §15 simulator-validity rule structurally.

- **`FeeModel`** (library) — pure fee evaluation for an order against a profile and as-of snapshot. **Owned by:** Stage 10. **Imported by:** Stage 11.

- **`ExecutionPolicy`** (library) — pure library for deterministic limit-price construction (§13) and single-shot cancel/re-submit policy. **Owned by:** Stage 10. **Imported by:** Stage 11 so simulator order construction matches live.

- **`AdmissionConformanceReport`** — jointly produced proof that `AdmissionKernel` used live and in simulator produce semantically equivalent decisions on a curated corpus under `CanonicalSerializer`. **Owned by:** Stage 11 (produces), Stage 10 (provides the live decision records). **Read by:** Stage 12 Gate-1 pack assembly. Without this contract, Gate 1's §17 "identical admission logic" criterion is not structurally evidenced.

- **`StrategyCertificationStatus`** — `(strategy_id, as_of) → {certified | expired | none}`. **Written by:** Stage 11. **Read by:** Stage 10 Phase B. Clean break between research certification and live admission; ships with `none` default for Stage 10 Phase A.

- **`GovernanceSigningWorkflow`** — shared reviewer-sign component (key binding, signature format, roster lookup, audit emission). **Owned by:** Stage 1 (built on Stage 2's `evidence.signing_primitives`). **Called by:** Stages 3, 6, 9, 10, 12. No stage implements its own signing.

- **`EvidenceRegistry`** — signed, content-addressed, effective-dated evidence records (§14). **Written by:** Stages 3, 6, 9, 10, 12. **Read by:** Stages 6, 9, 10, 11, 12.

- **`ComplianceDecisionStore`** — per-tradable-line signed decisions with fund-level evidence de-duplication (§8). **Written by:** Stage 6. **Read by:** Stages 8, 10.

- **`ExceptionStore`** — typed governance exceptions and overrides (tax exceptions, compliance freeze overrides, emergency manual repricing, cash-buffer-under-tax-bound overrides, pre-screen waivers, reconciliation-break manual close). **Sole write ingress:** Stage 1's signing workflow. **Instance sources:** Stages 3, 6, 7, 9, 10, 12 submit instance data; Stage 1 is the commit authority. **Read by:** every stage that can hit an override path.

- **`AlertPolicyStore`** — typed alert definitions. **Written by:** Stage 7. **Consumed by:** Stages 4, 5, 6, 8, 10, 12 as emitters via Stage 2's `obs.alerts` bus; ops tooling as subscribers.

- **`GateEvidencePackStore`** — the **sole** store that can close gates or promote tiers; every transition in `governance.gate_state` to `closed` requires a signed pack here accompanied by a reproducibility manifest. **Written by:** Stage 12. **Read by:** Stage 1 for state transitions; governance review.