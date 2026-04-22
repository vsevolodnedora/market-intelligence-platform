# Assumptions register

Unresolved items that are **not pinned by the canonical docs** (`.claude/CLAUDE.md`, `docs/00-charter/idea_11.md`, `docs/00-charter/idea_sidecar.yaml`, `docs/10-implementation/staged_plan_11_1.md`, `docs/10-implementation/plan_sidecar.yaml`, `docs/10-implementation/code_style.md`) but that the current slice (step 0 — Phase-0 pre-screen subset) needs resolved before code can ship.

Each entry: **A-nn** — statement of assumption, **Source gap**, **Blocks**, **Proposed default** (to be validated, not used as fact).

---

## Repository / tooling

### A-01 — Python package root is `./modules/` at repo root
- **Source gap:** Plan §1 refers to `modules/` but no doc pins the repo-relative path, package metadata (`pyproject.toml`), or Python version (>=3.12 inferred from `match` + PEP 695 generics common in `Outcome[T]`).
- **Blocks:** ACC-001 (Ruff / Pyright / Pytest / CI must be wired to *something*).
- **Proposed default:** Python 3.12+, single `pyproject.toml` at repo root, `modules/` is an importable namespace package. Needs confirmation.

### A-02 — Test layout is `tests/` mirroring `modules/`
- **Source gap:** Code style §Testing mandates pytest but no doc pins directory layout, fixture scoping, or whether tests live next to code or in a sibling tree.
- **Blocks:** ACC-001, every test obligation in plan §8.
- **Proposed default:** `tests/<module_name>/test_*.py`, per-module `conftest.py`, no auto-use fixtures.

### A-03 — CI platform is GitHub Actions
- **Source gap:** `ACC-001` / code_style "CI runs lint, type checks, tests on every change" does not name a CI provider.
- **Blocks:** CI setup for the repo baseline gate.
- **Proposed default:** GitHub Actions, since the repo is a GitHub remote. Needs confirmation.

### A-04 — Import-graph enforcement uses `import-linter`
- **Source gap:** Plan §8.1bis says "`import-linter` (or `tach`)". Not pinned.
- **Blocks:** ACC-004 structural invariants, plan §8.1bis cycle-introduction rehearsal.
- **Proposed default:** `import-linter` with a `contracts` section listing each module's allowed imports.

### A-05 — UUIDv7 implementation choice
- **Source gap:** `INV-015` requires deterministic IDs under seeded replay but no library is pinned; stdlib `uuid` has no v7 as of 3.12.
- **Blocks:** `kernel.IdFactory` live mode implementation.
- **Proposed default:** `uuid6` PyPI package (exposes `uuid7`), or roll `IdFactory` directly against a seeded `random.Random` for replay plus `uuid.uuid4()` fallback in live mode.

### A-06 — Pydantic v2 is the model framework
- **Source gap:** Code style §Types lists `Pydantic` but no version constraint.
- **Blocks:** All kernel value types if Pydantic-backed.
- **Proposed default:** Pydantic 2.x. `BaseModel` with `model_config = ConfigDict(frozen=True, strict=True)`.

---

## Kernel primitives

### A-07 — `Money` internal representation and Decimal context
- **Source gap:** Plan §2 / code style say "Decimal + Currency enum". Precision, rounding mode, and context-propagation strategy not pinned.
- **Blocks:** ACC-001 type checks, future fee/tax arithmetic, deterministic replay.
- **Proposed default:** `decimal.Decimal` at module-local context (`prec=28`, `ROUND_HALF_EVEN`), frozen dataclass `Money(amount: Decimal, currency: Currency)`, no implicit coercion from `float`.

### A-08 — `Currency` enum member set
- **Source gap:** Idea §5 pins EUR-only for v1 live; plan does not state whether the enum should be `{EUR}` singleton or `{EUR, USD, GBP, ...}` with v1 guarded.
- **Blocks:** Kernel scope.
- **Proposed default:** `Currency = Enum("Currency", ["EUR"])` at step 0; extend when v2 needs it. Cross-currency arithmetic raises regardless of membership.

### A-09 — `Instant` nanosecond representation
- **Source gap:** Plan §2 says "Instant (UTC nanosecond)" but does not pin internal type.
- **Blocks:** Kernel scope, determinism tests.
- **Proposed default:** `Instant(ns_since_epoch: int)` as a frozen dataclass wrapping an `int` count of nanoseconds UTC. No `datetime` leakage into domain code.

### A-10 — `BusinessDate` calendar
- **Source gap:** Plan §2 mentions `BusinessDate` but does not specify calendar (TARGET2, Xetra trading days, etc.) or whether the calendar is part of the type or injected.
- **Blocks:** Kernel scope; also blocks downstream settlement/OOS-window arithmetic.
- **Proposed default:** `BusinessDate(year: int, month: int, day: int)` opaque at step 0 — no calendar logic yet. Xetra calendar introduced at step 3 when it is needed.

