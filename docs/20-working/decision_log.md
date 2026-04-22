# Decision log

Decisions **already frozen by the canonical docs**. Each entry cites its source. No entry in this file is an inference; any inference goes in `assumptions_register.md`. If a decision below ever conflicts with the source doc, the source doc wins and this entry must be updated.

Conflict priority (from `CLAUDE.md` and `code_style.md`): `correctness > clarity > maintainability > testability > consistency > concision`.

---

## D-00 — Build order

- **D-00.1** Step 0 is the Phase-0 pre-screen subset. `CLAUDE.md BLD-001`, `BLD-002`, `staged_plan_11_1.md §6 step 0`.
- **D-00.2** Step 1 is blocked until `Phase0VerdictRecorded(verdict=PASS)` is emitted on a fresh `ResearchConfigId`. `CLAUDE.md BLD-002`, `INV-032`, `INV-033`, plan §6 step 0 exit.
- **D-00.3** Steps proceed in strict sequence 0→9. No step may start before its predecessor exits. `CLAUDE.md BLD-001`, plan §6.
- **D-00.4** v2 items are out of scope for v1. `CLAUDE.md FORB-001`, `FORB-002`, idea §18, plan §6.

## D-01 — Module topology

- **D-01.1** The codebase is a modular monolith with exactly 10 modules: `kernel`, `events`, `platform_runtime`, `market_policy`, `execution`, `portfolio_accounting`, `admission_control`, `tax`, `ops_reconciliation`, `strategy_simulation`. `CLAUDE.md TOP-001..002`, plan §1.
- **D-01.2** Each domain module exposes exactly one public `contract.py`. `CLAUDE.md TOP-003`, plan §1 / §5.
- **D-01.3** `events/contract.py` is type-only: dataclasses / Enums / TypedDicts / Pydantic models, zero functions, zero Protocols, zero business logic. `CLAUDE.md IMP-002`, `IMP-004`, plan §1bis `[C1]`.
- **D-01.4** Cross-module event payloads freeze at step 1 and do not change afterwards without a version bump. `CLAUDE.md BLD-003`, plan §1bis `[C1]`, §6 step 1.

## D-02 — Import graph (DAG)

- **D-02.1** `kernel` imports nothing else in this repo. `CLAUDE.md IMP-001`, plan §5.
- **D-02.2** `events` imports `kernel` only. `CLAUDE.md IMP-002`, plan §5.
- **D-02.3** Every domain module imports only `kernel`, `events`, other modules' `contract.py` (never their internals), and `platform_runtime.contract`. `CLAUDE.md IMP-003`, `IMP-006`, plan §5.
- **D-02.4** The import graph is a DAG. CI enforces no cycles. `CLAUDE.md ACC-004`, plan §8.1bis.
- **D-02.5** `strategy_simulation` does **not** import `admission_control` in the Phase-0 subset. Plan §3.8, §8.9.
- **D-02.6** No domain module may reach into another module's internal files. `CLAUDE.md IMP-006`, plan §5.

## D-03 — Determinism and dependency injection

- **D-03.1** No `datetime.now()`, `uuid.uuid4()`, `random.random()`, filesystem, network, or DB access in domain code. All such capabilities are injected via ports. `CLAUDE.md IMP-009`, `INV-015`, `INV-021`, code_style §State-and-determinism / §DI.
- **D-03.2** `IdFactory` exposes both live and seeded-replay modes. `CLAUDE.md INV-015`.
- **D-03.3** Replay must reproduce the same economic state from the same inputs under the same seed. `CLAUDE.md INV-021`.
- **D-03.4** Missing / ambiguous inputs return `Outcome.Err(DomainError)`; never silent defaults. `CLAUDE.md FAIL-001`, `FAIL-002`, code_style §Errors.
- **D-03.5** Same-currency arithmetic only; `Money` refuses cross-currency ops. Plan §2, code_style §Types.

## D-04 — Phase-0 semantics

