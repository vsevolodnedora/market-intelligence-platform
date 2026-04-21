# Staged Plan (revision 11_2)

Freeze interfaces, not implementations.

This plan defines a thin shared `kernel`, a thin shared `events` module, a generic `platform_runtime`, and seven bounded domain modules. Each domain module owns state, exposes a frozen `contract.py`, and communicates only through events and query Protocols. v1 is a **modular monolith** with synchronous in-process event delivery; v2 may swap transport/runtime without touching domain contracts.

Delivery sequencing (§14 of the idea): the execution kernel — append-only journal, execution-event ledger, `broker_perm_id`-anchored identity, correction-safe replay — is built and validated **first**. Reference-data and evidence machinery are built alongside at minimum scope; governance metadata does not precede proof that a small number of live trades reconcile.

**Revision 11_2 change markers.** Changes relative to revision 11_1 are marked `[C12]` (compositional `is_live_admissible`, removing a cyclic dependency), `[C13]` (strategy-certification invalidation on route re-pin), and `[C14]` (explicit Phase-0 verdict distinct from v1 terminal state). Minor unmarked edits are documented at the end of this file under "Revision 11_2 notes".


---

## 1. Layout

```
modules/
  kernel/                  # frozen day 1, thin, zero business logic
    contract.py            # re-exports the full kernel surface
    ...                    # types, IDs, protocols, errors, enums
  events/                  # [C1] frozen day 1, zero logic, cross-module payloads only
    contract.py            # re-exports the full events surface
    ...                    # cross-module event dataclasses, StrategyIntent,
                           # AdmissionInputs, AdmissionVerdict, FillBooked,
                           # BridgeAdjustmentBooked — any event or payload
                           # referenced by two or more domain modules
  platform_runtime/
    contract.py
    ...                    # event store, bus, outbox/inbox, replay, projections
  market_policy/
    contract.py
    ...                    # reference data, evidence, compliance, governance, config
  execution/
    contract.py
    ...                    # broker IO, canonical IDs, execution-event ledger,
                           # deterministic limit-price construction
  portfolio_accounting/
    contract.py
    ...                    # journal, cash decomposition, reserved cash, settlement forecast,
                           # churn-budget, turnover windows
  admission_control/
    contract.py
    ...                    # per-trade admission, reason codes, pure evaluate predicate
  tax/
    contract.py
    ...                    # two ledgers, reconciliation, exceptions, bridges, planned tax,
                           # candidate-proposal workflow
  ops_reconciliation/
    contract.py
    ...                    # Green/Amber/Red per channel, breaks, freeze,
                           # reference-data staleness alarms  [C9]
  strategy_simulation/
    contract.py
    ...                    # canonical simulator, overlays, shadow decisions,
                           # Phase-0 pre-screen harness (minimum sub-build)
```

**Contract file rule.** `contract.py` is the only file another module may import from this module. It contains: module-local command payloads, module-local event payloads, query `Protocol`s, error types, and stated invariants. Cross-module events and payloads referenced by two or more domain modules live in `events/contract.py`, not in any one domain contract. Everything else — aggregates, adapters, projections, stores — is internal and subject to change without coordination.

---

## 1bis. `events` module — frozen day 1 (`modules/events/`) [C1]

Zero business logic. Zero `Protocol`s. Only dataclasses and enums for payloads that cross module boundaries. Its purpose is to break the cross-module contract cycles the plan noted but handwaved via `TYPE_CHECKING`.

Contains exactly these payload types:

- **Intent / admission flow:** `StrategyIntent` (emitted by `strategy_simulation`, consumed by `admission_control`), `AdmissionInputs` (the frozen record materialised by `admission_control` and consumed unchanged by `strategy_simulation.evaluate`), `AdmissionVerdict` (emitted by `admission_control`, consumed by downstream), `OrderAdmitted` and `OrderRejected` wrappers that carry `AdmissionInputs` + `AdmissionVerdict` + causation.
- **Accounting ↔ tax bridge:** `FillBooked` (emitted by `portfolio_accounting`, consumed by `tax`), `BridgeAdjustmentBooked` (emitted by `tax`, consumed by `portfolio_accounting`).
- **Reason codes and predicate names:** `RejectReason` enum, `PredicateName` enum, `PredicateResult` dataclass, `FeeBreakdown` dataclass.

Domain modules import these types from `events.contract`; they no longer import each other's contracts for payload types. Protocol-based readers continue to live on the owning module's `contract.py`.

---

## 2. Kernel — frozen day 1 (`modules/kernel/`)

Zero business logic. No tax, compliance, fee, settlement, or broker concepts. Changes to the kernel are a multi-module coordination event and should be rare.

### 2.1 Identifier types (opaque, strongly typed)

Distinct `NewType`s over `str` / `UUID`. Never plain strings at module boundaries.

- Account / account structure: `AccountId`, `FiscalYear`
- Strategy / admission: `StrategyIntentId`, `AdmissionDecisionId`, `StrategyId`
- Order lifecycle: `ApiOrderId`, `BrokerPermId`, `ParentPermId`
- Execution: `ExecId`, `ExecCorrectionGroupId`, `CommissionReportId`, `StatementPostingId`
- Instruments: `Isin`, `VenueMic`, `ContractId` (= IBKR `conId`), `TradableLineId`
- Policy: `EvidenceRecordId`, `GovernanceDecisionId`, `GovernanceExceptionId`, `ReferenceDatumId`, `SettlementRegimeId`, `ParameterName`, `ProductionProfileId` 
- Economic: `JournalEntryId`, `TaxLotId`, `PostingAccountCode`
- Gate/Research: `GateVerdictId`, `StrategyCertificationId`, `Phase0ProtocolId`, `ResearchConfigId`, `SimulationRunId`, `OverlayName`

`BrokerPermId` is canonical for API-originated activity. `FallbackIdentity` is defined in the kernel as a data shape only — a frozen composite of kernel IDs — so that every module's events can reference it without importing `execution`. The semantics (when to use it, how to construct it, the `origin = non_api` flag, the link to a `GovernanceExceptionId` or `StatementPostingId`) are owned by `execution` (§3.3 of this plan and §10 of the idea).

### 2.2 Money and time

- `Money` — frozen (amount: `Decimal`, currency: `Currency`). Arithmetic only within same currency; cross-currency operations require an explicit `FxRate`. No floats anywhere economic.
- `Currency` — enum, v1 contains `EUR` only (other codes reserved).
- `Instant` — UTC datetime with nanosecond resolution.
- `BusinessDate` — calendar-anchored date; calendar resolution lives in `market_policy`.
- `EffectiveDateRange` — half-open `[open, close)` with `close=None` meaning "current".

### 2.3 Event envelope

Every persisted event carries:

- `event_id: EventId` (UUIDv7, monotonic)
- `event_type: str` (fully qualified, e.g. `execution.BrokerFill.v1`)
- `occurred_at: Instant` (when the fact became true in the domain)
- `recorded_at: Instant` (when the system observed it)
- `causation_id: EventId | None` (the event that directly caused this one)
- `correlation_id: EventId` (workflow-spanning id, e.g. the originating `StrategyIntent`)
- `schema_version: int`
- `payload: <module-specific>`

Versioning rule: payload schemas are append-only; a breaking change means a new event type (`BrokerFill.v2`), never a mutated `v1`.

### 2.4 Protocols

- `Clock` — `now() -> Instant`. Every module takes `Clock` by injection. No `datetime.now()` outside adapters.
- `EventBus` — `publish(event)` and `subscribe(event_type, handler)`. v1 implementation is in-process and synchronous; subscribers run in a deterministic order under a transaction boundary owned by `platform_runtime`.
- `IdFactory` — `new[T]() -> T` for each kernel ID type.  `IdFactory` has two modes: a **live** mode (UUIDv7, monotonic) and a **replay** mode constructed with an explicit `replay_seed: int`. Replay mode is deterministic: the same event stream replayed under the same seed produces identical IDs for any module-generated ID (e.g. derived `JournalEntryId` on `BookFill`). This is a precondition of the economic-state-invariance rule (§10 of the idea; §3.4 invariants).

### 2.5 Result / error

- `Outcome[T]` = `Ok(T) | Err(DomainError)`.
- `DomainError` — structured `(code: str, message: str, detail: Mapping)`. Never a bare `Exception` across a module boundary.
- Module-specific error codes live in that module's `contract.py` and inherit from `DomainError`.

### 2.6 Shared enums

- `Side = {BUY, SELL}`
- `OrderType = {DAY_LIMIT}` — only value allowed to appear in v1 live paths; other values reserved for shadow/simulation.
- `Session = {CONTINUOUS, EXTENDED_RETAIL_EARLY, EXTENDED_RETAIL_LATE, AUCTION_OPEN, AUCTION_CLOSE}` — v1 live paths accept `CONTINUOUS` only.
- `Channel = {BROKER_CASH, POSITIONS, FEES, TAX_POSTING, SETTLEMENT, COMPLIANCE}` — reconciliation channels. `SETTLEMENT` added so that divergence between the `portfolio_accounting` settlement forecast and broker-posted settlement releases is a first-class data-state concern rather than being rolled into `BROKER_CASH` (where it would be invisible until cash is already wrong). **`COMPLIANCE` added [C9] so reference-data staleness — expired PRIIPs KID, stale Basiszins, stale fund classification — is a first-class data-state concern with its own AMBER/RED lifecycle, not buried inside per-tradable-line flags.**
- `DataState = {GREEN, AMBER, RED}`
- `Tier = {TIER_0, TIER_1, TIER_2}`
- `V1TerminalState = {NOT_REACHED, NARROW_SUCCESS, FULL_SUCCESS}` — the narrow-success state from idea §1 is not observable from Gate verdicts alone (Gates 2–4 may simply be unasserted), so it must be a declared governance fact. Ownership: `market_policy`. **Phase-0 failure is explicitly not one of these states [C14]** — Phase-0 is a pre-v1 stop-loss under idea §16, not a terminal verdict on the v1 kernel.
- **[C14] `Phase0Verdict = {PENDING, PASS, FAIL}`** — the outcome of a single Phase-0 pre-screen run. `PASS` unblocks Phase-A engineering (step 1 onwards). `FAIL` does **not** declare a V1 terminal state; it requires a signed governance re-scope decision (raise capital, narrow to buy-and-hold with no rotation, or close v1 without an execution engine — idea §16). Re-running a failed pre-screen requires a **fresh** `Phase0ProtocolRegistered` against a new `ResearchConfigId` with its own four frozen items; silently re-evaluating the same data against a relaxed threshold is refused by `RegisterPhase0Protocol`'s structural invariant. Ownership: `market_policy`.
- **[C14] `GateVerdictStatus = {PROVISIONAL, DEFINITIVE, PASS, FAIL, INVALIDATED, DEFERRED}`** — the full set of statuses a `GateVerdictRecorded` event may carry. `PROVISIONAL` and `DEFINITIVE` are Gate-1-only (§4.8 Stage A / Stage B). `PASS` / `FAIL` apply to Gates 2–4. `INVALIDATED` is emitted by `GateVerdictInvalidated` on route re-pinning. `DEFERRED` is a signed governance decision recording that a Gate-2, -3, or -4 evaluation has been formally postponed under a continuation programme (idea §1 "Narrow-success state"); it is not an engine verdict but a reviewer declaration.

---

## 3. Domain modules — frozen contract surfaces

For each module: **responsibilities**, **commands** (what you ask it to do), **events emitted**, **events consumed**, **queries exposed** (read-only `Protocol`s other modules depend on), and **invariants** that the implementation must preserve.

### 3.1 `platform_runtime/`

**Responsibilities.** Host facts safely; it is support code, never business logic.

- **Commands:** `PersistEvent(event)`, `ReplayStream(from, to, filter)`, `RunProjection(name, from)`, `ApplyMigration(name)`.
- **Emits:** infrastructure signals only (`ReplayStarted`, `ReplayCompleted`, `OutboxDrained`, `ProjectionCaughtUp`). No domain events.
- **Consumes:** every domain event (for persistence and projection dispatch).
- **Queries:** `EventStoreReader` (by correlation, type, time window), `OutboxReader`, `ProjectionCheckpointReader`.
- **Invariants:**
  - Event store is append-only; no UPDATE, no DELETE.
  - Publish + persist is a single transactional boundary (outbox pattern); no event is visible to subscribers before it is durable.
  - Replay is deterministic given the same event stream + reference-data snapshots + pinned parameters + **a pinned `IdFactory` replay seed**.
  - Subscriber handlers are idempotent by `(event_id, handler_name)`.
  - **[M1]** `ReplayStream` accepts a **canonical ordering key function** `Callable[[Event], tuple]` as an injected parameter, wired at composition time. The runtime does not statically import any domain module's contract; `execution.contract.CanonicalOrderingKey` (which computes `(coalesce(broker_perm_id, fallback_identity), exec_correction_group_id, occurred_at)`) is the canonical choice and is passed in by the application composition root. This makes `platform_runtime` implementable and testable at build step 2 without any `execution` artefact yet existing.
- **Implementation notes (internal, not frozen):** v1 backing store is SQLite WAL; the bus is in-process; projections run inline on commit. Swapping to Postgres + a real broker is an internal change.

### 3.2 `market_policy/`

**Responsibilities.** Maintain effective-dated, evidence-backed facts and signed decisions that make trading legal, supported, and economically pinned. This is the merged home for reference data, evidence registry, compliance, governance (including the §6 parameters and the four-gate verdicts), and config. The merge is justified because all these artefacts share the **same shape**: effective-dated entry + signed decision + evidence-registry pointer.

- **Commands:** `RegisterReferenceDatum`, `RecordEvidence`, `ApproveTradableLine`, `FreezeTradableLine`, `UnfreezeTradableLine`, `PinParameter`, `RecordGovernanceDecision`, `OpenGovernanceException`, `CloseGovernanceException`, `RecordGateVerdict` (Gate 1..4), `DeferGateVerdict(gate, evidence_id, reviewer_id)` **[C14]**, `CertifyStrategyLcb`, `ExpireStrategyLcb`, `RegisterSettlementRegime`, `RegisterPhase0Protocol`, `RecordPhase0Verdict(protocol_id, verdict, evidence_id, reviewer_id)` **[C14]**, `DeclareV1TerminalState(state, evidence_id)`, `RegisterXlmSnapshot(isin, as_of, size, xlm_bps)`, **`PinProductionProfile(profile_id, rationale_evidence_id)`**, **`RePinProductionProfile(old_profile_id, new_profile_id, rationale_evidence_id)`**, **`SetTier(tier, evidence_id)`**.
- **Emits:** `ReferenceDatumVersioned`, `EvidenceRecorded`, `TradableLineApproved`, `TradableLineFrozen`, `TradableLineUnfrozen`, `ParameterPinned`, `ParameterPlaceholderSet`, `GovernanceDecisionRecorded`, `GovernanceExceptionOpened`, `GovernanceExceptionClosed`, `GateVerdictRecorded`, `GateVerdictDeferred` **[C14]**, `GateVerdictInvalidated`, `StrategyCertified`, `StrategyCertificationExpired`, **`StrategyCertificationInvalidated` [C13]**, `SettlementRegimeVersioned`, `Phase0ProtocolRegistered`, `Phase0VerdictRecorded` **[C14]**, `V1TerminalStateDeclared`, `XlmSnapshotRegistered`, `ProductionProfilePinned`, `ProductionProfileRePinned`, `TierChanged`.
- **Consumes:** nothing from other modules. External documents and reviewer decisions are ingested via commands.
- **Queries:**
  - `ReferenceDataReader.as_of(key, date) -> Datum | None`
  - **[C12] `TradableLineStatusReader(tradable_line_id, as_of) -> {approval_state ∈ {APPROVED, FROZEN}, kid_validity_range: EffectiveDateRange, fund_level_evidence_pointer: EvidenceRecordId, listing_line_evidence_pointer: EvidenceRecordId, bound_profile_id: ProductionProfileId}`** — returns the tradable line's **intrinsic** reference-data state as of `as_of`. This reader deliberately does **not** report a composed `is_live_admissible` boolean: the admissibility composition also depends on `ComplianceStalenessReader` (owned by `ops_reconciliation`, which sits **above** `market_policy` in the dependency graph of §5), and embedding a call from here to there would introduce a static import cycle. The composed admissibility check — intrinsic state **and** evidence freshness **and** profile match — is materialised at `AdmissionInputs` construction time inside `admission_control` (§3.5), where both readers are legitimately in scope. Shadow-profile reference entries (§5 of the idea) are representable as tradable lines with `bound_profile_id` ≠ current pinned profile; such lines are intrinsically approved but the composite check in `admission_control` refuses admission against them unless `V1TerminalStateReader(as_of) == FULL_SUCCESS`.
  - `GovernanceParameterReader(name, as_of) -> (value, {PINNED|PLACEHOLDER}, effective_from, decision_id)` — parameter names are drawn from a **pinned enum** `ParameterName` in `market_policy.contract`; unknown names are a contract error, not a silent miss. The enum includes, at minimum:
    - Cost / admission: `b_1w`, `R_nonfx` (the non-FX composition of `R`; FX component is a fixed-zero constant in v1 per idea §6), `cash_buffer_floor`, `B_model`, `B_ops`;
    - **LCB calibration bundle (idea §6):** `lcb_margin`, `lcb_sampling_distribution`, `lcb_calibration_window`, `lcb_refresh_cadence`, `gate3_evaluation_horizon`;
    - Churn: `churn_window`, `churn_cap`, `churn_metric` (the last is a plan-level disambiguation — idea §6 pins the window and cap but is silent on the unit; `churn_metric ∈ {notional_weighted, decision_count}` is pinned under governance alongside the other §6 parameters);
    - Re-submit: `per_decision_resubmit_cap`, `resubmit_window`, `resubmit_window_cap`;
    - **Fill-model calibration (idea §13):** `fill_model_min_exemplar_count`, `fill_model_calibration_window`, `fill_model_refresh_cadence`;
    - **[C3] Limit-price construction (idea §13):** `limit_price_aggression_fraction` (a dimensionless `Decimal ∈ [0, 1]` pinned under governance; 0 = pure passive placement at the passive-side top-of-book, 1 = cross to the opposite-side top-of-book; the pinned value and its rationale are a §6 governance input), `limit_price_max_spread_budget_bps` (absolute upper bound independent of the quoted spread).
    - **[C9] Reference-data freshness bounds:** `basiszins_max_age_days`, `kid_grace_days_before_expiry`, `fund_classification_max_age_days` — drive the `COMPLIANCE` channel state transitions.
    - Per-channel reconciliation parameters: `(tolerated_divergence_band | reconciliation_grace_window) × Channel`.
  - `EvidenceReader(evidence_id) -> EvidenceRecord` and `EvidenceReader.latest(subject)`
  - `SettlementRegimeReader(instrument, venue, currency, as_of) -> SettlementRegimeId + rule`
  - `StrategyCertificationReader(strategy_id, as_of) -> {CERTIFIED|EXPIRED, gate3_verdict_id, canonical_run_id, fill_stress_run_id}` — surfaces the **run ids** of both the qualifying canonical run and the mandatory fill-probability stress overlay, so an audit of live admission can reach back to the exact simulator artefacts that produced the certification.
  - `GateVerdictReader(gate, as_of) -> {status: GateVerdictStatus, production_profile_id, verdict_id, evidence_bundle_id, reviewer_id, recorded_at}` — surfaces the full `GateVerdictStatus` enum **[C14]** (`PROVISIONAL | DEFINITIVE | PASS | FAIL | INVALIDATED | DEFERRED | UNASSERTED` where `UNASSERTED` is returned when no verdict has been recorded). Gate-1 uses `PROVISIONAL` / `DEFINITIVE` **[C8]** (provisional = archived-synthetic + paper streams; definitive = archived + paper + N pinned weeks of Tier-0 micro-live reconciling cleanly). Gates 2–4 use `PASS` / `FAIL` / `DEFERRED` / `INVALIDATED`. `DEFERRED` is set by `DeferGateVerdict` under a signed continuation-programme decision (idea §1 Narrow-success). `INVALIDATED` is set by `GateVerdictInvalidated` on route re-pinning for verdicts bound to the old profile.
  - **[C14] `Phase0ProtocolReader(protocol_id) -> {research_protocol_id, oos_window, required_effect_size, decision_rule_id, signed_at, verdict: Phase0Verdict}`** — `verdict` is `PENDING` until `RecordPhase0Verdict` is called with `PASS` or `FAIL`. A `FAIL` verdict blocks Phase-A engineering (build step 1) until a new protocol is registered against a new `ResearchConfigId` or a signed re-scope decision is recorded.
  - `V1TerminalStateReader(as_of) -> V1TerminalState` (default `NOT_REACHED`)
  - `XlmReader(isin, as_of, size) -> xlm_bps | None`
  - `FeeScheduleReader(route, venue, order_class, session, lifecycle_state, as_of) -> FeeSchedule` — explicit so `portfolio_accounting` posts venue/clearing/regulatory fees against a pinned schedule and the simulator reads the same one; fee dimensions outside §12's pinned matrix (`lifecycle_state ∉ {NEW}`, `session ∉ {CONTINUOUS}`) return `None` under v1. `FeeSchedule` **[C4]** is decomposed into `(commission_rule, exchange_fee_rule, clearing_fee_rule, regulatory_fee_rule, transaction_tax_rule)`; each sub-rule is independently pinned and independently reconciled during Gate-2 closure. **`admission_control` also reads this at `EvaluateAdmission` time to materialise the fee component of `Ĉ_RT` into `AdmissionInputs`**.
  - **`ProductionProfileReader(as_of) -> {profile_id, route, session, order_type, pricing_plan, rationale_evidence_id, pinned_at, runtime_configuration ∈ {LIVE, PAPER}}` | None** **[C11]** — the single currently live-admissible profile, or `None` before it is pinned. `runtime_configuration` flags whether the broker adapter points at IBKR live or IBKR paper; both share the same pinned profile but every emitted event records the configuration tag.
  - **`TierReader(account_id, as_of) -> {tier, since, decision_evidence_id}`** — current tier is a governance state (transitions require `SetTier`). `admission_control` reads this directly rather than re-computing.
  - **[C5] `TradableLineLiquidityCapsReader(tradable_line_id, as_of) -> {quoted_spread_cap_bps, liquidity_window_open_utc, liquidity_window_close_utc, notional_cap_per_order}` | None** — per-ISIN admission caps are effective-dated reference data owned by `market_policy`. The `quoted_spread_cap_bps` is the ISIN-specific spread cap referenced in idea §13's per-order liquidity gate; the liquidity-window bounds exclude the first/last minutes of continuous trading unless empirical fill quality justifies relaxing them (idea §13). Consumed by `admission_control` predicate 6; consumed by `strategy_simulation` under the simulator-validity rule.