### A-11 — `Outcome[T]` implementation source
- **Source gap:** Plan §2 and code style §Errors imply a `Result`-shaped type but do not say "roll your own" vs. library.
- **Blocks:** All module signatures.
- **Proposed default:** Roll own: `Ok[T]` / `Err[DomainError]` as a `typing.Generic` pair with a `match`-friendly API. Avoid `returns` / `result` libraries to keep the surface local.

### A-12 — `DomainError` shape
- **Source gap:** Plan §2 lists `(code, message, detail)` without fixing `code` type (Enum vs. string), `detail` shape (`Mapping[str, Any]` is forbidden by code style §Forbidden), or whether `DomainError` subclasses.
- **Blocks:** Every fail-closed path.
- **Proposed default:** `DomainError` frozen model: `code: ErrorCode` (`StrEnum`), `message: str`, `detail: Mapping[str, str | int | Decimal | bool] | None`. Subclass only when a downstream module needs additional typed fields.

---

## Phase-0 semantics

### A-13 — "Signed" semantics on evidence and protocols
- **Source gap:** Idea §16 and plan §4.5 say "signed_by", "signed_at", "signed governance re-scope decision". Whether `signed` means cryptographic (PGP/Ed25519), authenticated metadata (attested authorial identity), or just an author+timestamp tuple is not stated.
- **Blocks:** Evidence-record shape, `RegisterPhase0Protocol` input validation, governance-rescope recording.
- **Proposed default:** Step 0 treats "signed" as authored metadata only: `signed_by: ReviewerId`, `signed_at: Instant`. Cryptographic signatures deferred to a later slice and marked FORB-pending if needed.

### A-14 — `ReviewerId` identity model
- **Source gap:** Plan §4.5 and §3.2 both call `reviewer_id` / `signed_by`. No type is specified; no external identity provider is pinned.
- **Blocks:** Kernel ID set at step 0 — decide whether `ReviewerId` is a kernel primitive now or deferred.
- **Proposed default:** Add `ReviewerId` as an opaque `NewType('ReviewerId', str)` in `kernel` at step 0 (it appears in `RecordPhase0Verdict`). String semantics: free-form human identifier pinned into an allow-list at config time. No directory integration in v1.

### A-15 — Research protocol document schema
- **Source gap:** Idea §16 lists the four Phase-0 items (research protocol, OOS window, effect size, decision rule) but does not pin the internal schema of each evidence record.
- **Blocks:** `RecordEvidence` payload type, the full shape of `EvidenceRecorded`.
- **Proposed default:** Each evidence record is `EvidenceRecord(evidence_id, kind: EvidenceKind, payload: bytes, content_hash: str, signed_by, signed_at)` where `payload` is the canonical-serialized document (YAML/JSON), content-addressed. Harness stores the bytes; interpretation is out of scope for step 0 except as payload-kind discrimination.

### A-16 — OOS window encoding
- **Source gap:** Idea §16 requires an OOS evaluation window with "calendar span, IS/OOS split, decision cadence". Whether this is one evidence record or three is not pinned.
- **Blocks:** Evidence-registry shape; plan §4.5 flow.
- **Proposed default:** One evidence record of `kind=OOS_WINDOW` containing all three fields in its payload document. `EffectiveDateRange` is the in-domain representation once decoded.

### A-17 — Required net effect size: numeric placeholder values
- **Source gap:** Idea §16 / §2 references "exploratory-placeholder buffers at their upper-end values"; upper-end numeric values are not written in any doc.
- **Blocks:** Fail-then-rescope rehearsal (plan §8.10 [C14]) needs a PASS and a FAIL test fixture.
- **Proposed default:** Use symbolic test fixtures (`effect_required = Decimal("0.05")`, `effect_realised_pass = Decimal("0.07")`, `effect_realised_fail = Decimal("0.02")`) and mark this as fixture-only until numbers are pinned in a dedicated governance entry.

### A-18 — Decision rule encoding
- **Source gap:** Idea §16 "single pass/fail statement". Plan §4.5 references `decision_rule_id`. Whether the rule is evaluated in code or is a human-signed artifact read by the harness is ambiguous.
- **Blocks:** `RunPhase0PreScreen` evaluator implementation.
- **Proposed default:** Step 0 treats the decision rule as the inequality `realised_effect >= required_effect`. Any richer rule is deferred; the evidence record still pins the prose statement for audit.

### A-19 — Phase-0 harness evaluator mechanism
- **Source gap:** Plan §3.8 and §4.5 describe that `RunPhase0PreScreen` produces `realised_effect` but do not pin what it reads (research data files? event-store evidence payloads? a fixture registered under `ResearchConfigId`?).
- **Blocks:** `RunPhase0PreScreen` implementation.
- **Proposed default:** The harness consumes a `Phase0Evaluator` protocol injected at construction. The evaluator is a pure function of `(ResearchConfig, Phase0Protocol, ResearchDataset)` producing `(realised_effect, frozen_dataset_hash)`. `ResearchDataset` is loaded via an injected `ResearchDatasetReader` port. This keeps kernel/events pure and deferrs dataset-shape questions.