- **D-04.1** Phase-0 is a stop-loss on project effort, not a v1 terminal gate. Idea §16, plan §3.2 `[C14]`.
- **D-04.2** `Phase0Verdict = {PENDING, PASS, FAIL}`. Plan §3.2, §8.5, `[C14]`.
- **D-04.3** `FAIL` is **not** a v1 terminal state. `V1TerminalState` enum does not carry a `FAIL` value. `CLAUDE.md INV-032`, plan §3.2 `[C14]`.
- **D-04.4** On `FAIL`, the path forward is (a) a fresh `ResearchConfigId` + new protocol that passes, or (b) a signed governance rescope per idea §16 (raise capital, narrow to buy-and-hold no-rotation, or close v1 without execution engine). `CLAUDE.md FAIL-004`, `INV-032`, idea §16, plan §6 step 0 exit.
- **D-04.5** Re-entry against the same `ResearchConfigId` that already holds a `Phase0VerdictRecorded(PASS|FAIL)` is refused. Plan §8.5 `[C14]`.
- **D-04.6** `RegisterPhase0Protocol` is refused when any prior `PhaseZeroPreScreenEvaluated` references the same `ResearchConfigId` — no relaxation of the protocol against the same data. `CLAUDE.md INV-033`, plan §3.2, §8.5, idea §16 (falsifiability).
- **D-04.7** `RunPhase0PreScreen(protocol_id)` fails with `PHASE0_PROTOCOL_ALREADY_EVALUATED` if called twice on the same protocol. Plan §8.9 `[C14]`.
- **D-04.8** The Phase-0 harness **cannot** emit `StrategyIntent` or `SimulationRunCompleted`. Plan §8.9, §3.8.
- **D-04.9** Commands `RecordEvidence`, `RegisterPhase0Protocol`, `RecordPhase0Verdict` live on `market_policy`. Commands `RegisterResearchConfig`, `RunPhase0PreScreen` live on `strategy_simulation`. Plan §3.8, §4.5.
- **D-04.10** Canonical flow is the seven-step sequence pinned in plan §4.5, ending with `Phase0VerdictRecorded`.
- **D-04.11** `RecordPhase0Verdict(FAIL)` does not emit any `V1TerminalStateDeclared`. Plan §3.2 `[C14]`.

## D-05 — Four Phase-0 items required before PASS

Per idea §16, four signed documents must exist as evidence before a protocol can register and pass:

- **D-05.1** Research protocol (feature construction, ranking rule, rebalance trigger, universe). Idea §16.
- **D-05.2** OOS evaluation window (calendar span, IS/OOS split, decision cadence inside OOS). Idea §16.
- **D-05.3** Required net effect size, a single numeric threshold under exploratory-placeholder buffers at their upper-end values. Idea §16.
- **D-05.4** Pre-registered decision rule, a single pass/fail statement, signed and timestamped. Idea §16.

## D-06 — Repository baseline

- **D-06.1** Ruff (lint + format) must pass. `CLAUDE.md ACC-001`, code_style §Repository-baseline.
- **D-06.2** Pyright in strict mode must pass on every new file. `CLAUDE.md ACC-001`, code_style §Types.
- **D-06.3** Pytest must run; all tests green. `CLAUDE.md ACC-001`, code_style §Testing.
- **D-06.4** CI runs lint + types + tests on every change. `CLAUDE.md ACC-001`, code_style §Repository-baseline.
- **D-06.5** Code is incomplete unless all of the above pass. Code_style §Repository-baseline, §Definition-of-done.

## D-07 — Types and validation

- **D-07.1** All new code is fully typed. `Any` is forbidden except at unavoidable third-party boundaries and must be localized. Code_style §Types.
- **D-07.2** `dict[str, Any]` is forbidden across module, service, persistence, or API boundaries. Code_style §Types / §Forbidden-shortcuts.
- **D-07.3** Validate untrusted data once at the boundary, convert to typed internals. Code_style §Validation.
- **D-07.4** Use `Enum` / `Literal` for closed sets; `Pydantic` for untrusted external data and trusted internal domain objects; `ABC` for injected behavior. Code_style §Types.