- **Invariants:**
  - All changes are additive (new effective-dated rows); prior rows are never mutated.
  - Every decision with a `GovernanceDecisionId` carries a non-empty evidence-pointer set.
  - Exactly one **live-admissible** production execution profile at any time. Shadow profiles may coexist as reference data with `is_live_admissible = False`. **`ApproveTradableLine(is_live_admissible=True)` is refused when it would introduce a tradable line bound to a different `ProductionProfileId` than the current pinned profile, unless `V1TerminalStateReader` returns `FULL_SUCCESS` (scope-expansion rule, idea §17)**.
  - PRIIPs KID validity is date-bounded (never "true forever").
  - **[C6] Intrinsic tradable-line state.** `TradableLineStatusReader` is a pure reference-data reader: its output at a given `as_of` is a deterministic function of the market-policy event stream as of that time. It reports approval state, KID validity range, evidence pointers, and the bound production-profile id — and nothing more. Any caller that needs a composed live-admissibility decision must additionally consult `ComplianceStalenessReader` (for evidence freshness on the `COMPLIANCE` channel) and `ProductionProfileReader` (for profile match). The composition rule is authoritatively specified in `admission_control.contract` as the `compose_live_admissibility(...)` pure function (§3.5), and is the same function called by both live admission and `strategy_simulation`'s simulator-validity check, so the composition cannot diverge across call sites.
  - Source precedence for Ledger-A-relevant facts: issuer legal > BMF > broker classification (§9).
  - A strategy has a Gate-3 certification if and only if it has a non-expired `StrategyCertified` event with a Gate-3 verdict pointer *and* a pointer to a canonical simulation run plus a passing fill-probability stress overlay run. **Certification expires via one of three paths: (a) on reaching `lcb_refresh_cadence` from its `signed_at` (emits `StrategyCertificationExpired`); (b) on a signed `ExpireStrategyLcb` decision (also emits `StrategyCertificationExpired`); (c) [C13] on a `ProductionProfileRePinned` event, for every certification whose bound `canonical_run_id` was produced under the old profile — `RePinProductionProfile` emits `StrategyCertificationInvalidated` referencing each affected certification. `StrategyCertificationReader` returns `EXPIRED` for a certification invalidated by any of these three paths; a re-certification under the new profile (requiring a fresh canonical simulator run bound to the new profile, plus a fresh mandatory fill-probability stress overlay, plus a new `CertifyStrategyLcb` command) is the only path back to `CERTIFIED`.**
  - Parameters tagged `PLACEHOLDER` are legal inputs to simulator runs but **not** to any Gate-2 or Gate-3 verdict.
  - `RegisterPhase0Protocol` refuses if any OOS evaluation number for the referenced research-configuration already exists in the event store. The four frozen items must be signed *before* `PhaseZeroPreScreenEvaluated` is ever emitted for that protocol. This is a structural guarantee, not a process note.
  - **[C14] Phase-0 verdict vs V1 terminal state.** `RecordPhase0Verdict(protocol_id, PASS|FAIL, …)` emits `Phase0VerdictRecorded`. A `FAIL` verdict does **not** imply any `V1TerminalState`; it blocks Phase-A engineering (step 1 onwards) until either (a) a fresh `Phase0ProtocolRegistered` is signed against a new `ResearchConfigId` with its own four frozen items (and subsequently passes), or (b) a signed governance decision records a v1 re-scope per idea §16 (raise capital, narrow to buy-and-hold with no rotation, or close v1 without an execution engine). Re-running a failed pre-screen against the *same* research configuration with a *relaxed* threshold is refused by `RegisterPhase0Protocol`'s existing structural invariant (no prior OOS numerics may exist for the referenced research config).
  - **[C14] `DeclareV1TerminalState(NARROW_SUCCESS)`** is admissible only if: (i) `GateVerdictReader(1, as_of).status == DEFINITIVE`; (ii) for each gate in `{2, 3, 4}`, `GateVerdictReader(gate, as_of).status ∈ {UNASSERTED, DEFERRED}` — that is, the gate has either never been recorded or has been explicitly deferred via `DeferGateVerdict` under a signed continuation-programme decision (idea §1 "Narrow-success state"). A Gate-2, -3, or -4 verdict with status `FAIL` or `INVALIDATED` is **not** admissible for `NARROW_SUCCESS`; the fail must be remediated (and a fresh `PASS` recorded) or formally deferred (which requires a signed governance decision recording *why* that gate's deferral is defensible given the evidence that caused the fail).
  - **[C14] `DeclareV1TerminalState(FULL_SUCCESS)`** requires `GateVerdictReader(1, as_of).status == DEFINITIVE` and `GateVerdictReader(2..4, as_of).status == PASS` (not `DEFERRED`, not `FAIL`, not `INVALIDATED`). A `PROVISIONAL` Gate-1 verdict is not sufficient for either terminal-state declaration; `PROVISIONAL` only unlocks Tier-0 micro-live. The terminal declaration is itself a signed governance event and feeds the scope-expansion rule (§17 of the idea) and the v2 precondition (§18).
  - **`RePinProductionProfile` invalidates any prior `GateVerdictRecorded(gate=2, production_profile_id=old, status=PASS)` by emitting `GateVerdictInvalidated`; the new profile is not production-supported until a fresh Gate-2 verdict is recorded against it (idea §5 "Re-pinning", §12 Gate-2 scope). [C13] It additionally emits `StrategyCertificationInvalidated` for every active certification whose bound `canonical_run_id` resolves to a simulator run with `production_profile_id == old_profile_id`. Invalidation is immediate and atomic with the re-pin event; there is no grace period in which a stale certification remains admissible**.
  - **Gate-2 and Gate-3 verdicts carry a `production_profile_id` field. A verdict is auto-invalidated on `ProductionProfileRePinned` when the new profile differs from the verdict's bound profile**.

### 3.3 `execution/`

**Responsibilities.** Convert broker traffic into canonical operational facts. Own the execution-event ledger; own canonical identity and correction grouping; own fallback identity for non-API activity. **Own deterministic limit-price construction at submit time per idea §13 (anchor to live top-of-book, cap aggression by a governance-pinned aggression fraction and a quoted-spread-derived budget, DAY tenor, single-shot cancel/re-submit under the pinned budget); the limit price is computed inside `execution.SubmitOrder` from the `OrderAdmitted.admission_decision_id` plus the current `MarketSnapshotReader`, not by `admission_control` and not by `strategy_simulation`**. Own the live-vs-paper adapter configuration tag **[C11]**. Never economic truth.

- **Commands:** `SubmitOrder(OrderAdmitted)`, `CancelOrder(broker_perm_id)`, `IngestBrokerCallback(raw)`, `IngestTradeConfirmation(raw)`, `IngestActivityStatement(raw)`, `AuthorizeFallbackEvent(governance_exception_id|broker_posting_id, payload)`, `RecordReSubmit(strategy_intent_id, parent_admission_decision_id)`.
- **Emits:** `OrderSubmitted`, `BrokerOrderAck`, `BrokerFill`, `BrokerPartialFill`, `OrderCancelled`, `OrderRejected`, `OrderExpired`, `BrokerCorrection`, `CommissionReported`, `StatementPostingReceived`, `FallbackEventRecorded`, `ReSubmitRecorded`, **`LimitPriceConstructed`** — payload carries `(admission_decision_id, side, bid, ask, quoted_spread_bps, aggression_fraction_used, spread_budget_cap_bps, chosen_limit_price, runtime_configuration)` so limit-price determinism is auditable **[C3]**. Every emitted event additionally carries a `runtime_configuration ∈ {LIVE, PAPER}` tag read from `ProductionProfileReader` **[C11]**.
- **Consumes:** `OrderAdmitted` (from `events.contract`; originally emitted by `admission_control`), broker IO, `ParameterPinned`/`ParameterPlaceholderSet` for `per_decision_resubmit_cap`, `resubmit_window`, `resubmit_window_cap`, `limit_price_aggression_fraction`, `limit_price_max_spread_budget_bps` (cached via `GovernanceParameterReader`).
- **Queries:**
  - `ExecutionEventLedgerReader` — lookup by `broker_perm_id`, `exec_correction_group_id`, `strategy_intent_id`, `fallback_identity`.
  - `CorrectedExecutionSetReader(broker_perm_id) -> ExecutionSet` — returns only the supersession-winner view of a correction group.
  - `ReSubmitBudgetReader(strategy_intent_id, as_of) -> {used, cap, remaining}` — **per-decision** budget. Cap is read from `GovernanceParameterReader("per_decision_resubmit_cap")`; this module enforces but does not own the cap.
  - `ReSubmitWindowBudgetReader(account_id, window_as_of) -> {used_in_window, cap, remaining}` — **cumulative-window** budget (§13 of the idea). Exceeding the window cap produces a `ReconciliationBreakOpened` on the `FEES` channel via an `ops_reconciliation` subscription; exhaustion freezes further admission until closed.
  - `CanonicalOrderingKey` — a pure function exported from `contract.py` that computes `(coalesce(broker_perm_id, fallback_identity), exec_correction_group_id, occurred_at)` for any execution-event-ledger record. Used by `platform_runtime.ReplayStream` (see §3.1 invariants).
  - **`MarketSnapshotReader(tradable_line_id, as_of) -> {bid, ask, quoted_spread_bps, book_depth_snapshot, session} | None`** — the authoritative live market-snapshot adapter. `admission_control` and `execution.SubmitOrder` both read this. Returns `None` outside `CONTINUOUS` session hours and outside the per-ISIN liquidity window (from `TradableLineLiquidityCapsReader`). The adapter is internal to `execution`; the Protocol is exposed on `execution.contract`. `top_of_book = (bid, ask)`; `quoted_spread_bps = 10000 · (ask − bid) / ((ask + bid) / 2)`.
  - **`LimitPriceComputer(side, market_snapshot, aggression_fraction, spread_budget_cap_bps) -> Decimal`** **[C3]** — pure function exported from `contract.py`, consumed by `execution.SubmitOrder` and (identically) by `strategy_simulation` so simulation and live construction cannot drift. See invariant below for the exact mathematical form.
- **Invariants:**
  - Corrections never mutate prior events; a `BrokerCorrection` shares an `exec_correction_group_id` with what it supersedes.
  - **[C10] `execDetails` correction-detection rule.** A broker callback is classified as a correction — and assigned the same `exec_correction_group_id` as the original — iff it arrives with the same `permId` and functionally-identical execution parameters (`side`, `qty`, `price`, `time` modulo nanosecond jitter within a pinned tolerance) as a prior event but a different `execId`. This matches the IBKR-documented correction signature. A same-`permId` callback with materially different parameters is not a correction; it is treated as a new execution and fails the invariant check with an explicit error requiring investigation.
  - Every order-side event carries `broker_perm_id`; every execution-side event carries `broker_perm_id` **or** a `FallbackIdentity` composite, never neither.
  - `FallbackIdentity` is used only when `broker_perm_id` is 0/absent, is flagged `origin = non_api`, and is linked either to a `GovernanceExceptionId` or a `StatementPostingId`.
  - Each re-submit is a **new** event with its own `strategy_intent_id` linkage; the per-decision re-submit counter is enforced at submit-time against `ReSubmitBudgetReader`, the rolling-window counter against `ReSubmitWindowBudgetReader`.
  - Canonical ordering for downstream replay: `(coalesce(broker_perm_id, fallback_identity), exec_correction_group_id, occurred_at)`.
  - A spike in `FallbackEventRecorded` over a rolling window is a governance signal: `ops_reconciliation` subscribes and may transition the `POSITIONS` or `BROKER_CASH` channel to `AMBER` / `RED` per pinned threshold. Non-API origin is auditable but not normal operating state.
  - **[C3] Deterministic limit-price construction.** `SubmitOrder` calls `LimitPriceComputer` with the pinned `limit_price_aggression_fraction` `α` and the pinned `limit_price_max_spread_budget_bps` `β`. The computer is defined as:
    ```
    quoted_spread_ccy = ask - bid
    spread_budget_ccy = min( α · quoted_spread_ccy,
                             β · mid / 10000 )                # β is an absolute cap
    if side == BUY:
        chosen_limit = bid + spread_budget_ccy                # starts passive at bid, walks up
    else:  # SELL
        chosen_limit = ask - spread_budget_ccy                # starts passive at ask, walks down
    ```
    — where `mid = (bid + ask) / 2`. At `α = 0` the limit sits at the passive-side top-of-book; at `α = 1` it sits at the opposite-side top-of-book (i.e. crosses). `β` caps aggression independently of the quoted spread, so a blown-out spread cannot force an unbounded absolute deviation. The aggression-fraction and spread-budget cap are pinned §6 governance parameters; until they are pinned, `SubmitOrder` uses the documented exploratory placeholder (`α = 0`, `β = 0`, i.e. purely passive) and admits the resulting no-fill as the expected exploratory outcome. Every `LimitPriceConstructed` event records `(bid, ask, α, β, chosen_limit)` so the construction is reproducible from the ledger alone.
  - If `MarketSnapshotReader` returns `None` (outside continuous session or outside the per-ISIN liquidity window), or if the constructed limit would violate an `OrderAdmitted.principal_allowance`, `SubmitOrder` emits `OrderRejected` rather than submitting.
  - **[C11] Live-vs-paper adapter.** `execution` ships a single broker adapter with two configurations (`LIVE`, `PAPER`) selected by the currently-pinned `ProductionProfile.runtime_configuration`. Both configurations share identical canonical-identity, correction, and fallback-identity semantics. Every emitted event carries the `runtime_configuration` tag; a replay that mixes `LIVE` and `PAPER` events on the same `account_id` is a structural error.

### 3.4 `portfolio_accounting/`

**Responsibilities.** Own the append-only economic journal and the cash-state decomposition. Own the settlement forecast as a projection (not an authority). Own the turnover/churn-budget projection. The journal is the sole economic source of truth.

- **Commands:** `BookFill(broker_perm_id, exec_correction_group_id)`, `BookCommission`, **`BookExchangeFee`, `BookClearingFee`, `BookRegulatoryFee`, `BookTransactionTax`** **[C4]** (all four replace the former single `BookVenueFee` — each is posted against its own `PostingAccountCode` so Gate-2 reconciliation can close at component granularity, as idea §12's fee-dimension requirement demands), `BookDistribution`, `BookTaxPosting`, `BookSettlementRelease`, `BookCorrection`, `BookBridgeAdjustment`, `ReserveCashFor(admission_decision_id)`, `ReleaseReservedCash(admission_decision_id, reason)`, `RefreshSettlementForecast`.
- **Emits:** `Posted` (any journal entry), `FillBooked` (emitted from `events.contract` type **[C1]**), `CommissionBooked`, **`ExchangeFeeBooked`, `ClearingFeeBooked`, `RegulatoryFeeBooked`, `TransactionTaxBooked`** **[C4]**, `DistributionBooked`, `TaxPostingBooked`, `BridgeAdjustmentPosted`, `ReservedCashChanged`, `SettlementForecastUpdated`, `JournalCorrectionPosted`, `TurnoverWindowUpdated`.
- **Consumes:** `OrderAdmitted`, `BrokerFill`, `BrokerCorrection`, `CommissionReported`, `StatementPostingReceived`, terminal order events (cancel/reject/expiry), `TaxPosted`, `BridgeAdjustmentBooked` (from `events.contract` type **[C1]**), `StatutoryTaxStateUpdated` (advisory, for reporting views only — see invariants), `SettlementRegimeVersioned` (from `market_policy`).
- **Queries:**
  - `CashStateReader(account_id, as_of) -> {trade_date, settled, reserved, withdrawable}`. Authoritative `settled` for reporting is the journal-derived view; **admission** reads the broker-reported figure via `ops_reconciliation` (§3.7).
  - `PositionReader(tradable_line_id, as_of) -> Position`
  - `JournalReader.by_correlation(correlation_id)` and `.by_account(account_id, range)`
  - `SettlementForecastReader(account_id, horizon)`
  - `ChurnBudgetReader(account_id, window_as_of) -> {used, cap, remaining, window_start, window_end, metric}` — turnover projection over the pinned churn-budget window (`churn_window`, `churn_cap`, `churn_metric` from `GovernanceParameterReader`). The `metric` field surfaces the pinned unit (notional-weighted vs decision-count) so callers do not silently misread.
  - `AumMinComputer(k, route, as_of) -> Money` — pure function exported from `contract.py` that computes `k · N_min(r,t) + cash_buffer(t)` from `GovernanceParameterReader` inputs and `PlannedTaxPostingReader` (annual upper bound — **[C7]** distinct from the per-trade `PlannedTaxFrictionPerTradeReader`). **Consumed by `market_policy` during Gate-4 certification and by reporting views; `admission_control` does NOT call `AumMinComputer` on the per-trade path — it reads the current `Tier` via `TierReader` and enforces the tier's `max_positions` constraint (see §3.5 predicate 3)**.
- **Invariants:**
  - Append-only. Corrections are posted as **adjusting entries** referencing the original `JournalEntryId` + the `exec_correction_group_id`. Prior entries are never mutated.
  - Cash categories (trade-date / settled / reserved / withdrawable) are disjoint and never mixed in admission logic.
  - Reserved cash is created only on `OrderAdmitted` and released only on a terminal order event or explicit reconciliation-driven command; it is bounded above by the open admitted-order set.
  - Settlement forecast is advisory; it never short-circuits admission when it disagrees with broker-reported settled cash. Divergence between the forecast and broker-posted settlement releases is reported on the `SETTLEMENT` channel to `ops_reconciliation`, not on `BROKER_CASH`.
  - Every posting carries the `SettlementRegimeId` active at `occurred_at`.
  - v1 asserts exactly one active `SettlementRegimeId` for the pinned production profile over the full v1 horizon. In-flight regime transitions are not exercised and are not supported by the forecast (§11 of the idea — v2 scope). If `SettlementRegimeReader` returns a regime change while an order is in flight, the journal refuses to post and raises a reconciliation break; this is an explicit v1 guardrail.
  - Economic-state invariance (§10 Rule 4 of the idea): after `RunProjection("journal", …)` on the archived event stream under the canonical ordering **and a pinned `IdFactory` replay seed**, the derived position / settled-cash / reserved-cash / fee / tax state is identical.
  - **[C4] Fee-component posting accounts.** `CommissionBooked`, `ExchangeFeeBooked`, `ClearingFeeBooked`, `RegulatoryFeeBooked`, `TransactionTaxBooked` each use a distinct `PostingAccountCode`. Gate-2 closure evaluates reconciliation against the pinned `FeeSchedule` sub-rule for each component independently; a residual in any single component that exceeds its pinned tolerance blocks Gate 2, even if aggregate fees reconcile (idea §12 "every fee dimension … that the production profile actually touches").
  - Both tax ledgers post to the journal under **distinct account codes** (idea §10). `TaxPostingBooked` (from Ledger B cashflows) and `BridgeAdjustmentPosted` (from Ledger-A-to-B bridges) each use dedicated `PostingAccountCode` values. `StatutoryTaxStateUpdated` is advisory only and does **not** generate journal entries by itself — Ledger A is a state ledger, not a cashflow ledger. This resolves the idea §9/§10 reconciliation by posting *cashflows* from both ledgers to the journal and leaving statutory state non-cash.

### 3.5 `admission_control/`

**Responsibilities.** The single crossing point that decides whether a live order is admissible **right now**, given every dated fact, pinned parameter, and channel state the system knows about.

- **Commands:** `EvaluateAdmission(StrategyIntent)` — `StrategyIntent` is the type from `events.contract` **[C1]**.
- **Emits:** `OrderAdmitted` (payload type from `events.contract`; carries `AdmissionDecisionId`, a full `AdmissionInputs` snapshot, and the emitted `AdmissionVerdict`), `OrderRejected` (payload type from `events.contract`; carries `AdmissionInputs` + `AdmissionVerdict` with `reason_codes` populated).
- **Consumes:** `StrategyIntent` (from `events.contract`), `ChannelStateChanged` (from `ops_reconciliation`) as a short-circuit input.
- **Queries (read):** `GovernanceParameterReader`, `TradableLineStatusReader` (intrinsic state only, per §3.2 **[C12]**), **`ComplianceStalenessReader`** **[C12]** (from `ops_reconciliation`; the evidence-freshness input to the composed admissibility check), `ProductionProfileReader` **[C12]** (for profile-match check), `V1TerminalStateReader` **[C12]** (because scope-expansion under `FULL_SUCCESS` relaxes the profile-match rule per idea §17), **`TradableLineLiquidityCapsReader`** **[C5]**, `StrategyCertificationReader`, `SettlementRegimeReader`, `CashStateReader`, `ChurnBudgetReader`, `ReSubmitBudgetReader`, `ReSubmitWindowBudgetReader`, `ChannelStateReader`, `ConservativeInputReader` (for AMBER), `MarketSnapshotReader` (from `execution`), **`PlannedTaxFrictionPerTradeReader`** **[C7]** (from `tax`; computes the expected per-trade tax-friction cost component of `Ĉ_RT` — this is the per-trade complement of `PlannedTaxPostingReader`, which is annual-cadence and used only on the Gate-4/reporting path), `FeeScheduleReader` (from `market_policy`; materialised into `AdmissionInputs.fee_schedule_snapshot` for the ticket's route/venue/order_class/session/lifecycle_state at `as_of`), `TierReader` (from `market_policy`).
- **Types (defined in `events.contract`, referenced here) [C2]:**
  - `AdmissionInputs` — frozen dataclass enumerating every input the predicate reads (see invariants below for the exhaustive field list).
  - `AdmissionVerdict` — frozen dataclass with fields:
    - `disposition: Literal["ADMIT", "REJECT"]`
    - `reason_codes: frozenset[RejectReason]` — empty iff `disposition == "ADMIT"`
    - `predicate_evaluations: Mapping[PredicateName, PredicateResult]` — one entry per predicate in the rule below, each recording the numeric evaluation that determined pass/fail (e.g. `Ĉ_RT` value, `N_min` value, channel states read)
    - `ĉ_rt_breakdown: FeeBreakdown` — the commission / exchange / clearing / regulatory / transaction-tax / spread / tax-friction decomposition used to compute `Ĉ_RT`, matching the component granularity of §3.4 fee postings **[C4]**
    - `p_fill_telemetry: Decimal` — the bootstrap `p_fill` estimate recorded on the decision record as telemetry only; **not an input to any predicate** (idea §6, §13)
    - `as_of: Instant`
    - `inputs_hash: str` — hash over the frozen `AdmissionInputs` record for replay verification
  - `RejectReason` — enum with one value per predicate below, plus `INCOMPLETE_INPUTS` and `RED_CHANNEL(<channel>)`.
  - `PredicateName` — enum listing the nine admission predicates below.
- **Exposes:**
  - `AdmissionDecisionReader(admission_decision_id) -> AdmissionDecisionRecord` — the audit primitive. The returned record includes a fully materialised `AdmissionInputs` bundle, the emitted `AdmissionVerdict`, and the causation link to the source `StrategyIntent`. This reader is what investigators read to reconstruct "why did the system admit/reject this order".
  - `evaluate(inputs: AdmissionInputs) -> AdmissionVerdict` — pure function (no I/O, no clock, no randomness) exported from `contract.py`. Shared verbatim by `strategy_simulation` per the simulator-validity rule (§15). Returns `Err(INCOMPLETE_INPUTS)` via `AdmissionVerdict.disposition = REJECT` + `reason_codes = {INCOMPLETE_INPUTS}` if any required field is missing.
  - **[C12] `compose_live_admissibility(intrinsic: TradableLineIntrinsicState, staleness: ComplianceStalenessSnapshot, current_pinned_profile: ProductionProfileId | None, terminal_state: V1TerminalState, as_of: Instant) -> LiveAdmissibility`** — pure function (no I/O, no clock, no randomness) exported from `contract.py`. `LiveAdmissibility` is a frozen dataclass with fields `{admissible: bool, reason_codes: frozenset[LiveAdmissibilityReasonCode]}` where a `False` result enumerates exactly which sub-checks failed. The function returns `admissible = True` iff **all** of:
    1. `intrinsic.approval_state == APPROVED`;
    2. `intrinsic.kid_validity_range` contains `as_of`;
    3. `staleness.kid_status`, `staleness.basiszins_status`, and `staleness.classification_status` are all `GREEN` or `AMBER` (not `RED`); if any is `AMBER`, `admissible` remains `True` but `reason_codes` includes an advisory `STALENESS_AMBER(<subject>)` flag that the caller may act on (v1 treats `AMBER` as degraded-but-safe per §4 Rule 6 of the idea);
    4. `intrinsic.bound_profile_id == current_pinned_profile`, **or** `terminal_state == FULL_SUCCESS` (scope-expansion rule, idea §17).
    The composition rule lives here — not in `market_policy` — because both of its non-`market_policy` inputs (`ComplianceStalenessReader` from `ops_reconciliation`, and the intrinsic-state reader from `market_policy`) legitimately compose at the `admission_control` level given the §5 dependency graph. `strategy_simulation` calls the same function under the simulator-validity rule.
- **Invariants (the per-trade rule, §6 of the idea):**
  - `AdmissionInputs` is a frozen dataclass (defined in `events.contract` **[C1]**) enumerating **every** input the predicate reads:
    - **[C12] `live_admissibility`** — a `LiveAdmissibility` record produced by calling `compose_live_admissibility(...)` at materialisation time. The materialiser reads `TradableLineStatusReader` (intrinsic state), `ComplianceStalenessReader` (evidence freshness), `ProductionProfileReader` (current pinned profile), and `V1TerminalStateReader`, then passes those four values into the pure function. The frozen result is stored on `AdmissionInputs` so that replay produces identical verdicts. The admission predicate no longer reads `TradableLineStatusReader` directly; it reads `AdmissionInputs.live_admissibility` only.
    - **per-ISIN liquidity caps** (`quoted_spread_cap_bps`, `liquidity_window_open_utc`, `liquidity_window_close_utc`, `notional_cap_per_order`) from `TradableLineLiquidityCapsReader` **[C5]**;
    - **current tier** (via `TierReader`);
    - governance parameters by name (`b_1w`, `R_nonfx`, `B_model`, `B_ops`, `cash_buffer_floor`, the per-channel band/grace pair, the per-decision re-submit cap, the churn window/cap/metric triple);
    - strategy certification state (certified|expired|invalidated, bound run ids) — **[C13]** the `INVALIDATED` status is distinct from calendar `EXPIRED`; the `StrategyCertificationReader` reports `EXPIRED` in both cases, but the underlying event (either `StrategyCertificationExpired` or `StrategyCertificationInvalidated`) is retained in the event store for audit;
    - settlement regime;
    - broker-authoritative settled cash (via `ops_reconciliation.ConservativeInputReader`);
    - projected settled cash (via `CashStateReader`);
    - reserved-cash snapshot;
    - re-submit per-decision usage, re-submit window usage;
    - churn-window usage;
    - channel states for all `Channel` values (including the `COMPLIANCE` channel **[C9]**);
    - market snapshot (bid, ask, quoted spread from `MarketSnapshotReader`);
    - **fee-schedule snapshot** for the ticket's (route, venue, order_class, session, lifecycle_state, as_of) from `FeeScheduleReader`, decomposed into commission / exchange / clearing / regulatory / transaction-tax sub-rules per §3.4 **[C4]**;
    - **per-trade tax-friction expectation** (from `PlannedTaxFrictionPerTradeReader` **[C7]**) — the per-trade component of `Ĉ_RT`, distinct from the annual `cash_buffer(t)` upper bound used by `AumMinComputer`;
    - ticket parameters (side, notional, route, session).

    If an input is missing, `evaluate` returns an `AdmissionVerdict` with `disposition = REJECT` and `reason_codes = {INCOMPLETE_INPUTS}`; it does not silently default.
  - Admission is admitted iff **all** of:
    0. **[C12] Live admissibility**: `AdmissionInputs.live_admissibility.admissible == True`. If `False`, the verdict carries `reason_codes` that includes one `LiveAdmissibilityReasonCode` per failing sub-check, mapped onto the parent `RejectReason` enum as `TRADABLE_LINE_NOT_ADMISSIBLE(<sub_reason>)`. This predicate is evaluated **first** and short-circuits: a non-admissible tradable line produces a `REJECT` without exercising any of predicates 1–9.
    1. Fill-conditional excess-benefit check: `E[Δα_vs_do_nothing | fill] > Ĉ_RT + B_model + B_ops`. `Ĉ_RT` is computed from the **materialised fee schedule in `AdmissionInputs`** (by component **[C4]**) plus the spread component from the market snapshot plus the per-trade tax-friction component from `PlannedTaxFrictionPerTradeReader` **[C7]**; it includes the worst-case commission charge under the **per-decision** re-submit cap.
    2. Ticket size ≥ `N_min(r, t)` for the active route at `t` (`N_min` is computed inside `evaluate` from the materialised fee-schedule snapshot and `b_1w`, `R_nonfx`).
    3. **Tier permits the proposed post-trade position count: `post_trade_open_positions ≤ Tier.max_positions` where `Tier` is read from `AdmissionInputs.current_tier`. `AumMinComputer` is NOT invoked on the per-trade path; tier transitions are governance events handled by `market_policy.SetTier` after a Gate-4 verdict, per idea §6**.
    4. Churn-budget window usage is within its pinned cap (read from `AdmissionInputs.churn_usage`).
    5. Strategy holds a currently valid Gate-3 LCB certification (`StrategyCertificationReader` returns `CERTIFIED`, not `EXPIRED`). Tier-0 plumbing trades are exempt. **[C13]** A certification invalidated by route re-pinning is reported as `EXPIRED` and therefore fails this predicate until re-certification under the new profile.
    6. Ticket-size-native liquidity gate passes **[C5]**: quoted spread at the market snapshot below `AdmissionInputs.liquidity_caps.quoted_spread_cap_bps`; decision timestamp within `[liquidity_window_open_utc, liquidity_window_close_utc]`; ticket notional within `notional_cap_per_order`; observed-slippage reserve covered. XLM is **not** a per-ticket reject input; it enters only through the `R_nonfx` reserve composition.
    7. No reconciliation channel relevant to the order is in `RED`. `BROKER_CASH` `RED` freezes all buy admissions; `SETTLEMENT` `RED` freezes buy admissions that rely on forecasted settling cash; `POSITIONS` / `FEES` / `TAX_POSTING` `RED` freeze admission paths that depend on the channel; **`COMPLIANCE` `RED` freezes admission on the affected tradable line(s)** **[C9]** — note that `COMPLIANCE=RED` scoped to a specific line will typically have already been reflected in `live_admissibility.admissible = False` via predicate 0, but predicate 7 additionally guards cross-line `COMPLIANCE=RED` states (e.g. a stale Basiszins affecting all lines) that the per-line composition cannot express.
    8. Broker-reported settled cash supports principal + commission-allowance + exchange/clearing/regulatory/tax fee allowance + spread allowance + per-trade tax-friction allowance after reservation. Under no circumstance does the engine override the broker when the broker reports insufficient cash.
    9. Per-decision re-submit cap has remaining budget; rolling-window re-submit cap has remaining budget. An intent that would immediately exhaust either is rejected.
  - Bootstrap `p_fill` is recorded on `AdmissionVerdict.p_fill_telemetry` as **telemetry only**; it is not an input to any of the above predicates in v1.
  - If any channel is `AMBER`, inputs are taken from the **more conservative** of the divergent sources (e.g. lower of projected vs broker-reported settled cash).
  - Every `OrderAdmitted` carries a frozen copy of `AdmissionInputs` and the full `AdmissionVerdict`; `OrderRejected` carries the same plus the failing predicate(s) in `reason_codes`.
  - `RejectReason` enum in `events.contract` has exactly one value per predicate above (including `TRADABLE_LINE_NOT_ADMISSIBLE` with sub-reasons **[C12]**), plus `INCOMPLETE_INPUTS` and `RED_CHANNEL(<channel>)`. The test suite (§8) asserts coverage of every reason code and every sub-reason.
  - The admission predicate is exposed in `contract.py` as a **pure function** `evaluate(inputs: AdmissionInputs) -> AdmissionVerdict`. `strategy_simulation` calls this identical function under the simulator-validity rule (§15 of the idea); there is never a second implementation of admission logic. The composition helper `compose_live_admissibility(...)` is similarly pure and similarly shared with the simulator.

### 3.6 `tax/`

**Responsibilities.** Maintain the two legally distinct ledgers and the bridge between them. Own the annual-reconciliation artefact and the exception workflow. **Own the candidate/approval split for manually-signed tax events: the engine computes candidate entries via proposal commands, and a reviewer signs them via the corresponding record commands (idea §4 Rule 5, §9 "v1 operational minimality")**.

- **Commands:**
  - Engine-authored candidates: `ProposeDeemedTransaction(tax_lot_id, candidate_payload)`, `ProposeBridgeAdjustment(fiscal_year, candidate_payload)`, `ProposeAnnualReconciliation(fiscal_year, candidate_residual, hypotheses)`.
  - Reviewer-authorised: `UpdateStatutoryState(evidence_id, payload)`, `BookTaxCashFromStatement(statement_posting_id)`, `RecordBridgeAdjustment(fiscal_year, reason, amount, evidence_id)` — now takes a reviewer id and **may reference a prior `BridgeAdjustmentProposed` event id**, `RecordDeemedTransaction(tax_lot_id, reviewer_id, evidence_id)` — may reference a prior `DeemedTransactionProposed` event id, `OpenTaxException(fiscal_year, residual, hypotheses)`, `CloseTaxException(exception_id, resolution, evidence_id)`, `ComputeAnnualReconciliation(fiscal_year)`, `ReopenFiscalYear(fiscal_year, reason, evidence_id)` — for retrospective broker restatements or issuer retroactive reclassifications that cross a closed fiscal-year boundary (idea §9).
- **Emits:** `StatutoryTaxStateUpdated`, `TaxPosted` (Ledger B entries, drive accounting postings), `BridgeAdjustmentBooked` (payload type from `events.contract` **[C1]**), `DeemedTransactionProposed`, `BridgeAdjustmentProposed`, `DeemedTransactionRecorded`, `TaxReconciliationOpened`, `TaxReconciliationClosed`, `TaxExceptionOpened`, `TaxExceptionClosed`, `FiscalYearReopened`.
- **Consumes:** `FillBooked` (payload type from `events.contract` **[C1]**; opens/updates tax lots), `DistributionBooked`, `StatementPostingReceived` (filtered to tax-coded postings), `ReferenceDatumVersioned` (Basiszins, classification), `EvidenceRecorded` (Teilfreistellung regime transitions), `BrokerCorrection` on prior-year postings (triggers automatic `TaxExceptionOpened` for the affected year per idea §9 "Retrospective broker changes").
- **Queries:**
  - `StatutoryLedgerReader(tax_lot_id | fund, as_of)`
  - `TaxCashLedgerReader(fiscal_year, as_of)`
  - `TaxLotReader(tax_lot_id) -> {cost, date, qty, accumulated_notional_gl}`
  - `TaxReconciliationReader(fiscal_year) -> {residual, status, exceptions}`
  - `PlannedTaxPostingReader(fiscal_year) -> {expected_vorabpauschale, expected_distribution_tax, worst_case_deemed}` — feeds the `cash_buffer(t)` sizing rule (consumed by `AumMinComputer` in `portfolio_accounting`). **Annual-cadence only; not a per-trade reader [C7]**.
  - **[C7] `PlannedTaxFrictionPerTradeReader(tradable_line_id, side, notional, as_of) -> Money`** — the per-trade tax-friction expectation consumed by `admission_control` to populate the tax-friction component of `Ĉ_RT`. Computed from the statutory ledger state (Teilfreistellung regime, fund classification) and a conservative expected-tax-impact model for a single trade of the given `side` and `notional` under the pinned Germany-retail tax treatment. Deliberately distinct from `PlannedTaxPostingReader`: the latter aggregates expected annual tax cashflows for buffer sizing; this one computes the expected per-order cost stack contribution. Per-trade values are necessarily estimates; they carry a documented conservative bias (over-estimate, never under-estimate) so that the per-trade hurdle cannot be gamed by optimistic tax-friction assumptions.
  - **`ProposedCandidateReader(status={OPEN|APPROVED|REJECTED}) -> [CandidateRecord]`** — the reviewer-facing queue of engine-generated candidates pending sign-off.
- **Invariants:**
  - Ledger A (statutory) and Ledger B (tax-cash) are structurally distinct and never merged.
  - Every Ledger-A entry carries a non-null pointer to an evidence-registry record (authoritative source + retrieval timestamp + document hash + reviewer id). A Ledger-A update without a resolved pointer does not post.
  - Source precedence for Ledger A: issuer legal > BMF (Basiszins) > broker classification (corroborating only).
  - Ledger-B precedence: broker statement postings; broker corrections supersede under `exec_correction_group_id`.
  - Unexplained annual residual > governance tolerance creates a `TaxExceptionOpened`; year-end does not close until every open exception is closed via correction, bridge adjustment, or signed de-minimis.
  - No closure path silently absorbs a residual delta.
  - `RecordDeemedTransaction` and `RecordBridgeAdjustment` always carry a reviewer id and an evidence id; **the engine may emit `DeemedTransactionProposed` / `BridgeAdjustmentProposed` (candidate events) which are reviewer-visible but do not post to the ledgers. Only the `Record...` commands produce `DeemedTransactionRecorded` / `BridgeAdjustmentBooked`, and then only with a reviewer id and evidence id**.
  - `BridgeAdjustmentBooked` emits a journal-destined event consumed by `portfolio_accounting.BookBridgeAdjustment`; Ledger-A `StatutoryTaxStateUpdated` is non-cash and does not. This separation is the contract that implements idea §10's "both ledgers post to the journal under distinct account codes" cleanly: only cashflow-bearing Ledger-A events (bridge adjustments, de-minimis resolutions) cross into the journal.
  - A `BrokerCorrection` touching any `statement_posting_id` whose `fiscal_year < current_fiscal_year` automatically emits a `TaxExceptionOpened` for that year via a subscription; the reviewer evaluates whether `ReopenFiscalYear` is required.
  - **The statutory-state numeric computations (Vorabpauschale per §18 InvStG, Teilfreistellung split per §20 InvStG, deemed transactions per §22 InvStG) are internal to this module and evaluated on-event from the statutory ledger plus BMF Basiszins reference data; v1 operational minimality (idea §9) means these compute at event cadence (buy, sell, distribution, regime change, annual close) rather than as an always-on engine**.

### 3.7 `ops_reconciliation/`

**Responsibilities.** Own cross-channel data-state (Green / Amber / Red per channel), divergence bands, grace windows, and the freeze signal into admission. **Also own reference-data staleness alarms on the `COMPLIANCE` channel [C9]** so that expired PRIIPs KIDs, stale BMF Basiszins, and stale fund-classification evidence are first-class data-state concerns rather than being buried in per-tradable-line flags.

- **Commands:** `IngestLiveSnapshot(channel, source, reading)`, `EvaluateChannelState(channel)`, `OpenBreak(channel, diagnostics)`, `RecordInvestigation(break_id, note)`, `CloseBreak(break_id, resolution)`, **`ScanReferenceDataFreshness(as_of)`** **[C9]** (scheduled; evaluates KID expiry, Basiszins age, and fund-classification age against the pinned freshness bounds).
- **Emits:** `ChannelStateChanged(channel, from, to)`, `ReconciliationBreakOpened`, `ReconciliationBreakClosed`, `InvestigationRecorded`, **`ReferenceDataStalenessDetected(subject, as_of, age, bound)`** **[C9]**.
- **Consumes:** `BrokerFill`, `StatementPostingReceived`, `CommissionReported` (fees channel), `ExchangeFeeBooked` / `ClearingFeeBooked` / `RegulatoryFeeBooked` / `TransactionTaxBooked` **[C4]** (all feed the `FEES` channel at component granularity), `TaxPosted` (tax channel), `SettlementForecastUpdated`, `Posted` (accounting-side views), `ReSubmitRecorded` + `ReSubmitWindowBudgetReader` (fees channel — window exhaustion is a break), `FallbackEventRecorded` (non-API-origin rate monitoring), **`ReferenceDatumVersioned`** **[C9]** (for Basiszins, KID, and fund-classification refresh events), and live TWS/IB Gateway snapshots via an `execution`-owned adapter.
- **Queries:**
  - `ChannelStateReader(channel, as_of) -> {state, since, divergence_observed, sources_in_view}`
  - `BreakReader(status={OPEN|CLOSED})`
  - `ConservativeInputReader(channel, metric) -> value` — returns the governance-more-conservative side of a divergent pair (e.g., lower of projected vs broker-reported settled cash). The "pair" is tracked internally by `ops_reconciliation` from successive `IngestLiveSnapshot` calls per channel.
  - **`ComplianceStalenessReader(tradable_line_id, as_of) -> ComplianceStalenessSnapshot`** **[C9]** — where `ComplianceStalenessSnapshot = {kid_status: DataState, basiszins_status: DataState, classification_status: DataState}` surfaces, per tradable line, the current staleness classification (`GREEN` / `AMBER` / `RED`) of each piece of evidence that bears on admissibility. **[C12] Consumed by `admission_control` at `AdmissionInputs` materialisation time** (the `compose_live_admissibility(...)` pure function takes a `ComplianceStalenessSnapshot` argument). It is **not** called from `market_policy` — that would introduce a static import cycle against the dependency graph of §5. The reader is also consumed by `strategy_simulation` under the simulator-validity rule, so that simulated admission sees the same staleness input as live admission.
- **Invariants:**
  - State transitions are deterministic given `(inputs + pinned tolerance band + pinned grace window)`. The tolerance band and grace window are read from `GovernanceParameterReader` per the per-channel parameter names in §3.2 and are not owned here.
  - `AMBER` is normal operating state, not an exception. `RED` is an exception.
  - `RED` on `BROKER_CASH` freezes all buy admissions until closed. `RED` on `SETTLEMENT` freezes buy admissions that rely on forecast settling cash. `RED` on `FEES`, `POSITIONS`, or `TAX_POSTING` freezes admission paths that depend on that channel. **`RED` on `COMPLIANCE` [C9]** freezes admission on any tradable line whose evidence is classified stale; `AMBER` on `COMPLIANCE` triggers the degraded-but-safe rule (use the more conservative of available evidence, log, schedule re-check).
  - Recovery from `RED` requires an explicit `CloseBreak` with a resolution record.
  - **[C9] Reference-data staleness evaluation.** `ScanReferenceDataFreshness` runs at least daily against the pinned freshness bounds (`basiszins_max_age_days`, `kid_grace_days_before_expiry`, `fund_classification_max_age_days`). Freshness state transitions emit `ChannelStateChanged` on `COMPLIANCE` scoped to the affected tradable-line set. `ReferenceDataStalenessDetected` also feeds the annual-tax-close checklist as an advisory input for the Basiszins year in question.
  - The module never writes to the journal or the execution-event ledger; it only surfaces state.

### 3.8 `strategy_simulation/`

**Responsibilities.** Produce candidate actions under the exact live rules; run the canonical simulator with scenario overlays; produce Phase 0 pre-screen evidence; never trade directly.

**Two-sub-build structure.**  
- **Phase-0 harness (build step 0, alongside `kernel`).** Implements only `RegisterResearchConfig`, `RunPhase0PreScreen`, `PhaseZeroPreScreenFrozen`, `PhaseZeroPreScreenEvaluated`. Operates against frozen research data and `market_policy.Phase0ProtocolReader`. **Does not** import `admission_control`, **cannot** emit `StrategyIntent`, and cannot produce gate evidence. Contract surface unchanged — the rest of the surface returns `Err(NOT_IMPLEMENTED)` until step 9.
- **Full simulator (build step 9).** Implements the canonical simulator, overlay runner, shadow-decision generation, Gate-3 evidence production. At this point the whole §3.8 surface is live.

- **Commands:** `RegisterResearchConfig`, `RunCanonicalSimulation(research_config_id, snapshot_ids)`, `RunOverlay(canonical_run_id, overlay_name, perturbation)`, `EmitStrategyIntent(strategy_id, tradable_line_id, side, target_notional, rationale_ref)` — emits the `StrategyIntent` type from `events.contract` **[C1]**, `GenerateRebalanceProposal(strategy_id, as_of)`, `RunPhase0PreScreen(frozen_protocol_id)`.
- **Emits:** `StrategyIntent` (type from `events.contract`), `RebalanceProposal`, `SimulationRunCompleted` (tagged `canonical` or `overlay:<n>`, with a flag per §6 parameter indicating `PINNED` vs `PLACEHOLDER` at run time; canonical runs additionally carry the bound `ProductionProfileId` and `runtime_configuration` tag **[C11]**), `PhaseZeroPreScreenFrozen`, `PhaseZeroPreScreenEvaluated`.
- **Consumes:** everything as queries — `GovernanceParameterReader`, `ReferenceDataReader`, `TradableLineStatusReader`, **`TradableLineLiquidityCapsReader`** **[C5]**, `SettlementRegimeReader`, `JournalReader`, `PositionReader`, `CashStateReader`, `StatutoryLedgerReader` (dated tax state for tax-friction cost modelling per §15), **`PlannedTaxFrictionPerTradeReader`** **[C7]** (for simulator-materialised `Ĉ_RT` to match live admission exactly), `ChannelStateReader`, `ExecutionEventLedgerReader`, `StrategyCertificationReader`, `FeeScheduleReader` (decomposed into components per §3.4 **[C4]**), `XlmReader`, `Phase0ProtocolReader`, `TierReader`, `ProductionProfileReader` (to bind a canonical run to the exact pinned profile it simulates), **`LimitPriceComputer`** **[C3]** (identical pure function that live `execution` calls, guaranteeing simulator and live limit-price construction cannot drift), and the pure `admission_control.evaluate` predicate (simulator-validity rule).
- **Exposes:**
  - `SimulationArtifactReader(run_id)`
  - `FillModelReader(as_of) -> {kind = bootstrap | exemplar_calibrated, params}`
  - `OverlayCoverageReader(canonical_run_id) -> {overlays_present, mandatory_overlays_missing}` — `mandatory_overlays_missing` is computed against the mandatory-stress list from idea §15. Gate-3 verdict recording by `market_policy` must reject any claim whose `mandatory_overlays_missing` is non-empty.
- **Invariants:**
  - **One canonical simulator** bound to the pinned live profile. Overlays perturb a named, bounded input set; a single run never mixes inputs from two overlays.
  - Simulator admits only what live admission would admit (§3.5 predicates). A run that admits anything live would reject is invalid. Enforced structurally by calling `admission_control.evaluate` directly — not by a parallel re-implementation.
  - Fill model is present for **telemetry + stress overlays only**; `p_fill` does not enter the simulated admission scalar in v1.
  - **Gate-evidence rule:** only canonical runs with every §6 parameter `PINNED` produce Gate-2 / Gate-3 evidence. Runs with any `PLACEHOLDER` are research-grade. Overlay runs are research-only, with the single exception that the **fill-probability stress overlay** is a mandatory pass for Gate 3.
  - **Canonical runs are tagged with the `ProductionProfileId` they simulate. A canonical run whose bound profile differs from the current `ProductionProfileReader` result at verdict-recording time is not admissible evidence (prevents a stale-profile run being re-used across a re-pinning)**.
  - The mandatory Gate-3 stress overlay list (idea §15) — fee stress, spread stress, fill-probability stress, settlement-delay stress, stale-KID/classification freeze, broker-correction replay, order-partial/reject/cancel, re-submit-budget exhaustion, Green/Amber/Red transition — is encoded as an enum `MandatoryStressOverlay` in `contract.py`. `market_policy.CertifyStrategyLcb` refuses certification if any mandatory overlay is missing or failed for the referenced canonical run.
  - Phase 0 pre-screen records the four frozen items (research protocol, OOS window, required net effect size, pre-registered decision rule) **before** any OOS numbers are computed. "Before" is structurally enforced: `RegisterPhase0Protocol` in `market_policy` must succeed (which by invariant requires no prior OOS numbers) before `RunPhase0PreScreen` accepts the protocol id.
  - This module emits intents; it never submits orders.

---

## 4. Cross-module orchestration — canonical flows

The bus delivery order below is deterministic and enforced by `platform_runtime`.

### 4.1 Happy path: intent → admission → fill → journal → tax

```
strategy_simulation.EmitStrategyIntent
  → StrategyIntent
admission_control.EvaluateAdmission
  ├─ read: GovernanceParameterReader,
  │        TradableLineStatusReader (intrinsic state only, [C12]) +
  │          ComplianceStalenessReader [C12] +
  │          ProductionProfileReader + V1TerminalStateReader
  │          → compose_live_admissibility(...) → AdmissionInputs.live_admissibility,
  │        TradableLineLiquidityCapsReader [C5],
  │        StrategyCertificationReader, ChannelStateReader (6 channels inc. COMPLIANCE [C9]),
  │        ConservativeInputReader (for any AMBER channel),
  │        CashStateReader (projected) + broker-authoritative cash via ops_reconciliation,
  │        SettlementRegimeReader, ReSubmitBudgetReader, ReSubmitWindowBudgetReader,
  │        ChurnBudgetReader,
  │        PlannedTaxFrictionPerTradeReader [C7]  ← per-trade tax-friction for Ĉ_RT,
  │        MarketSnapshotReader, FeeScheduleReader (component-decomposed [C4]), TierReader
  │        (PlannedTaxPostingReader is NOT on the per-trade path [C7]; it is annual-cadence
  │         for AumMinComputer on the Gate-4 / tier-up evaluation path only)
  └─ emit: OrderAdmitted | OrderRejected  [with frozen AdmissionInputs + AdmissionVerdict [C2]]
portfolio_accounting (on OrderAdmitted)
  → ReserveCashFor → ReservedCashChanged
execution.SubmitOrder (on OrderAdmitted)
  ├─ read: MarketSnapshotReader (current top-of-book + quoted spread)
  ├─ construct: deterministic limit price (§13 of the idea)
  ├─ emit: LimitPriceConstructed
  → OrderSubmitted → BrokerOrderAck → BrokerFill (or terminal)
portfolio_accounting (on BrokerFill, CommissionReported, StatementPostingReceived)
  → BookFill + BookCommission + BookExchangeFee + BookClearingFee + BookRegulatoryFee + BookTransactionTax
      → FillBooked, Posted, CommissionBooked, ExchangeFeeBooked,
        ClearingFeeBooked, RegulatoryFeeBooked, TransactionTaxBooked, ReservedCashChanged  [C4]
  → TurnoverWindowUpdated
tax (on FillBooked)
  → open/update tax lot → StatutoryTaxStateUpdated
ops_reconciliation (continuous)
  → ingest live + statement + journal views → ChannelStateChanged as needed
```

### 4.2 Corrections replay

```
execution.IngestBrokerCallback (correction)
  → BrokerCorrection (shares exec_correction_group_id with original)
portfolio_accounting (on BrokerCorrection)
  → BookCorrection (adjusting entry referencing original JournalEntryId)
  → JournalCorrectionPosted, ReservedCashChanged (if applicable)
tax (on corrected FillBooked view)
  → recompute candidate lot state
  → if fiscal year already closed: TaxExceptionOpened (plus optional ReopenFiscalYear)
  → else: StatutoryTaxStateUpdated in-line
platform_runtime.ReplayStream (any time)
  → under canonical ordering (coalesce(broker_perm_id, fallback_identity),
     exec_correction_group_id, occurred_at) AND a pinned IdFactory replay seed,
     all downstream projections reproduce identical economic state (§10 Rule 4)
```

### 4.3 Data-state transitions

```
ops_reconciliation observes divergence (e.g. TWS vs Flex, or forecast vs broker settlement)
  evaluates against GovernanceParameterReader("tolerated_divergence_band", channel)
                and GovernanceParameterReader("reconciliation_grace_window", channel)
  → ChannelStateChanged(AMBER | RED) on the specific channel

admission_control on every EvaluateAdmission:
  if ChannelStateReader(any relevant channel) == RED → OrderRejected(RED_CHANNEL(...))
  if AMBER → read ConservativeInputReader for divergent inputs
  if GREEN → normal path
```

### 4.4 Tax annual close

```
tax.ComputeAnnualReconciliation(fiscal_year)
  if residual within de-minimis → TaxReconciliationClosed
  else → TaxReconciliationOpened + TaxExceptionOpened (one or more)

engine-authored candidates:
  tax.ProposeBridgeAdjustment | tax.ProposeDeemedTransaction
    → BridgeAdjustmentProposed | DeemedTransactionProposed
    → visible in ProposedCandidateReader; no ledger posting yet

manual review (off-system); reviewer signs off

tax.CloseTaxException via:
  - UpdateStatutoryState (corrective) with new evidence, OR
  - RecordBridgeAdjustment with reason + evidence (may cite prior Proposed event id)
      → BridgeAdjustmentBooked
      → portfolio_accounting.BookBridgeAdjustment
      → BridgeAdjustmentPosted  [journal entry under distinct PostingAccountCode], OR
  - signed de-minimis declaration recorded in the exception

once all exceptions closed:
  → TaxReconciliationClosed
```

### 4.5 Phase 0 pre-screen

```
market_policy.RecordEvidence (research protocol document)
market_policy.RecordEvidence (OOS window definition)
market_policy.RecordEvidence (required net effect size — pinned numeric threshold)
market_policy.RecordEvidence (pre-registered decision rule — pass iff X)
market_policy.RegisterPhase0Protocol(these four evidence ids, signed_by)
  invariant: refuses if any OOS numeric for the referenced research_config already exists
  → Phase0ProtocolRegistered

strategy_simulation.RunPhase0PreScreen(protocol_id)
  deterministic OOS evaluation against the frozen window
  → PhaseZeroPreScreenFrozen (records the inputs actually used)
  → PhaseZeroPreScreenEvaluated(verdict = PASS | FAIL, realised_effect, threshold)

market_policy.RecordPhase0Verdict(protocol_id, verdict, evidence_id, reviewer_id)  [C14]
  → Phase0VerdictRecorded(protocol_id, verdict)

On PASS: Phase-A engineering (build step 1 onwards) is unblocked.

On FAIL: Phase-A engineering is BLOCKED. [C14] FAIL does NOT declare a V1
  terminal state (the V1TerminalState enum does not have a FAIL value).
  Re-entry requires one of:
    - a FRESH pre-screen against a NEW research_config_id with its own four
      evidence items re-registered (silent re-evaluation with a relaxed
      threshold is refused by RegisterPhase0Protocol's structural invariant),
    - a signed governance decision recording a v1 re-scope per idea §16:
      (i) raise capital, (ii) narrow to a single buy-and-hold instrument with
      no rotation, or (iii) close v1 without an execution engine.
```

### 4.6 Terminal state declaration

```
Gate verdicts accumulate: GateVerdictReader(1..4)
  — each verdict carries status ∈ {PROVISIONAL, DEFINITIVE, PASS, FAIL,
                                   INVALIDATED, DEFERRED, UNASSERTED}  [C14]

For gates that have been attempted but failed or been invalidated, the
governance options are:
  (a) remediate and record a fresh PASS verdict, or
  (b) formally defer via market_policy.DeferGateVerdict(gate, evidence_id,
      reviewer_id) under a signed continuation-programme decision  [C14]
      → GateVerdictDeferred(gate, status=DEFERRED)

market_policy.DeclareV1TerminalState(state, evidence_id)  [C14]
  invariants:
    NARROW_SUCCESS requires:
      - GateVerdictReader(1).status == DEFINITIVE
      - For each g ∈ {2, 3, 4}: GateVerdictReader(g).status
          ∈ {UNASSERTED, DEFERRED}
      - Gates with status FAIL or INVALIDATED are NOT admissible for
        NARROW_SUCCESS; they must first be remediated (fresh PASS) or
        formally deferred (fresh DEFERRED under a signed continuation
        programme per idea §1 "Narrow-success state")
    FULL_SUCCESS requires:
      - GateVerdictReader(1).status == DEFINITIVE
      - GateVerdictReader(2).status == PASS
      - GateVerdictReader(3).status == PASS
      - GateVerdictReader(4).status == PASS
      - No gate may be DEFERRED under FULL_SUCCESS
  → V1TerminalStateDeclared
```

This event is the precondition for either (a) entering the continuation programme (narrow-success) or (b) moving to v2 scoping (full-success).

### 4.7 Route re-pinning  

Idea §5 ("Re-pinning") and idea §12 ("Gate-2 scope: closure, not selection") require that a change of pinned route during v1 is a signed governance event that reopens Gate 2. The flow is:

```
market_policy.RecordEvidence (public pricing / broker policy shift)
market_policy.RecordEvidence (re-pinning rationale)
market_policy.RePinProductionProfile(old_profile_id, new_profile_id, rationale_evidence_id)
  → ProductionProfileRePinned
  → for every GateVerdictRecorded(gate=2, production_profile_id=old) with status=PASS:
       GateVerdictInvalidated(verdict_id, reason="profile re-pinned")
  → [C13] for every active StrategyCertified whose bound canonical_run_id
    resolves to a simulator run with production_profile_id == old_profile_id:
       StrategyCertificationInvalidated(certification_id,
                                        reason="profile re-pinned")
    — this fires ATOMICALLY with the ProductionProfileRePinned event; there
      is no grace window in which a stale certification remains admissible.
      Subsequent StrategyCertificationReader reads return status=EXPIRED for
      every invalidated certification.

Consequence:
  - admission_control.EvaluateAdmission now resolves to the new profile via
    ProductionProfileReader; any in-flight admission that has NOT yet
    emitted OrderAdmitted is re-evaluated under the new profile and will
    fail the new profile-match check in compose_live_admissibility [C12]
    until a new approved tradable line exists bound to the new profile.
  - Existing Gate-3 certifications are EXPIRED [C13] — strategies cannot
    admit new rotation trades until re-certified under the new profile,
    which requires:
      (i) a fresh canonical simulator run bound to the new profile,
      (ii) a fresh mandatory fill-probability stress overlay run,
      (iii) a fresh CertifyStrategyLcb command under the new Gate 2.
  - A fresh Gate-2 verdict must be recorded against the new profile before
    the new profile is production-supported; live trading that was operating
    pre-re-pin falls back to Tier-0 plumbing until Gate 2 closes for the new
    profile.
  - Open positions are not force-liquidated; no new rotation trades are
    admitted until the new profile clears Gate 2 and strategies are re-certified
    under Gate 3.
```

### 4.8 Gate verdict recording

The linkage from rehearsal artefact → recorded verdict is explicit:

```
Gate 1 [C8] — two-stage:

  Stage A — PROVISIONAL (unlocks Tier-0 micro-live):
    after the end-to-end Gate-1 rehearsal (§8.10) passes on the combined
    synthetic broker stream (exercising the full normal + correction event
    shapes) + Phase-B paper stream. At PROVISIONAL stage there is no
    archived LIVE stream yet — that is what DEFINITIVE unlocks. "Archived"
    here means "archived synthetic":
    market_policy.RecordGateVerdict(gate=1, status=PROVISIONAL,
                                    rehearsal_artefact_id, reviewer_id,
                                    production_profile_id=current)
      → GateVerdictRecorded(gate=1, status=PROVISIONAL)
    Consequence: Tier-0 micro-live is permitted; Gate 2 evaluation is NOT yet
    admissible, because Gate 2 requires live exemplars that Tier-0 is about to
    produce.

  Stage B — DEFINITIVE (prerequisite for Gate 2 evaluation):
    after the pinned number of weeks of Tier-0 micro-live reconcile cleanly
    against archived + paper replay (no unexplained residuals, no Red
    channels, Amber within band):
    market_policy.RecordGateVerdict(gate=1, status=DEFINITIVE,
                                    tier0_evidence_bundle_id, reviewer_id,
                                    production_profile_id=current)
      → GateVerdictRecorded(gate=1, status=DEFINITIVE)
    Consequence: Gate 2 evaluation window opens; V1 terminal-state declaration
    (NARROW_SUCCESS or FULL_SUCCESS) becomes admissible.

Gate 2: after the pinned-route fee-closure evidence is complete — Gate 1
  status=DEFINITIVE, all §6 parameters PINNED, exemplar coverage across the
  ticket-size distribution, modelled fees within tolerance at component
  granularity [C4] (commission / exchange / clearing / regulatory /
  transaction-tax each reconciling independently), realised slippage within
  tolerance, re-submit fill-rate tracking within tolerance:
  market_policy.RecordGateVerdict(gate=2, evidence_bundle_id, reviewer_id,
                                  production_profile_id=current)

Gate 3: after a canonical simulator run with all §6 parameters PINNED, bound to
  the current production_profile_id, plus a passing fill-probability stress
  overlay plus all other MandatoryStressOverlay runs:
  market_policy.CertifyStrategyLcb(strategy_id, canonical_run_id,
                                   fill_stress_run_id, gate3_evidence_bundle_id,
                                   reviewer_id)
    invariants: OverlayCoverageReader(canonical_run_id).mandatory_overlays_missing
                must be empty; canonical_run_id.production_profile_id must match
                current ProductionProfileReader.
    → StrategyCertified + GateVerdictRecorded(gate=3)

Gate 4: after AUM_min(k, r, t) is met for the proposed post-trade tier:
  market_policy.RecordGateVerdict(gate=4, aum_evidence_id, reviewer_id,
                                  target_tier, production_profile_id=current)
    → GateVerdictRecorded
  market_policy.SetTier(new_tier, gate4_verdict_id)
    → TierChanged

No gate verdict is admissible unless its production_profile_id matches the
current ProductionProfileReader at the time of recording.
```

---

## 5. Dependency / import rules

Module dependency graph (compile-time enforced; add a `tach` or `import-linter` ruleset):

```
kernel                    (imports nothing from this repo)
  ← events                (imports kernel only; zero logic, pure payload types)  [C1]
    ← platform_runtime    (imports kernel, events; no static import of any domain
                           contract — the canonical-ordering key is injected at
                           composition time as a Callable[[Event], tuple])
    ← market_policy       (imports kernel, events)
    ← execution           (imports kernel, events, market_policy.contract)
      ← portfolio_accounting   (imports kernel, events, market_policy.contract, execution.contract)
        ← tax                  (imports kernel, events, market_policy.contract, execution.contract,
                                 portfolio_accounting.contract for event payload types
                                 (DistributionBooked, JournalCorrectionPosted) only)
          ← ops_reconciliation (imports kernel, events, market_policy.contract, execution.contract,
                                portfolio_accounting.contract, tax.contract)
            ← admission_control (imports kernel, events, contracts of market_policy, execution,
                                 portfolio_accounting, ops_reconciliation, tax)
              ← strategy_simulation (imports kernel, events, contracts of market_policy, execution,
                                     portfolio_accounting, ops_reconciliation, tax, admission_control)
```

Rules:

1. `kernel` imports nothing from this repo.
2. `events` **[C1]** imports `kernel` only; contains zero business logic, zero `Protocol`s, and zero query types — pure dataclasses and enums for cross-module payloads (`StrategyIntent`, `OrderAdmitted`, `OrderRejected`, `AdmissionInputs`, `AdmissionVerdict`, `FillBooked`, `BridgeAdjustmentBooked`, `RejectReason`, `PredicateName`, `PredicateResult`, `FeeBreakdown`). **Selection rule:** a payload type belongs in `events.contract` iff it either (a) would create a cross-module contract cycle if placed in one module's `contract.py`, or (b) is consumed unchanged by three or more domain modules. `StatutoryTaxStateUpdated` and `DistributionBooked` do not meet either criterion (each is consumed by one downstream module in a one-way import relationship) and therefore remain on the emitting module's `contract.py`.
3. Every other module imports `kernel` and `events`. **[M1]** `platform_runtime` takes its canonical-ordering key as a `Callable[[Event], tuple]` parameter to `ReplayStream`; at composition time the application's composition root wires in `execution.contract.CanonicalOrderingKey` as that callable. `platform_runtime` therefore has no static import of `execution` (not even under `TYPE_CHECKING`), removing the final cycle workaround.
4. Domain modules may import another module's `contract.py` **only**; never its internal files. Cross-module payload types are imported from `events.contract` rather than from the emitting module's `contract.py`, per the selection rule in rule 2.
5. **Cycles eliminated [C1].** The plan's `admission_control`↔`strategy_simulation` and `portfolio_accounting`↔`tax` contract cycles are gone: `StrategyIntent`, `AdmissionInputs`, `AdmissionVerdict`, `OrderAdmitted`, `OrderRejected`, `FillBooked`, and `BridgeAdjustmentBooked` are all defined in `events.contract` and imported identically by both sides of each former cycle. The remaining asymmetric dependencies (e.g. `strategy_simulation` imports `admission_control.contract` for the pure `evaluate` and `compose_live_admissibility` functions **[C12]**) are one-directional and acyclic. `TYPE_CHECKING` is no longer used to paper over contract cycles anywhere in the codebase.
6. **[C12] Staleness-composition location.** The composite "live admissibility" check that combines intrinsic tradable-line state (owned by `market_policy`) with evidence freshness (owned by `ops_reconciliation`) lives in `admission_control.contract` as the pure function `compose_live_admissibility(...)`. It is deliberately **not** in `market_policy` — that would require `market_policy` to import `ops_reconciliation.contract`, which the dependency graph forbids. The simulator calls the same function under the simulator-validity rule.
7. Tests may import a module's internals but only within that module's test suite.
8. Adapters to the outside world (IBKR, Flex, BMF, BaFin, KID publishers, reviewer tooling) live inside the owning module and are injected via kernel `Protocol`s.
9. `datetime.now()`, `uuid.uuid4()`, filesystem, network, and DB calls do not appear in domain code — only in adapters or `platform_runtime`.
10. Every `contract.py` is type-only: class definitions, `Protocol`s, `NewType`s, `Enum`s, `TypedDict`s, dataclasses, and pure functions with no side effects. Runtime wiring lives in each module's internal composition root.

---

## 6. Build order

Drives directly from §14 sequencing of the idea: reconcile first, governance-metadata second. Phase-0 pre-screen is pulled forward to step 0 per idea §16 ("before Phase-A engineering begins").

0. **`kernel` (Phase-0 subset) + `events` [C1] (Phase-0 subset) + `market_policy` (Phase-0 subset) + `strategy_simulation` (Phase-0 harness only)** — enough to run `RegisterPhase0Protocol` → `RunPhase0PreScreen` → `RecordPhase0Verdict` **[C14]**. Subset of `kernel`: ID types needed by Phase-0 (`ResearchConfigId`, `Phase0ProtocolId`, `EvidenceRecordId`, `GovernanceDecisionId`), `Money`, `Instant`, `BusinessDate`, `EffectiveDateRange`, `Outcome`, `DomainError`, `Phase0Verdict` enum **[C14]**. Subset of `events.contract`: evidence-registry payloads only — the remainder (cross-module admission payloads, etc.) freeze at step 1. Subset of `market_policy`: `RecordEvidence`, `RegisterPhase0Protocol`, `RecordPhase0Verdict` **[C14]**, `Phase0ProtocolReader`. Subset of `strategy_simulation`: Phase-0 harness only (emits `PhaseZeroPreScreenFrozen` and `PhaseZeroPreScreenEvaluated`; does not emit `StrategyIntent`). **Exit gate [C14]:** `Phase0VerdictRecorded(verdict=PASS)` → proceed to step 1. `Phase0VerdictRecorded(verdict=FAIL)` → step 1 is blocked until either a fresh protocol passes or a signed re-scope decision under idea §16 is recorded. A `FAIL` is **not** a V1 terminal state declaration.
1. **`kernel` + `events` (full)** — freeze both surfaces. `events` includes the full cross-module payload set (`StrategyIntent`, `AdmissionInputs`, `AdmissionVerdict`, `OrderAdmitted`, `OrderRejected`, `FillBooked`, `BridgeAdjustmentBooked`, `RejectReason`, `PredicateName`, `PredicateResult`, `FeeBreakdown`, `LiveAdmissibility`, `LiveAdmissibilityReasonCode`, `ComplianceStalenessSnapshot`, `TradableLineIntrinsicState` — the last four added for **[C12]**). Additions after this step require a version bump and a coordinated release across every downstream module, by the same rule as `kernel` changes.
2. **`platform_runtime`** — event store, in-process bus, outbox, replay runner, projection runner, **seeded `IdFactory` for replay mode**. **[M1]** `ReplayStream` takes the canonical-ordering key as an injected `Callable[[Event], tuple]`; no static import of `execution`. Tested with synthetic event streams whose ordering key is a simple stand-in (e.g. `lambda e: (e.event_id,)`); the real `execution.contract.CanonicalOrderingKey` is wired in later at composition time.
3. **`execution` (minimum)** — broker callback ingest, canonical identity, correction grouping, fallback identity, re-submit budget readers, **`MarketSnapshotReader` adapter**, **deterministic limit-price construction [C3] via `LimitPriceComputer` pure function on `SubmitOrder`**, **`execDetails` correction-detection rule [C10]**, **live-vs-paper adapter configuration tag [C11]** (the adapter itself ships with both configurations; the pinned `ProductionProfile.runtime_configuration` selects which is active at runtime). Exports `CanonicalOrderingKey` as a pure function on `execution.contract`; the application composition root wires this into `platform_runtime.ReplayStream`. Not yet submitting orders live.
4. **`portfolio_accounting` (minimum)** — journal, cash decomposition, reserved cash, correction-safe posting, churn-budget projection, `AumMinComputer`, **fee-component posting split [C4]** (`BookCommission`, `BookExchangeFee`, `BookClearingFee`, `BookRegulatoryFee`, `BookTransactionTax` each with their own `PostingAccountCode`). **Deterministic replay demonstrated end-to-end against synthetic broker streams — this is the Gate-1 PROVISIONAL precondition artefact.** Replay tests run in CI on every change to `execution` or `portfolio_accounting`. **Step-4 regime note:** this step binds to a single hard-coded `SettlementRegimeId` for the pinned production profile (e.g. `"DE_CASH_EQ_T+2_V1"`); the full `SettlementRegimeReader` implementation, including effective-dated registration and the `SettlementRegimeVersioned` event flow, lands with `market_policy` in step 5. Step 4 tests use a stub `SettlementRegimeReader` that returns the hard-coded regime for any query; the regime-change-in-flight guardrail is still exercised via synthetic test streams that inject a simulated regime change.
5. **`market_policy` (full)** — reference data, evidence registry, tradable-line approval (exposing **intrinsic state only per `TradableLineStatusReader` [C12]**; the composed `is_live_admissible` rule lives in `admission_control`), settlement-regime registration, pinning of placeholders, gate-verdict recording (with **PROVISIONAL vs DEFINITIVE status for Gate 1 [C8]**, plus `DeferGateVerdict` and `RecordPhase0Verdict` **[C14]**), XLM snapshots, fee schedules (decomposed by component **[C4]**), **per-ISIN liquidity caps reference data [C5]**, terminal-state declaration, production-profile pinning and re-pinning (emitting `StrategyCertificationInvalidated` on re-pin **[C13]**), tier management. Scope capped at the 2–4 live ISINs.
6. **`tax`** — two-ledger shape, event-driven statutory updates, bridge + exception workflow, auto-exception on cross-year broker corrections, candidate-proposal commands and events separated from reviewer-authorised record commands, **`PlannedTaxFrictionPerTradeReader` [C7]** (per-trade tax-friction estimator, distinct from annual `PlannedTaxPostingReader`). Operation kept minimal per §9's "v1 operational minimality".
7. **`ops_reconciliation`** — Green/Amber/Red machinery across all six channels (`BROKER_CASH`, `POSITIONS`, `FEES`, `TAX_POSTING`, `SETTLEMENT`, **`COMPLIANCE` [C9]**), freeze signal into admission, fallback-identity rate monitoring, re-submit-window exhaustion as a `FEES` break, **reference-data staleness alarms feeding `COMPLIANCE` [C9]**, fee-component-level `FEES` channel evaluation **[C4]**, `ComplianceStalenessReader` published for consumption by `admission_control` **[C12]** (not by `market_policy`).
8. **`admission_control`** — per-trade rule with broker-cash-authoritative reading; reason-code-rich rejections via `AdmissionVerdict` typed record **[C2]**; pure `evaluate` predicate exported for shared use by the simulator; pure `compose_live_admissibility` predicate exported **[C12]** (called at `AdmissionInputs` materialisation time to combine `TradableLineStatusReader`, `ComplianceStalenessReader`, `ProductionProfileReader`, and `V1TerminalStateReader` into a single `LiveAdmissibility` record). `FeeScheduleReader` (component-decomposed **[C4]**), `TierReader`, **`TradableLineLiquidityCapsReader` [C5]**, and **`PlannedTaxFrictionPerTradeReader` [C7]** wired in.
9. **`strategy_simulation` (full)** — canonical simulator bound to pinned profile, overlay runner, mandatory-stress-overlay enforcement, Gate-3 evidence production, canonical-run binding to `ProductionProfileId`, identical `LimitPriceComputer` consumption **[C3]**, identical `compose_live_admissibility` consumption **[C12]**.

Paper trading (Phase B) begins after step 8 using the `execution` adapter in `PAPER` configuration. Micro-live (Phase C / Tier 0) begins after Gate 1 status=`PROVISIONAL` closes on the synthetic + paper streams **[C8, M2]**; Gate 2 evaluation may begin only after Gate 1 status=`DEFINITIVE` closes on Tier-0 micro-live.

---

## 7. Validation matrix — idea section → scaffold location

| Idea ref | Concern | Owned by                                                                                                                                                                                                                 |
|---|---|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| §1 / §17 | Four sequential gates; pinned parameters; verdicts | `market_policy` (`RecordGateVerdict`, `GateVerdictReader`, `GovernanceParameterReader`); verdicts carry `production_profile_id` per §4.8                                                                       |
| §1 "Narrow-success" | Gate-1-only terminal state is legitimate and declarable | `market_policy.DeclareV1TerminalState` + `V1TerminalStateReader`                                                                                                                                                         |
| §3 | Broker/market micro-assumptions | `market_policy` reference data (settlement regimes, Xetra session definitions, XLM facts, broker routing restrictions)                                                                                                   |
| §3 German fund-tax | Vorabpauschale / Teilfreistellung / §22 / Basiszins as dated data | `market_policy` (dated reference + evidence) + `tax` (Ledger A, statutory-state numeric computations internal)                                                                                                           |
| §4 Rule 1 | Reject unsupported | `admission_control` reason codes                                                                                                                                                                                         |
| §4 Rule 4 | Journal is sole economic truth | `portfolio_accounting` invariants                                                                                                                                                                                        |
| §4 Rule 5 | Human sign-off on ambiguous tax/compliance | `tax.Propose*` / `tax.Record*` candidate/approval split ; `market_policy.RecordGovernanceDecision` requires reviewer id                                                                                        |
| §4 Rule 6 | Green/Amber/Red data states | `ops_reconciliation` — 6-channel coverage including `SETTLEMENT` and `COMPLIANCE` **[C9]**                                                                                                                                |
| §5 | Pinned v1 profile (SmartRouting Fixed, continuous, DAY limit, EUR); shadow profiles representable but not live-admissible; re-pinning flow | `market_policy.PinProductionProfile` / `RePinProductionProfile` (emits `StrategyCertificationInvalidated` **[C13]**); `ProductionProfileReader`; composed `compose_live_admissibility` in `admission_control` **[C12]** combines intrinsic `TradableLineStatusReader` with `ComplianceStalenessReader` + `ProductionProfileReader` + `V1TerminalStateReader`; exactly one live-admissible pinned profile at a time                |
| §5 | Deterministic limit-price construction at submit | `execution.SubmitOrder` owns construction via `LimitPriceComputer` pure function **[C3]**; `LimitPriceConstructed` event records side, bid, ask, α, β, chosen limit                                                             |
| §6 | N_min, AUM_min, per-trade rule, re-submit cost folded into Ĉ_RT, tier policy | `admission_control` predicates (reads `FeeScheduleReader` decomposed **[C4]**, `TierReader`, `TradableLineLiquidityCapsReader` **[C5]**, `PlannedTaxFrictionPerTradeReader` **[C7]**); `portfolio_accounting.AumMinComputer` used on Gate-4 cert path only ; inputs read from `market_policy` and `execution`   |
| §6 | Governance parameters pinned vs placeholder; LCB calibration bundle; fill-model calibration; **limit-price aggression parameter [C3]** | `market_policy.GovernanceParameterReader` with `ParameterName` enum expanded (includes `limit_price_aggression_fraction`, `limit_price_max_spread_budget_bps`, reference-freshness bounds); `strategy_simulation` tags runs accordingly                                                                                     |
| §6 | Strategy-level LCB certification as admission input; bound to canonical-run + fill-stress-run ids | `market_policy.StrategyCertificationReader` returns both run ids; `CertifyStrategyLcb` rejects if mandatory stress overlays missing; canonical-run `production_profile_id` must match current pinned profile   |
| §6 | `cash_buffer(t)` sizing rule (max of floor vs tax upper bound) — annual cadence | `portfolio_accounting.AumMinComputer` composes `PlannedTaxPostingReader` (annual) + `cash_buffer_floor` parameter; per-trade tax-friction is a separate reader **[C7]**                                                                                                                 |
| §6 | Churn-budget window and cap (plus unit disambiguation) | `portfolio_accounting.ChurnBudgetReader`; parameters `churn_window`, `churn_cap`, `churn_metric` (plan-level unit disambiguation )                                                                            |
| §6 | Tier transitions are governance events, not auto-activated | `market_policy.SetTier` / `TierReader` ; `admission_control` reads tier, does not compute                                                                                                                       |
| §6 | Cumulative re-submit budget per rolling window | `execution.ReSubmitWindowBudgetReader` separate from per-decision cap; exhaustion raises `FEES` break via `ops_reconciliation`                                                                                           |
| §7 | Universe policy; tradable line; XLM in universe admission and reserve role only | `market_policy` (tradable lines, XLM reference via `XlmReader`, `RegisterXlmSnapshot`); `admission_control` (no XLM in per-ticket predicate); `strategy_simulation` (XLM in reserve `R_nonfx`)                            |
| §7 | Research universe (5–10 ETFs) vs live universe (2–4 approved lines) | research-only ETFs live as `ReferenceDataReader` entries without `TradableLineApproved`; live-universe requires an approval with the governance-intent `is_live_admissible=True` flag on `ApproveTradableLine`; actual runtime admissibility is composed at admission time per `compose_live_admissibility` **[C12]**                                                             |
| §7 / §8 | PRIIPs KID validity date-bounded; evidence staleness freezes tradable line | composed in `admission_control.compose_live_admissibility` **[C12]**: intrinsic KID validity from `market_policy.TradableLineStatusReader`, evidence staleness from `ops_reconciliation.ComplianceStalenessReader` on the `COMPLIANCE` channel **[C9]**; staleness detection is an `ops_reconciliation` responsibility |
| §8 | Tradable-line-centric compliance with fund + listing + currency + broker conId + date | `market_policy.TradableLineApproved` + fund-level evidence sharing                                                                                                                                                       |
| §9 | Two-ledger tax, reconciliation, exception workflow, precedence | `tax` (complete); candidate/approval split                                                                                                                                                                     |
| §9 | Both ledgers post to journal under distinct account codes | `tax.BridgeAdjustmentBooked` (in `events.contract` **[C1]**) → `portfolio_accounting.BookBridgeAdjustment` → `BridgeAdjustmentPosted` under dedicated `PostingAccountCode`; Ledger A statutory state non-cash                                            |
| §9 / §3 Broker reporting | Basiszins / KID / classification freshness alarms | `ops_reconciliation.ComplianceStalenessReader` + `COMPLIANCE` channel **[C9]**; feeds annual-tax-close checklist advisory                                                                                   |
| §10 | Cross-module event payloads live in shared contract, not in one owner | `events/contract.py` **[C1]**; former contract cycles between `admission_control`↔`strategy_simulation` and `portfolio_accounting`↔`tax` eliminated                                                                 |
| §10 | Append-only journal + execution-event ledger separation | `portfolio_accounting` + `execution`                                                                                                                                                                                     |
| §10 | Canonical identity (`broker_perm_id`), correction groups, replay invariance, `execDetails` detection | `execution` + `platform_runtime.ReplayStream` using `execution.CanonicalOrderingKey` + seeded `IdFactory`; `execDetails` same-`permId`-different-`execId` rule documented as invariant **[C10]**                      |
| §10 Fallback identity | `PermId = 0` fallback composite with origin flag; spike monitoring | `execution.FallbackIdentity` + `ops_reconciliation` subscription to `FallbackEventRecorded`                                                                                                                              |
| §11 | Cash decomposition, reserved cash, settlement regime as forecast | `portfolio_accounting` (forecast owner) + `market_policy` (regime reference)                                                                                                                                             |
| §11 | Broker-authoritative cash for admission | `admission_control` reads live via `ops_reconciliation.ConservativeInputReader` / TWS adapter, never overrides broker                                                                                                    |
| §11 | v1 no in-flight regime transitions | `portfolio_accounting` invariant: exactly one active regime over v1 horizon; regime-change-in-flight is a reconciliation break                                                                                           |
| §11 | Settlement forecast vs broker settlement divergence visible | `SETTLEMENT` channel in `ops_reconciliation`                                                                                                                                                                             |
| §12 | Pinned fee model, Gate-2 closure on pinned route, conservative-higher-cost on ambiguity, **component granularity** | `market_policy.FeeScheduleReader` decomposed into commission/exchange/clearing/regulatory/transaction-tax sub-rules **[C4]**; `portfolio_accounting` posts each component separately; `strategy_simulation` simulates component-wise; `admission_control` materialises all components  |
| §12 | Fee exemplar coverage across ticket-size distribution (by component) | `market_policy` evidence registry stores exemplar observations per component; Gate-2 verdict rejects on any-component coverage gap **[C4]**                                                                                                              |
| §12 | Re-pinning reopens Gate 2 | `market_policy.RePinProductionProfile` invalidates old Gate-2 verdict; new profile must earn its own Gate-2                                                                                                     |
| §13 | Ticket-size-native liquidity gate, deterministic limit-price, re-submit budget | `admission_control` predicates + per-ISIN caps via `TradableLineLiquidityCapsReader` **[C5]**; `execution` re-submit budgets + deterministic `LimitPriceComputer` **[C3]**                                                     |
| §13 | `p_fill` as telemetry + stress overlay, NOT admission scalar | `admission_control.AdmissionVerdict.p_fill_telemetry` field **[C2]**; `strategy_simulation` uses it in overlays; mandatory fill-probability stress overlay enforced via `MandatoryStressOverlay` enum and `OverlayCoverageReader` |
| §13 | Fill-model calibration procedure pinned under governance | Parameters `fill_model_min_exemplar_count`, `fill_model_calibration_window`, `fill_model_refresh_cadence` in `ParameterName`                                                                                    |
| §13 | Emergency manual override with signed exception | `execution.AuthorizeFallbackEvent(governance_exception_id, …)` + `market_policy.OpenGovernanceException`                                                                                                                 |
| §14 | Reference data, evidence registry, as-of versioning | `market_policy`; every simulator run tags snapshot ids per `§14 As-of versioning`                                                                                                                                        |
| §14 Sequencing | Kernel reconciles first; governance metadata at live-universe scope only | §6 of this plan ("Build order")                                                                                                                                                                                          |
| §15 | One canonical simulator + overlays; gate-evidence rule; simulator-validity rule | `strategy_simulation` invariants; simulator-validity enforced by direct call to `admission_control.evaluate` + `LimitPriceComputer` **[C3]**; canonical runs bound to `ProductionProfileId` + `runtime_configuration` **[C11]**                                                     |
| §15 | Mandatory stress-overlay list | `MandatoryStressOverlay` enum; `OverlayCoverageReader` surfaces missing overlays; `CertifyStrategyLcb` rejects on missing                                                                                                |
| §16 Phase 0 | Frozen four items + pre-registered decision rule; ordering enforced structurally | `market_policy.RegisterPhase0Protocol` refuses if any OOS numeric already exists; `RunPhase0PreScreen` requires a registered protocol                                                                                    |
| §17 Gate 1 — two-stage **[C8]** | PROVISIONAL on archived+paper unlocks Tier-0; DEFINITIVE on Tier-0 clean reconciliation unlocks Gate 2 | `market_policy.RecordGateVerdict(gate=1, status={PROVISIONAL|DEFINITIVE}, …)` per §4.8                                                                                                                             |
| §17 Gate 2 | Fee-model closure on pinned route only; pinned §6 parameters; exemplar coverage per component **[C4]**; requires Gate 1 DEFINITIVE | `strategy_simulation` gate-evidence rule + `market_policy` pinning + per-component exemplar coverage; verdict carries `production_profile_id`                                                                                 |
| §17 Gate 3 | Strategy-level LCB + mandatory fill-probability stress | `strategy_simulation` overlay + `market_policy.CertifyStrategyLcb` bound to canonical run id + fill-stress run id; run's `production_profile_id` must match current                                             |
| §17 Gate 4 | `AUM_min(k, r, t)` met | `portfolio_accounting.AumMinComputer` composing `market_policy` parameters + `CashStateReader` + `PlannedTaxPostingReader` (annual); Gate-4 verdict gates `SetTier`                                                     |
| §17 Scope-expansion | All four gates before second route/session; v2 route-comparison forbidden in v1 | `market_policy` invariant — `ApproveTradableLine(is_live_admissible=True)` refuses second live profile without prior `V1TerminalStateDeclared(FULL_SUCCESS)`                                                   |
| §18 | v2 additions without weakening v1 disciplines | No v1 module absorbs v2 concerns. Route-comparison, forward-dated regimes, per-trade LCB, two-mode simulator remain additive at `market_policy` and `strategy_simulation` surface without contract breakage              |
| §5 / §7 / §8 **[C12]** | Live-admissibility composition without a static cycle | `admission_control.compose_live_admissibility(intrinsic, staleness, current_pinned_profile, terminal_state, as_of)` — pure function on `admission_control.contract`; `market_policy.TradableLineStatusReader` returns intrinsic state only (no `is_live_admissible` boolean on that reader); `strategy_simulation` calls the same function |
| §5 / §17 **[C13]** | Route re-pinning invalidates strategy certifications, not merely Gate 2 | `market_policy.RePinProductionProfile` atomically emits `StrategyCertificationInvalidated` for every certification bound to the old profile; `StrategyCertificationReader` returns `EXPIRED` for invalidated certifications; re-certification under the new profile requires a fresh canonical run + fresh mandatory fill-probability stress + fresh `CertifyStrategyLcb` |
| §16 **[C14]** | Phase-0 failure ≠ V1 terminal state | `Phase0Verdict = {PENDING, PASS, FAIL}` enum distinct from `V1TerminalState`; `RecordPhase0Verdict(FAIL)` blocks Phase-A engineering (step 1) until either a new protocol passes or a signed re-scope decision is recorded; no `V1TerminalStateDeclared` is emitted on Phase-0 fail |
| §1 "Narrow-success" **[C14]** | Gates 2–4 must be UNASSERTED or DEFERRED (not FAILED) for NARROW_SUCCESS | `market_policy.DeferGateVerdict(gate, evidence_id, reviewer_id)` emits `GateVerdictDeferred(status=DEFERRED)`; `DeclareV1TerminalState(NARROW_SUCCESS)` invariant refuses when any gate 2–4 has `FAIL` or `INVALIDATED` status |

---

## 8.  Test obligations

For AI-assisted implementation, invariants don't enforce themselves. Each module **must** ship with the following automated test coverage before it is considered complete. The CI gate for a module merge is: (a) contract surface compiles and type-checks; (b) the tests below exist and pass; (c) import-linter ruleset passes (§5).

### 8.1 Kernel

- Property tests that `Money` arithmetic refuses cross-currency; that `EffectiveDateRange` correctly implements half-open semantics; that `IdFactory` is deterministic under a pinned seed **and distinct from the live-mode factory's sequence**.

### 8.1bis `events` [C1]

- Import-linter test: `events.contract` imports nothing except `kernel`; no domain module imports `events`'s internals.
- Type-only invariant: every member of `events.contract` is a dataclass, `Enum`, `TypedDict`, or type alias — zero functions, zero `Protocol`s, zero business logic.
- Cross-module contract-cycle test: the import graph from §5 is acyclic — enforced by `tach` or `import-linter` in CI. A new import that introduces a cycle fails the build.
- **[C12] New payload types present:** `LiveAdmissibility`, `LiveAdmissibilityReasonCode`, `ComplianceStalenessSnapshot`, and `TradableLineIntrinsicState` are defined in `events.contract` as frozen dataclasses / enums; both `admission_control.compose_live_admissibility` and `strategy_simulation` import them from `events.contract`, not from any one domain contract.
- **[M3] Selection-rule compliance:** a CI lint enforces that every payload class defined in `events.contract` is consumed by at least two modules; conversely, every payload consumed by ≥3 modules is defined in `events.contract` and not in any single domain's `contract.py`. A violation fails the build.

### 8.2 `platform_runtime`

- Append-only property: random fuzz of event streams never produces an UPDATE/DELETE.
- Replay determinism: for any persisted stream S and any two replay passes **under the same `IdFactory` seed**, downstream projection output is byte-identical.
- Outbox atomicity: simulated subscriber failure does not lose or duplicate an event.

### 8.3 `execution`

- Canonical-identity property: for every `exec_correction_group_id` containing N events, `CorrectedExecutionSetReader` returns exactly the supersession winners.
- Fallback-identity construction: `broker_perm_id = 0` events require a linked `GovernanceExceptionId` or `StatementPostingId`; unlinked fallback fails the command.
- Re-submit budgets: per-decision cap and window cap each enforced independently; integer-edge tests at cap boundaries.
- Replay canonical-ordering-key test: a shuffled event stream replayed under `CanonicalOrderingKey` produces deterministic ordering.
- **Deterministic limit-price construction [C3]:** `LimitPriceComputer(side, snapshot, α, β)` is a pure function; for a fixed input tuple it returns the same `Decimal` on every call. Property test: for `α = 0` the output equals `bid` on BUY and `ask` on SELL; for `α = 1` the output equals `ask` on BUY and `bid` on SELL; for `0 < α < 1` the output lies strictly inside `(bid, ask)`. Edge tests: `β` cap binds when `α · quoted_spread_ccy` exceeds `β · mid / 10000`; `MarketSnapshotReader` returning `None` causes `OrderRejected`; chosen limit exceeding `OrderAdmitted.principal_allowance` causes `OrderRejected`.
- **Simulator/live `LimitPriceComputer` identity [C3]:** calling `LimitPriceComputer` from live `execution.SubmitOrder` and from `strategy_simulation` on the same inputs produces the same output. (Guards against drift if anyone adds a private helper.)
- **`execDetails` correction-detection rule [C10]:** a same-`permId` callback with functionally-identical parameters and a different `execId` is classified as a correction and shares `exec_correction_group_id` with the original. A same-`permId` callback with materially different parameters raises `CorrectionParamMismatch` rather than silently grouping.
- **Paper/live runtime-configuration tag [C11]:** every emitted event carries the `runtime_configuration` tag from the current pinned profile; a replay of an event stream that mixes `LIVE` and `PAPER` events for the same `account_id` raises `MixedRuntimeConfigurationReplay`.

### 8.4 `portfolio_accounting`

- **Economic-state-invariance test (Gate-1 precondition artefact).** Given a representative archived event stream (initially synthetic, later real broker streams), replay **under a pinned `IdFactory` seed** produces identical position / settled / reserved / fee / tax state under any callback-order permutation. **This test runs in CI on every change touching `execution` or `portfolio_accounting` internals.**
- Broker-correction replay: original + correction stream produces same end state as the correction-only stream applied to the corrected view.
- Reserved-cash lifecycle: random admission/terminal sequences never produce a reserved cash greater than the open admitted set.
- Journal append-only: no test can post an UPDATE or DELETE.
- Cross-currency refusal: any journal command with mixed-currency line items fails.
- Regime-change-in-flight guardrail: an attempted post with regime mismatch fails with a specific error.
- **Step-4 stub regime smoke test:** step-4 build passes all `portfolio_accounting` tests with a stub `SettlementRegimeReader` returning a single hard-coded regime; step-5 build re-runs the same tests with the real `SettlementRegimeReader` and must produce identical results.
- **[C4] Fee-component posting-account disjointness:** commission, exchange fee, clearing fee, regulatory fee, and transaction tax each use a distinct `PostingAccountCode`; attempting to post any component under another component's account code fails with a specific error.
- **[C4] Fee-component reconciliation resolution:** a synthetic Gate-2 closure scenario where exchange-fee posts within tolerance but clearing-fee posts outside tolerance blocks Gate 2 — demonstrated by `ops_reconciliation` emitting a `FEES` break pointing at the specific component, not at aggregate fees.
- **[C4] Churn-budget metric disambiguation:** for a pinned `churn_metric = notional_weighted`, `ChurnBudgetReader.used` accumulates notional; for `churn_metric = decision_count`, it accumulates integer counts. Switching the pinned metric and re-computing on the same event stream produces a different `used` value, confirming the reader honours the pinned unit.

### 8.5 `market_policy`

- Additive-only: random updates never mutate prior rows.
- `PinParameter`: attempting to pin an unknown `ParameterName` fails with a contract error.
- `RegisterPhase0Protocol` ordering: refuses if any OOS numeric for the same research config already exists.
- **[C14] `RecordPhase0Verdict`:** emits `Phase0VerdictRecorded(PASS|FAIL)`. A `FAIL` verdict does not emit `V1TerminalStateDeclared`. `RegisterPhase0Protocol` refuses against a `ResearchConfigId` that already has a `Phase0VerdictRecorded(PASS)` or `Phase0VerdictRecorded(FAIL)` — forcing re-entry through a fresh `ResearchConfigId`.
- `CertifyStrategyLcb` binding: refuses without a canonical run id and a fill-probability stress run id; refuses if any mandatory overlay is missing; **refuses if the canonical run's bound `production_profile_id` does not match the current `ProductionProfileReader`**.
- **[C14] `DeclareV1TerminalState`:** `NARROW_SUCCESS` requires Gate-1 `DEFINITIVE` status **[C8]** and, for each gate in `{2,3,4}`, `GateVerdictReader(gate).status ∈ {UNASSERTED, DEFERRED}`. A test that records `Gate-2 FAIL` and attempts `DeclareV1TerminalState(NARROW_SUCCESS)` fails. A test that records `Gate-2 FAIL` followed by `DeferGateVerdict(2, evidence, reviewer)` followed by `DeclareV1TerminalState(NARROW_SUCCESS)` succeeds (formal deferral legitimises the previously-failed gate as out-of-scope for the narrow terminal state). `FULL_SUCCESS` requires Gate-1 `DEFINITIVE` plus Gates 2..4 all `PASS` (not `DEFERRED`, not `FAIL`, not `INVALIDATED`). Declaring either state with Gate-1 only `PROVISIONAL` fails.
- **[C14] `DeferGateVerdict`:** emits `GateVerdictDeferred(gate, status=DEFERRED, evidence_id, reviewer_id)`. `DeferGateVerdict` requires a non-null `evidence_id` pointing to a signed continuation-programme decision record; attempting to defer without one fails.
- Exactly-one-live-admissible-profile invariant: property test across random approval sequences.
- **Scope-expansion refusal:** `ApproveTradableLine(is_live_admissible=True)` with a different `ProductionProfileId` than the current pinned profile is refused unless `V1TerminalStateReader` returns `FULL_SUCCESS`.
- **Re-pinning invalidates prior Gate-2:** after `RePinProductionProfile(old, new, …)`, any prior `GateVerdictRecorded(gate=2, production_profile_id=old, status=PASS)` is followed in the event stream by `GateVerdictInvalidated` referencing that verdict id; `GateVerdictReader(2, as_of)` reports `INVALIDATED` for the old verdict at any as_of after the re-pin.
- **[C13] Re-pinning invalidates strategy certifications.** Given a `StrategyCertified` signed against a canonical run bound to profile `A`, after `RePinProductionProfile(A, B, …)`: (a) a `StrategyCertificationInvalidated` event is emitted in the same transaction, referencing the certification id; (b) `StrategyCertificationReader(strategy_id, as_of_after_repin)` returns `status=EXPIRED` and the underlying invalidation event can be retrieved for audit; (c) the `inputs_hash` of a subsequent `AdmissionInputs` materialisation differs from the pre-repin hash because the certification-state field is now `EXPIRED`. Atomicity test: no intervening `as_of` exists between `ProductionProfileRePinned` and `StrategyCertificationInvalidated` at which admission could read `CERTIFIED` and also see the new profile.
- **[C13] Re-certification restores live admission.** After invalidation under re-pin, recording a fresh `CertifyStrategyLcb` referencing a canonical run bound to the new profile plus a fresh mandatory fill-probability stress overlay restores `StrategyCertificationReader` to `CERTIFIED`. A `CertifyStrategyLcb` that references a canonical run still bound to the old profile is refused.
- **Tier-change authorisation:** `SetTier` requires a valid `GateVerdictRecorded(gate=4, target_tier=new_tier, status=PASS, production_profile_id=current)`; `SetTier` without this fails.
- **Strategy-certification expiry:** a certification signed at `t` with `lcb_refresh_cadence = Δ` is reported as `EXPIRED` by `StrategyCertificationReader` for any as_of `≥ t + Δ`; explicit `ExpireStrategyLcb` before `t + Δ` also produces `EXPIRED`.
- **[C12] Intrinsic tradable-line state only.** `TradableLineStatusReader(line, as_of)` returns `{approval_state, kid_validity_range, fund_level_evidence_pointer, listing_line_evidence_pointer, bound_profile_id}` — no composed `is_live_admissible` boolean. An import-linter rule verifies that `market_policy` never imports `ops_reconciliation.contract`; a test that attempts to read `ComplianceStalenessReader` from inside `market_policy` fails the build.
- **[C5] Per-ISIN liquidity caps.** `TradableLineLiquidityCapsReader` returns the effective-dated caps at `as_of`; attempting to approve a tradable line without a caps record fails. The caps are consumed unchanged by `admission_control` predicate 6 — identity test between the reader output and the materialised field on `AdmissionInputs`.
- **[C8] Two-stage Gate 1.** `GateVerdictReader(1, as_of)` distinguishes `PROVISIONAL` vs `DEFINITIVE`. `RecordGateVerdict(gate=2, …)` is refused while Gate-1 status is only `PROVISIONAL`; the same command succeeds once Gate-1 `DEFINITIVE` has been recorded for the current pinned profile.

### 8.6 `admission_control`

- Predicate-coverage test: every `RejectReason` enum value **[C2]** is reached by at least one test case.
- Pure `evaluate` determinism: same `AdmissionInputs` → same `AdmissionVerdict`, across 10k random input bundles. `AdmissionVerdict.inputs_hash` **[C2]** is identical for identical inputs and differs for any single-field change.
- `INCOMPLETE_INPUTS` test: every field omitted in turn produces `AdmissionVerdict(disposition=REJECT, reason_codes={INCOMPLETE_INPUTS})`; no silent defaulting. **This includes `AdmissionInputs.fee_schedule_snapshot` (decomposed per `FeeBreakdown` **[C4]**), `market_snapshot`, `current_tier`, `liquidity_caps` **[C5]**, and `per_trade_tax_friction` **[C7]**.**
- AMBER-conservative-input test: for each channel (including the new `COMPLIANCE` channel **[C9]**), AMBER state causes `ConservativeInputReader` to be used instead of the default source.
- `p_fill`-not-in-admission test: a decision record with any non-neutral `p_fill_telemetry` value must still produce the same `disposition` as one with a neutral value, holding all other inputs equal.
- Simulator-identity test: same `AdmissionInputs` bundle produces the same `AdmissionVerdict` whether called from live path or from `strategy_simulation`.
- **Tier-read test:** admission predicate 3 reads `AdmissionInputs.current_tier` only; it does not invoke `AumMinComputer` on the per-trade path (enforced by a test that stubs `AumMinComputer` to raise on call and asserts `evaluate` succeeds).
- **Fee-schedule-materialisation test [C4]:** `Ĉ_RT` computed inside `evaluate` from `AdmissionInputs.fee_schedule_snapshot` decomposes into `ĉ_rt_breakdown` with one entry per fee component (commission, exchange, clearing, regulatory, transaction-tax, spread, per-trade tax-friction), and the sum of breakdown entries equals the scalar `Ĉ_RT` used in predicate 1.
- **[C5] Per-ISIN liquidity gate test:** predicate 6 reads `AdmissionInputs.liquidity_caps` — three sub-cases: quoted spread above cap → `REJECT(LIQUIDITY_SPREAD_CAP)`; decision time outside `[liquidity_window_open_utc, liquidity_window_close_utc]` → `REJECT(LIQUIDITY_WINDOW)`; notional above per-order cap → `REJECT(LIQUIDITY_NOTIONAL_CAP)`. XLM values are not an input to any of these paths.
- **[C7] Per-trade tax-friction vs annual buffer disjointness:** predicate 1's `Ĉ_RT` reads `AdmissionInputs.per_trade_tax_friction` only; it does not call `PlannedTaxPostingReader` (enforced by a test that stubs `PlannedTaxPostingReader` to raise and asserts `evaluate` succeeds on the per-trade path). A test that stubs `PlannedTaxFrictionPerTradeReader` to raise confirms the reverse is not true — the per-trade reader is the live dependency.
- **[C12] Composite live-admissibility.** `compose_live_admissibility(intrinsic, staleness, current_pinned_profile, terminal_state, as_of)` is a pure function; for a fixed input tuple it returns the same `LiveAdmissibility` on every call. Property tests: (a) `intrinsic.approval_state == FROZEN` → `admissible = False` with `LIVE_ADMISSIBILITY_FROZEN`; (b) `as_of` outside `intrinsic.kid_validity_range` → `admissible = False` with `LIVE_ADMISSIBILITY_KID_EXPIRED`; (c) any of `staleness.{kid,basiszins,classification}_status == RED` → `admissible = False` with `LIVE_ADMISSIBILITY_STALENESS_RED(<subject>)`; (d) `intrinsic.bound_profile_id ≠ current_pinned_profile` and `terminal_state ≠ FULL_SUCCESS` → `admissible = False` with `LIVE_ADMISSIBILITY_PROFILE_MISMATCH`; (e) all checks pass → `admissible = True` with empty `reason_codes`; (f) at least one `staleness.*_status == AMBER` (no `RED`) → `admissible = True` with `reason_codes ⊇ {STALENESS_AMBER(<subject>)}` as an advisory flag. Integration test: an `AdmissionInputs` materialised from a tradable-line with a stale KID (intrinsic KID validity expired at `as_of`) AND classification stale on `COMPLIANCE=RED` produces an `AdmissionVerdict(REJECT)` whose `reason_codes` contains both `TRADABLE_LINE_NOT_ADMISSIBLE(KID_EXPIRED)` and `TRADABLE_LINE_NOT_ADMISSIBLE(STALENESS_RED(classification))`.
- **[C12] Simulator/live composition identity.** `compose_live_admissibility` called from live `admission_control` and from `strategy_simulation` on the same inputs produces the same `LiveAdmissibility` record. Guards against drift if anyone adds a private simulator variant.
- **[C12] No cycle via composition.** An import-linter rule verifies that `market_policy.contract` does not import anything from `ops_reconciliation.contract`; a synthetic branch that adds such an import fails the build.

### 8.7 `tax`

- Two-ledger disjointness: no command can produce a Ledger-A entry that references a Ledger-B field or vice versa.
- Evidence-pointer requirement: Ledger-A updates without a resolved evidence pointer fail.
- Cross-year-correction auto-exception: a `BrokerCorrection` on a posting with `fiscal_year < current` produces a `TaxExceptionOpened` for the affected year.
- Residual-absorption refusal: closing a reconciliation with a residual > de-minimis without an explicit bridge or de-minimis signature fails.
- **Candidate/approval split:** `ProposeDeemedTransaction` emits `DeemedTransactionProposed` but does not update Ledger A; `RecordDeemedTransaction` (with reviewer id + evidence id) is the only path that produces `DeemedTransactionRecorded`; a `RecordDeemedTransaction` that cites a non-existent `DeemedTransactionProposed` id is refused.
- **[C7] Per-trade tax-friction reader conservative bias:** for a random sample of (tradable_line, side, notional, as_of) tuples, `PlannedTaxFrictionPerTradeReader` never returns a value below the theoretical minimum tax-friction cost for that trade under the pinned Teilfreistellung regime. A deliberately-optimistic alternative implementation is caught by this property test.
- **[C7] Annual vs per-trade reader distinctness:** `PlannedTaxPostingReader(fiscal_year)` returns a `{expected_vorabpauschale, expected_distribution_tax, worst_case_deemed}` triple with fiscal-year semantics; `PlannedTaxFrictionPerTradeReader(line, side, notional, as_of)` returns a `Money` value with per-trade semantics. The two readers are disjoint in both signature and implementation; a test asserts no shared internal state.
- **[C11] Statutory state unaffected by runtime configuration:** Ledger-A updates produced from a `FillBooked` event carry the same statutory effect whether the upstream `runtime_configuration` was `LIVE` or `PAPER`. Paper fills still exercise the full tax-state machinery — this is what makes paper trading (Phase B) a meaningful rehearsal.

### 8.8 `ops_reconciliation`

- Channel-state determinism: given pinned band + grace + input sequence, state transitions are identical under replay.
- `RED on BROKER_CASH` freeze: any admission request during `BROKER_CASH=RED` is rejected.
- `SETTLEMENT` channel integration: settlement-forecast-vs-posted divergence produces an AMBER/RED on `SETTLEMENT`, not on `BROKER_CASH`.
- Re-submit window exhaustion: exceeding `resubmit_window_cap` produces a `FEES` break.
- **[C4] Fee-component-level `FEES` break:** a synthetic sequence where commission reconciles within tolerance but exchange-fee reconciles outside tolerance produces a `FEES` break whose diagnostic pointer names the exchange-fee component specifically, not aggregate fees. Repeat per component.
- **[C9] `COMPLIANCE` channel staleness transitions:** given a pinned `basiszins_max_age_days = D`, a `ReferenceDatumVersioned(basiszins)` event at `t` followed by an `as_of = t + D + ε` scan produces `ChannelStateChanged(COMPLIANCE, …, AMBER_or_RED)` with a diagnostic pointer to the stale subject. Analogous tests for `kid_grace_days_before_expiry` and `fund_classification_max_age_days`.
- **[C9] `COMPLIANCE=RED` freezes affected tradable lines:** a `COMPLIANCE=RED` scoped to a specific tradable line produces `REJECT(RED_CHANNEL(COMPLIANCE))` for admission requests against that line; other tradable lines with fresh evidence remain admissible.
- **[C9] Staleness alarm idempotency:** repeated `ScanReferenceDataFreshness` calls in the same day produce at most one `ReferenceDataStalenessDetected` event per stale subject, per staleness transition.

### 8.9 `strategy_simulation`

- Phase-0 harness refuses to emit `StrategyIntent` or `SimulationRunCompleted`.
- Simulator admits only what live would admit: fuzz test over random `AdmissionInputs`.
- Gate-evidence rule: simulation runs with any placeholder parameter are tagged research-grade; `CertifyStrategyLcb` refuses to consume them.
- Mandatory-overlay coverage: a certification request without every `MandatoryStressOverlay` fails.
- **Canonical-run profile binding:** every `SimulationRunCompleted(kind=canonical)` carries the `ProductionProfileId` it simulated; `CertifyStrategyLcb` refuses if the run's profile id differs from current `ProductionProfileReader`.
- **[C3] Shared `LimitPriceComputer` identity.** In a canonical simulation run, the simulated limit price for every intent equals `LimitPriceComputer(side, snapshot, α, β)` with the same `α` and `β` a live `execution.SubmitOrder` would read from `GovernanceParameterReader` at the simulation `as_of`. A test stub that replaces the pure function with a deliberately different implementation fails the run.
- **[C11] Runtime-configuration isolation.** `canonical` runs are bound to a specific `runtime_configuration`; a run that mixes `LIVE`-tagged and `PAPER`-tagged exemplars as calibration inputs is refused with `MixedRuntimeConfigurationInCalibration`.
- **[M5] Session-restriction on live-admissible runs.** A canonical simulation run that admits any intent whose ticket carries `Session ∉ {CONTINUOUS}` fails the simulator-validity rule with `NON_CONTINUOUS_SESSION_REJECTED`. This test is required because idea §3 explicitly forbids blurring continuous and extended-retail sessions in simulation or live routing. Overlay runs may vary session for research purposes but are tagged `overlay:<n>` and are explicitly non-admissible as gate evidence.
- **[C14] Phase-0 structural ordering.** `RunPhase0PreScreen(protocol_id)` called against a `Phase0ProtocolId` that already has an associated `PhaseZeroPreScreenEvaluated` event fails with `PHASE0_PROTOCOL_ALREADY_EVALUATED`. Combined with `RegisterPhase0Protocol`'s refusal-on-prior-OOS-numerics invariant, this makes re-running a failed pre-screen against the same research config structurally impossible.

### 8.10 Cross-module

- **End-to-end Gate-1 rehearsal (PROVISIONAL) [C8, M2].** Replay a **synthetic** broker stream — designed to exercise the full shape of normal + correction + fallback-identity + partial-fill + reject + expiry event flows — combined with a Phase-B paper stream, through the full pipeline; assert: journal = target state, tax ledgers = target state, no unexplained residuals, no Red channels. **No live-broker data is available at this stage** — "archived" in the Gate-1 PROVISIONAL context means archived synthetic. This is the artefact signed off for Gate-1 `PROVISIONAL` verdict via `RecordGateVerdict(1, status=PROVISIONAL, …)` per §4.8.
- **End-to-end Gate-1 rehearsal (DEFINITIVE) [C8].** After N pinned weeks of Tier-0 micro-live, replay the combined synthetic + paper + live-micro stream; same assertions as the PROVISIONAL rehearsal, additionally: every Amber transition resolved within the pinned grace window, zero unexpected Red transitions, re-submit-window budget usage within cap. Signs off `RecordGateVerdict(1, status=DEFINITIVE, …)`. A test that records `DEFINITIVE` against a stream that still contains unresolved Amber past the grace window fails.
- **End-to-end Phase-0 rehearsal.** Register a protocol, run the pre-screen against frozen research data, record a Phase-0 verdict via `RecordPhase0Verdict` **[C14]**, assert verdict determinism across re-runs on a clean event store.
- **[C13] End-to-end re-pinning rehearsal.** Record a Gate-2 verdict on profile A; issue a `CertifyStrategyLcb` against a canonical run bound to profile A; invoke `RePinProductionProfile(A, B, …)`; assert that (i) `GateVerdictReader(2)` reports `INVALIDATED` for the old verdict, (ii) `StrategyCertificationReader` reports `EXPIRED` for the previously-certified strategy, (iii) `admission_control.EvaluateAdmission` now rejects any trade for that strategy with a reason code indicating expired certification, (iv) `admission_control.EvaluateAdmission`'s `compose_live_admissibility` rejects any tradable line bound to profile A (unless `V1TerminalStateReader == FULL_SUCCESS`), (v) a fresh canonical simulator run bound to profile B plus a fresh mandatory fill-probability stress run is required before a new Gate-2 verdict can be recorded, (vi) after that evidence is in place a fresh `CertifyStrategyLcb` restores live admission.
- **[C14] End-to-end Phase-0 fail-then-rescope rehearsal.** Register a Phase-0 protocol, run the pre-screen, record a `FAIL` verdict. Assert: (i) no `V1TerminalStateDeclared` event is emitted; (ii) attempting to run step-1 engineering work (e.g. full `events` freeze) is blocked until either a fresh protocol is registered against a new `ResearchConfigId` and passes, or a signed re-scope governance decision is recorded; (iii) re-running the same protocol is refused. Register a fresh protocol against a new `ResearchConfigId`, pass it, and assert step-1 work unblocks.
- **[C1] No-cycle rehearsal.** Import-linter asserts that the static import graph is a DAG. A synthetic cycle introduced on a feature branch (e.g. `admission_control` imports `strategy_simulation.contract` directly, bypassing `events.contract`) fails the build. **[C12]** Additionally, a synthetic import from `market_policy` to `ops_reconciliation.contract` fails the build.
- **[C9] Staleness-driven admission freeze rehearsal.** Age an existing KID to expire between two admission calls on the same tradable line. Assert: first call admits; `ScanReferenceDataFreshness` transitions `COMPLIANCE` to `AMBER` then `RED`; second call rejects with `RED_CHANNEL(COMPLIANCE)` and/or `TRADABLE_LINE_NOT_ADMISSIBLE(STALENESS_RED(kid))` via `compose_live_admissibility` **[C12]**; after a fresh KID is registered and confirmed valid, the channel returns to `GREEN` and admission is again permitted.
- **[C11] Paper-to-live cutover rehearsal.** Run a full pipeline in `PAPER` runtime configuration, stop, re-pin the profile's runtime configuration to `LIVE` under a signed governance decision, resume. Assert: the event stream from before the cutover is never re-played against the live broker; the `runtime_configuration` tag on every event accurately reflects the pinned configuration at `occurred_at`; a mixed-configuration replay for the same `account_id` fails.

---

## 9. Self-check before implementation

- [x] Kernel contains zero business logic and is imported by all; imports nothing.
- [x] **[C1] `events` module contains zero business logic, zero `Protocol`s, and zero functions — pure dataclasses/enums for cross-module payloads only; imported by every module that otherwise would have formed a contract cycle.**
- [x] Every module has a single `contract.py` as its only public surface; cross-module payload types live in `events.contract`.
- [x] Every module's invariants can be stated without referencing another module's internals.
- [x] The journal, the execution-event ledger, and the two tax ledgers each have exactly one owner.
- [x] `broker_perm_id` (type in `kernel`, canonical semantics in `execution`) and `FallbackIdentity` (composite shape in `kernel`, construction and usage rules in `execution`) are each defined in exactly one place per concern; downstream modules reference the types without importing `execution`'s internals.
- [x] `p_fill` is absent from every admission predicate in this plan; it is present only on the `AdmissionVerdict.p_fill_telemetry` field **[C2]** and in `strategy_simulation` overlays.
- [x] Broker-reported settled cash is the authoritative admission input; the local settlement engine is a projection only.
- [x] Gate-2 / Gate-3 evidence can only be produced by canonical simulator runs with all §6 parameters pinned; placeholder runs are research-grade and tagged as such.
- [x] Phase 0 pre-screen freezes its four items before any OOS evaluation; re-running against a new alpha is a fresh pre-screen with a new `ResearchConfigId` **[C14]**. Ordering is enforced by `RegisterPhase0Protocol` refusing when prior OOS numerics exist — not relying on process discipline.
- [x] Tax reconciliation residuals cannot close silently; the exception workflow is the only closure path.
- [x] Green/Amber/Red is channel-scoped across **six** channels **[C9]** (`BROKER_CASH`, `POSITIONS`, `FEES`, `TAX_POSTING`, `SETTLEMENT`, `COMPLIANCE`); `RED` on `BROKER_CASH` freezes all buy admission; `RED` on `COMPLIANCE` freezes admission on affected tradable lines.
- [x] The scaffold matches the idea's v1 scope (§2) and does not pre-bake v2 concerns (route-comparison science, forward-dated regimes, per-trade LCB, two-mode simulator).
- [x] Every module's contract surface is small enough that an AI agent can implement it against frozen interfaces without touching other modules.
- [x] Phase-0 pre-screen runs *before* Phase-A engineering, via the Phase-0 sub-build at step 0.
- [x] Narrow-success is a declarable terminal state, not an implicit one; declaration requires Gate-1 `DEFINITIVE` **[C8]** (not merely `PROVISIONAL`) and requires Gates 2–4 to be `UNASSERTED` or `DEFERRED` **[C14]** (not `FAIL` / `INVALIDATED`).
- [x] Both tax ledgers post to the journal under distinct account codes via explicit `BridgeAdjustmentBooked → BookBridgeAdjustment → BridgeAdjustmentPosted` flow; Ledger A statutory state is non-cash and does not post.
- [x] Re-submit controls are two separate budgets (per-decision cap, rolling-window cap) per idea §13.
- [x] Churn-budget is a first-class projection with its own reader and pinned metric (unit disambiguation pinned under governance).
- [x] `AdmissionInputs` enumerates every predicate input; missing fields fail closed with `INCOMPLETE_INPUTS`, never silently default; **fee-schedule snapshot (decomposed per component **[C4]**), market snapshot, current tier, per-ISIN liquidity caps **[C5]**, and per-trade tax friction **[C7]** are enumerated fields**.
- [x] `AdmissionVerdict` is a typed record with disposition, reason codes, predicate evaluations, fee breakdown, `p_fill` telemetry, and inputs hash **[C2]**.
- [x] `AumMinComputer` is a single shared pure function in `portfolio_accounting.contract` consumed by `market_policy` on the Gate-4 certification path and by reporting — **not invoked by `admission_control` on the per-trade path; per-trade tier check reads `TierReader`**.
- [x] **[C7] Per-trade tax-friction input is a distinct reader (`PlannedTaxFrictionPerTradeReader`) from the annual buffer-sizing reader (`PlannedTaxPostingReader`); no module conflates the two.**
- [x] Mandatory Gate-3 stress overlays are an enum; `CertifyStrategyLcb` refuses certification with missing overlays; canonical runs are profile-bound and refused if the bound profile ≠ current pinned profile; runs additionally carry a `runtime_configuration` tag **[C11]**.
- [x] Shadow profiles are representable via the governance-intent `is_live_admissible=False` flag on `ApproveTradableLine`, and cannot reach live admission because `compose_live_admissibility` rejects them when their bound profile ≠ the current pinned profile (unless `V1TerminalStateReader == FULL_SUCCESS`).
- [x] **[C12] Live-admissibility composition has no static import cycle.** `market_policy.TradableLineStatusReader` returns intrinsic state only (`approval_state, kid_validity_range, evidence_pointers, bound_profile_id`); the composed check — intrinsic state ∧ evidence freshness ∧ profile match — is a pure function `compose_live_admissibility(...)` exported from `admission_control.contract` and called identically by live admission and the simulator. An import-linter rule verifies `market_policy` never imports `ops_reconciliation.contract`.
- [x] **Route re-pinning is a first-class governance flow with invalidation of prior Gate-2 verdicts (idea §5, §12).**
- [x] **[C13] Route re-pinning also invalidates strategy certifications.** `RePinProductionProfile` atomically emits `StrategyCertificationInvalidated` for every certification bound to the old profile; `StrategyCertificationReader` reports `EXPIRED` post-re-pin; re-certification under the new profile requires a fresh canonical simulator run bound to the new profile plus a fresh mandatory fill-probability stress overlay plus a new `CertifyStrategyLcb`. No grace window.
- [x] **Scope-expansion is structurally enforced by `ApproveTradableLine` refusing a second live-admissible profile without prior `FULL_SUCCESS` declaration (idea §17).**
- [x] **Tier transitions are governance events gated on Gate-4 verdicts; admission reads, not computes (idea §6).**
- [x] **Tax candidate/approval split separates engine-authored proposals from reviewer-authorised records (idea §4 Rule 5, §9 "operational minimality").**
- [x] **[C3] Deterministic limit-price construction is a pure function `LimitPriceComputer(side, snapshot, α, β)` shared verbatim by `execution.SubmitOrder` and `strategy_simulation`; aggression and spread-budget-cap are pinned §6 governance parameters; every `LimitPriceConstructed` event records `(side, bid, ask, α, β, chosen_limit)` so the construction is reproducible from the ledger alone.**
- [x] **[C4] Fees posted at component granularity: commission, exchange, clearing, regulatory, transaction tax, each with its own `PostingAccountCode`; Gate-2 closure reconciles each component independently against its pinned sub-schedule.**
- [x] **[C8] Gate 1 is two-stage: `PROVISIONAL` unlocks Tier-0 micro-live; `DEFINITIVE` unlocks Gate-2 evaluation and terminal-state declaration.**
- [x] **[C9] Reference-data staleness is a first-class data-state concern on a dedicated `COMPLIANCE` channel, not a silent per-tradable-line flag; freshness bounds pinned under governance; daily staleness scan feeds the annual-tax-close checklist.**
- [x] **[C10] IBKR `execDetails` correction-detection rule (same `permId` + identical params + different `execId`) is an explicit invariant in `execution`, not a tacit implementation detail.**
- [x] **[C11] Paper vs live adapter share identical canonical-identity semantics and share a single codebase; every event carries a `runtime_configuration ∈ {LIVE, PAPER}` tag; replay of mixed-configuration streams for the same `account_id` is a structural error.**
- [x] **[C14] Phase-0 verdict (`Phase0Verdict = {PENDING, PASS, FAIL}`) is distinct from V1 terminal state.** A Phase-0 `FAIL` blocks Phase-A engineering but does **not** declare a V1 terminal state; re-entry is via a fresh `ResearchConfigId` or a signed re-scope governance decision under idea §16.
- [x] **[C14] Narrow-success requires Gates 2–4 to be UNASSERTED or DEFERRED, never FAILED or INVALIDATED.** `DeferGateVerdict` is the signed governance path that legitimises a previously-attempted gate's deferral under a continuation programme (idea §1 "Narrow-success state"); a `FAIL` verdict must be either remediated (fresh `PASS`) or explicitly deferred before `DeclareV1TerminalState(NARROW_SUCCESS)` is admissible.
- [x] **[M1] `platform_runtime` has no static import of `execution`; canonical-ordering-key is injected as a `Callable[[Event], tuple]` at composition time.** This removes the final `TYPE_CHECKING` workaround and makes step 2 implementable without any step-3 artefact existing.
- [x] **[M5] Canonical simulator rejects `Session ∉ {CONTINUOUS}` on live-admissible paths per idea §3.** Overlay runs may vary session for research purposes but are explicitly non-admissible as gate evidence.
- [x] `IdFactory` has an explicit seeded-replay mode so that economic-state-invariance replay produces reproducible module-generated IDs (idea §10 Rule 4).
- [x] Every module's test obligations (§8) are explicit; CI enforces coverage of invariants, not just syntactic pass.
---
