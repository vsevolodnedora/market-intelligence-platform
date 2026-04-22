# Current slice

Derived from `docs/00-charter/idea_11.md`, `docs/10-implementation/staged_plan_11_1.md`, `docs/10-implementation/code_style.md`, and the compressed spec in `.claude/CLAUDE.md`. Conflicts resolve in the order `correctness > clarity > maintainability > testability > consistency > concision`.

## 1. Slice identity

**Build step 0 of `staged_plan_11_1.md §6` — the Phase-0 pre-screen subset.**

Rationale (pinned):
- `CLAUDE.md` BLD-001 / BLD-002 and `INV-032` / `INV-033` require Phase-0 pre-screen to be built and to PASS before any step-1 engineering begins.
- `idea_11.md §16` — Phase-0 is a stop-loss on project effort, not a v1 terminal gate.
- Repo currently contains no `modules/` tree (no Python package yet), so no later step has been started. The docs identify step 0 as the first admissible work unit.

## 2. Scope (what is inside this slice)

Exactly four partial module surfaces, per plan §6 step 0:

### 2.1 `modules/kernel/` — Phase-0 subset
ID types: `ResearchConfigId`, `Phase0ProtocolId`, `EvidenceRecordId`, `GovernanceDecisionId`.  
Primitives: `Money` (Decimal + `Currency` enum, `EUR` only), `Instant` (UTC nanosecond), `BusinessDate`, `EffectiveDateRange` (half-open `[open, close)`).  
Result/error: `Outcome[T] = Ok(T) | Err(DomainError)`, `DomainError(code, message, detail)`.  
Enums: `Phase0Verdict = {PENDING, PASS, FAIL}` **[C14]**.  
Protocols that Phase-0 needs: `Clock`, `IdFactory` (live + seeded-replay modes per INV-015).

### 2.2 `modules/events/` — Phase-0 subset
Only evidence-registry / Phase-0 payload types. Zero business logic, zero `Protocol`s, zero functions (plan §1bis, IMP-002, IMP-004).  
The full cross-module admission/intent surface freezes at step 1 (BLD-003).

### 2.3 `modules/market_policy/` — Phase-0 subset
Commands: `RecordEvidence`, `RegisterPhase0Protocol`, `RecordPhase0Verdict` **[C14]**.  
Events emitted: `EvidenceRecorded`, `Phase0ProtocolRegistered`, `Phase0VerdictRecorded` **[C14]**.  
Readers: `Phase0ProtocolReader(protocol_id) -> {research_protocol_id, oos_window, required_effect_size, decision_rule_id, signed_at, verdict}`.

### 2.4 `modules/strategy_simulation/` — Phase-0 harness only
Commands: `RegisterResearchConfig`, `RunPhase0PreScreen(frozen_protocol_id)`.  
Events emitted: `PhaseZeroPreScreenFrozen`, `PhaseZeroPreScreenEvaluated`.  
Contract surface may declare the full §3.8 surface, but everything outside the Phase-0 harness returns `Err(NOT_IMPLEMENTED)` until step 9 (plan §3.8 "Two-sub-build structure").  
Does **not** import `admission_control`, **cannot** emit `StrategyIntent`, cannot produce gate evidence.

### 2.5 Canonical flow to support (plan §4.5)
```
RecordEvidence (research protocol)
RecordEvidence (OOS window)
RecordEvidence (required net effect size — pinned numeric threshold)
RecordEvidence (pre-registered decision rule)
RegisterPhase0Protocol(these four evidence ids, signed_by)     → Phase0ProtocolRegistered
RunPhase0PreScreen(protocol_id)
    → PhaseZeroPreScreenFrozen
    → PhaseZeroPreScreenEvaluated(verdict = PASS | FAIL, realised_effect, threshold)
RecordPhase0Verdict(protocol_id, verdict, evidence_id, reviewer_id)
                                                               → Phase0VerdictRecorded
```

## 3. Acceptance criteria for this slice

Each item maps to a `CLAUDE.md` id or a plan test obligation (§8). All must hold simultaneously.

### 3.1 Repo baseline (`ACC-001`, code_style "Repository baseline")
- Ruff lint + format pass
- Pyright strict passes on every new file
- Pytest runs, all tests green
- CI runs lint + types + tests on every change

### 3.2 Structural / import invariants (`ACC-004`, plan §8.1bis)
- `kernel` imports nothing from this repo (`IMP-001`)
- `events.contract` imports `kernel` only; contains zero functions, zero `Protocol`s, zero business logic (`IMP-002`, `IMP-004`)
- `market_policy` and `strategy_simulation` import only `kernel`, `events`, and each other's `contract.py` per `IMP-003` / `IMP-006`
- `import-linter` (or `tach`) ruleset in CI proves the graph is a DAG; a deliberately introduced cycle must fail the build (plan §8.1bis, §8.10 no-cycle rehearsal)
- `events.contract` members at this step are dataclasses/Enums/TypedDicts only — CI lint enforces type-only purity