## D-08 — Out-of-scope confirmations (current slice)

The following items are **not** built in step 0 and are explicitly excluded from this slice. They remain on the roadmap at the step shown.

- `platform_runtime` (event store, bus, outbox, replay, projections) — step 2 `BLD-004`.
- Broker IO, canonical identity, `MarketSnapshotReader`, `LimitPriceComputer` — step 3 `BLD-005`.
- Journal, cash decomposition, reserved cash, `AumMinComputer`, fee postings — step 4 `BLD-006`.
- Full `market_policy` (tradable-line approval, settlement regimes, parameter pinning, gate verdicts, fee schedules, tiering, production-profile pinning) — step 5 `BLD-007`.
- `tax`, `ops_reconciliation`, `admission_control`, full `strategy_simulation` — steps 6–9 `BLD-008..BLD-011`.
- Full `events.contract` cross-module payloads — step 1 `BLD-003`.

## D-09 — Forbidden patterns (apply now)

- **D-09.1** No ambient global state. Code_style §State-and-determinism / §Forbidden-shortcuts.
- **D-09.2** No env-var reads outside a single config loader at startup. Code_style §Configuration.
- **D-09.3** No broad-exception catches without meaningful translation or re-raise. Code_style §Errors.
- **D-09.4** No code that requires patching internals to test. Code_style §Forbidden-shortcuts.
- **D-09.5** No mixing of parsing + validation + I/O + business rules in one function. Code_style §Forbidden-shortcuts.
- **D-09.6** No `datetime.now()` or `uuid.uuid4()` in domain code. `CLAUDE.md IMP-009`, code_style §DI.

## D-10 — v1 operating profile (not built in this slice, but frozen and carried forward)

The following are pinned by idea §5 / §4 / §7 and apply unchanged once step 3+ comes online. Recorded here so that step-0 kernel decisions (enum shapes, ID types) do not drift from them.

- EUR-denominated, IBKR cash account, SmartRouting Fixed. Idea §5.
- 2–4 pre-approved ISINs, typically one funded position. Idea §7.
- Xetra continuous trading 09:00–17:30 CET, DAY limit orders only. Idea §5.
- No modify/replace as a normal path. Idea §5.
- `p_fill` is telemetry / stress-only in v1; not an admission scalar. `CLAUDE.md INV-017` or cognate, idea §14.
- Component-granular fee posting: commission, exchange, clearing, regulatory, transaction tax. Idea §12, `[C4]`.
- Two-ledger tax model (statutory + cash), Vorabpauschale §18 InvStG, Teilfreistellung §20 InvStG, deemed sale §22 InvStG, BMF Basiszins. Idea §9.
- Canonical identity via `BrokerPermId` + `exec_correction_group_id` with `FallbackIdentity` composite. Idea §10.
- Green/Amber/Red per-channel reconciliation across 6 channels including COMPLIANCE. Idea §13 `[C9]`.
- Acceptance gates: Gate 1 (operational, two-stage PROVISIONAL→DEFINITIVE `[C8]`), Gate 2 (cost-model closure), Gate 3 (strategy-level LCB alpha), Gate 4 (capital sufficiency). Idea §17.
- Two terminal states: `NARROW_SUCCESS` (Gate 1 DEFINITIVE + Gates 2–4 UNASSERTED/DEFERRED), `FULL_SUCCESS` (all four PASS). Idea §17, `CLAUDE.md INV-032`.

## D-11 — Documentation discipline

- Public modules, classes, functions have docstrings covering purpose, inputs, outputs, invariants, side effects, and failure modes when not obvious. Code_style §Documentation.
- No comments that restate the code; comments reserved for non-obvious reasoning, tradeoffs, or edge cases. Code_style §Documentation.