### A-20 — Persistence backend at step 0 (no `platform_runtime` yet)
- **Source gap:** Plan §6 step 2 builds `platform_runtime` (event store, bus, outbox). Step 0 invariants (plan §3.2 / §8.5) require checks like "no prior `PhaseZeroPreScreenEvaluated` for this `ResearchConfigId`" which imply *some* form of state retrieval.
- **Blocks:** Structural invariants of plan §3.3 / §8.5; the canonical-flow rehearsal (§8.10).
- **Proposed default:** Step 0 introduces a minimal in-memory event log in a `tests/` helper and an injected `EventStoreReader` port on the `market_policy` and `strategy_simulation` command handlers. No persistence, no bus, no projections. `platform_runtime` replaces this at step 2 without a contract change.

### A-21 — `ResearchConfigId` vs `Phase0ProtocolId` — relationship
- **Source gap:** Plan §3.8 introduces both but the N:M relationship between them (is each protocol bound to exactly one research config? Can one research config accumulate multiple protocols over time? Plan §8.5 [C14] implies 1:1 enforcement — "re-entry requires a fresh `ResearchConfigId`" — but the binding mechanism is not named.).
- **Blocks:** `RegisterPhase0Protocol` input schema, invariant test at plan §8.5.
- **Proposed default:** `Phase0Protocol` carries one `research_config_id`. Invariant: at most one `Phase0VerdictRecorded(PASS|FAIL)` per `ResearchConfigId`; at most one live (unrecorded-verdict) `Phase0Protocol` per `ResearchConfigId`.

### A-22 — `GovernanceDecisionId` scope at step 0
- **Source gap:** Plan §3.8 lists `GovernanceDecisionId` among Phase-0 kernel IDs. The only step-0 path requiring it is the rescope branch in plan §6 / idea §16. No event type is pinned for recording governance decisions at step 0.
- **Blocks:** Exit condition (rescope-on-FAIL branch).
- **Proposed default:** Add the ID type but defer any `market_policy` command that emits it to step 5 (production-profile pinning). At step 0, rescope is a documentation-only path — `Phase0VerdictRecorded(FAIL)` plus a human-authored note outside the codebase.

---

## Events module (step 0 subset)

### A-23 — Partial `events.contract` shape vs. step-1 freeze
- **Source gap:** Plan §1bis / `[C1]` requires `events.contract` to be the frozen-at-step-1 type surface. Step 0 ships a subset. Merge strategy — split the file from the start? Single file, with step 1 adding members? — is not pinned.
- **Blocks:** How to author step-0 events without rewriting them at step 1.
- **Proposed default:** Single `events/contract.py` with clearly marked section headers `# === Phase-0 subset (step 0) ===` and `# === Cross-module freeze (step 1, pending) ===`. Step 1 fills the second section; the first is append-stable.

### A-24 — Event envelope shape
- **Source gap:** Plan §1bis names events as typed payloads; the envelope (event id, stream id, version, timestamp, causation/correlation) is not pinned.
- **Blocks:** `EventRecorded` shape, replay determinism tests.
- **Proposed default:** `Event[T]` generic envelope: `event_id: EventId`, `stream_id: StreamId`, `version: int`, `occurred_at: Instant`, `causation_id: EventId | None`, `correlation_id: EventId`, `payload: T`. All IDs from `IdFactory`.

---

## Ops / determinism

### A-25 — Seed injection for replay
- **Source gap:** `INV-015` requires seeded-replay mode for `IdFactory` but does not say where the seed comes from (config? env? test fixture?).
- **Blocks:** Determinism tests (plan §8.10 rehearsal claims verdict determinism across re-runs).
- **Proposed default:** Seed is an explicit constructor argument: `IdFactory.seeded(seed: bytes)` vs. `IdFactory.live()`. Tests inject `b"phase0-rehearsal-001"`; production code uses `.live()`.

### A-26 — Clock interface shape
- **Source gap:** Code style §DI requires time injection; plan §2 names `Clock` as a Protocol. Method set (`now() -> Instant` only? also `today(tz) -> BusinessDate`?) is not pinned.
- **Blocks:** All command handlers that stamp events.
- **Proposed default:** `Clock` ABC with `now(self) -> Instant` only at step 0. Any calendar-aware helpers live in `market_policy` or adapters.

---

## How to close each assumption

- Every A-nn above must resolve to either (a) a pinned entry in `CLAUDE.md` / `idea_11.md` / `staged_plan_11_1.md`, or (b) a signed governance decision recorded under `idea §16`.
- Until resolved, code shipped under this slice carries each unresolved A-nn as a `TODO(A-nn)` comment at the single site where the default is used, so the default can be swept once pinned.