### 3.3 Phase-0 structural invariants (plan §3.2, §3.8, §8.5, §8.9, `INV-033`)
- `RegisterPhase0Protocol` refuses when **any** prior `PhaseZeroPreScreenEvaluated` event exists referencing the same `ResearchConfigId` (prevents relaxed re-evaluation against the same data — idea §16 falsifiability requirement)
- `RegisterPhase0Protocol` also refuses against a `ResearchConfigId` that already has `Phase0VerdictRecorded(PASS|FAIL)` — re-entry requires a fresh `ResearchConfigId` (plan §8.5 [C14])
- `RunPhase0PreScreen(protocol_id)` fails with `PHASE0_PROTOCOL_ALREADY_EVALUATED` if called twice on the same protocol (plan §8.9 [C14])
- `RecordPhase0Verdict(FAIL)` does **not** emit any `V1TerminalStateDeclared`. The `V1TerminalState` enum does not carry a `FAIL` value (plan §3.2 [C14])
- Phase-0 harness refuses to emit `StrategyIntent` or `SimulationRunCompleted` (plan §8.9)

### 3.4 Determinism / fail-closed (`INV-015`, `INV-021`, `FAIL-001`, `FAIL-002`)
- No `datetime.now()`, `uuid.uuid4()`, filesystem, network, or DB call in domain code; all injected (`IMP-009`)
- `IdFactory` exposes both a live mode and a seeded-replay mode
- `Money` refuses cross-currency arithmetic; arithmetic within currency only
- `EffectiveDateRange` half-open semantics tested
- Missing / ambiguous inputs return `Outcome.Err(DomainError)`; never silently default

### 3.5 End-to-end rehearsal (plan §8.10 "End-to-end Phase-0 rehearsal")
- Register a protocol, run the pre-screen against frozen research data, record a Phase-0 verdict, assert verdict determinism across re-runs on a clean event store
- **Fail-then-rescope rehearsal (plan §8.10 [C14])** — record a `FAIL` and assert (i) no `V1TerminalStateDeclared` emitted, (ii) re-running the same protocol is refused, (iii) registering a fresh protocol against a new `ResearchConfigId` and passing it unblocks step 1

## 4. Exit condition (plan §6 step 0)

- `Phase0VerdictRecorded(verdict=PASS)` → step 1 (full `kernel` + full `events` freeze) unblocks (`BLD-002`)
- `Phase0VerdictRecorded(verdict=FAIL)` → step 1 remains blocked until either a fresh protocol is registered against a new `ResearchConfigId` and passes, or a signed governance re-scope decision under `idea §16` is recorded (raise capital, narrow to buy-and-hold with no rotation, or close v1 without an execution engine) — `FAIL-004`, `INV-032`
- `FAIL` is **not** a v1 terminal state (`INV-032`)

## 5. Out of scope for this slice (do not build here)

- Full `events.contract` — cross-module admission/fill/bridge payloads freeze at step 1 (plan §6 step 1)
- `platform_runtime` (event store, bus, outbox, replay, projections) — step 2 (`BLD-004`)
- Broker IO, canonical identity, `MarketSnapshotReader`, `LimitPriceComputer` — step 3 (`BLD-005`)
- Journal, cash decomposition, reserved cash, `AumMinComputer`, fee postings — step 4 (`BLD-006`)
- Full `market_policy` (tradable-line approval, settlement regimes, parameter pinning, gate verdicts, fee schedules, tiering, production-profile pinning) — step 5 (`BLD-007`)
- `tax`, `ops_reconciliation`, `admission_control`, full `strategy_simulation` — steps 6–9 (`BLD-008`..`BLD-011`)
- All v2-deferred items (`FORB-001`, `FORB-002`): route-comparison, forward-dated settlement regimes, per-trade LCB, two-mode simulator, wider universe, extended sessions, market orders, NLP alpha, etc.
- Any live order submission (plan §6 step 3: "Not yet submitting orders live")

## 6. Entry prerequisites for this slice

None within the codebase (step 0 is the first admissible build step). External to code, before the harness can meaningfully emit a `PASS`, the four Phase-0 items per `idea §16` must exist as documents ready for `RecordEvidence`:
1. Research protocol (feature construction, ranking rule, rebalance trigger, universe)
2. OOS evaluation window (calendar span, IS/OOS split, decision cadence inside OOS)
3. Required net effect size (single numeric threshold under exploratory-placeholder buffers at their upper-end values)
4. Pre-registered decision rule (single pass/fail statement, signed and timestamped)

These are external inputs. The harness does not create them; it pins and evaluates against them.
