# Open questions

Contradictions, ambiguities, and load-bearing unknowns surfaced while reading the six canonical docs (`.claude/CLAUDE.md`, `docs/00-charter/idea_11.md`, `docs/00-charter/idea_sidecar.yaml`, `docs/10-implementation/staged_plan_11_1.md`, `docs/10-implementation/plan_sidecar.yaml`, `docs/10-implementation/code_style.md`) against the current slice (step 0 — Phase-0 pre-screen subset).

Each entry: **Q-nn**, **What's unclear**, **Where the tension shows up**, **Proposed resolution direction**. Items here are distinct from `assumptions_register.md` — assumptions are things the docs simply don't say; open questions are things the docs *say enough about to create a tension* but not enough to resolve it.

---

## Q-01 — Step-0 state-retrieval tension vs. step-2 `platform_runtime`

- **What's unclear:** Plan §3.2, §8.5 `[C14]`, and `CLAUDE.md INV-033` require `RegisterPhase0Protocol` to refuse when prior `PhaseZeroPreScreenEvaluated` exists for the same `ResearchConfigId`, and require `RunPhase0PreScreen` to refuse a second call against the same `protocol_id`. Both invariants *imply storage + read-your-writes semantics*. But `platform_runtime` (event store, bus, projections) is not built until step 2 (`CLAUDE.md BLD-004`, plan §6).
- **Tension:** Step 0 needs "has an event of kind K for stream S ever been emitted?" without owning the store.
- **Candidate resolutions:**
  - (a) Introduce an injected `EventStoreReader` port on `market_policy` and `strategy_simulation` command handlers at step 0; step 0 provides only an in-memory test implementation; step 2 provides the real one. No contract change between step 0 and step 2.
  - (b) Build a minimal append-only in-memory event log inside `platform_runtime` at step 0 as a degenerate subset, parallel to how `events.contract` ships a Phase-0 subset. Risks breaking the declared build order.
  - (c) Deferred: run step 0 fully in-process with invariants only enforced in tests, not in the domain. Weak — leaves a gap.
- **Recommended direction:** (a). Keeps the declared build order intact; the port is the natural v1 interface.

## Q-02 — `ResearchConfigId` vs. `Phase0ProtocolId` semantic overlap

- **What's unclear:** Plan §3.8 introduces both IDs as separate kernel types. Plan §8.5 `[C14]` implies at most one live-or-resolved protocol per research config. But the charter / plan do not state what the research config *is* as a distinct artifact — i.e., whether it is a superset pointing to several potential protocols, or whether it is a 1:1 wrapper around "one candidate protocol and its data".
- **Tension:** If 1:1, the two IDs collapse conceptually. If 1:N, the rules in plan §8.5 need to explain which N-of-N configurations trigger the "new `ResearchConfigId` required" branch.
- **Candidate resolutions:**
  - (a) 1:1 semantic: a `ResearchConfig` is the research-side artifact (strategy feature definition, data intended for evaluation); a `Phase0Protocol` is the signed governance artifact (the four evidence records + the signing metadata). One config, one protocol, one verdict.
  - (b) 1:N semantic: one research config may accumulate multiple protocol attempts, each with its own evidence bundle; invariant is at most one non-FAIL verdict per config, and re-entry requires a fresh config.
  - (c) Something else.
- **Recommended direction:** Needs a pinned statement. (a) is the simpler reading of plan §4.5 + §8.5 `[C14]` and matches the falsifiability frame of idea §16.

## Q-03 — "Signed" — cryptographic vs. authored

- **What's unclear:** Idea §16, plan §4.5, and plan §3.2 use "signed" in multiple places (evidence, protocol, governance rescope). No doc pins cryptographic vs. attested-authorial semantics.
- **Tension:** If cryptographic, step 0 needs a key-management port, a canonicalization algorithm for evidence bytes, and a verifier. If authored-only, step 0 just needs `signed_by: ReviewerId` + `signed_at: Instant`.
- **Recommended direction:** Step 0 adopts authored-only. Add a governance decision if cryptographic signatures are required later, bumped under the events-contract version policy (`CLAUDE.md BLD-003`).

## Q-04 — `GovernanceDecisionId` — where is it emitted?

- **What's unclear:** Plan §3.8 lists `GovernanceDecisionId` as a kernel ID, and idea §16 / plan §6 step 0 exit say that the FAIL branch unblocks step 1 via either (a) a fresh protocol that passes, or (b) "a signed governance re-scope decision". But no event name, no command name, and no module owner is pinned for the rescope path.
- **Tension:** The rescope branch is part of the step-0 exit condition (`FAIL-004`, `INV-032`) but has no built-in representation.
- **Candidate resolutions:**
  - (a) Add `RecordGovernanceDecision` command on `market_policy` at step 0 emitting `GovernanceDecisionRecorded`. Expand step-0 scope slightly.
  - (b) Treat the rescope branch as documentation-only at step 0 (human-signed note outside the codebase), and materialize it in `market_policy` later (step 5 production-profile pinning).
- **Recommended direction:** (b), given that step 0 should stay minimal and the rescope branch does not occur on the golden path.

## Q-05 — Phase-0 evaluator: inputs, purity, reproducibility

- **What's unclear:** Plan §3.8 / §4.5 say `RunPhase0PreScreen` produces a `realised_effect` and compares against the signed threshold, but does not pin (a) what data it reads, (b) whether the evaluator is a kernel-injected pure function or a separate service, (c) how determinism is proven across re-runs when the underlying dataset is external.
- **Tension:** Plan §8.10 asserts determinism across re-runs on a clean event store. Determinism of a data-dependent evaluator demands that the dataset used is itself content-addressed and pinned at the moment of freeze.
- **Candidate resolutions:**
  - (a) `RunPhase0PreScreen` first emits `PhaseZeroPreScreenFrozen` with a content-hash of the dataset and the `ResearchConfig`. The evaluator then runs against that frozen artifact. Determinism follows from content-addressing.
  - (b) The evaluator is the `RecordEvidence` path — the caller submits a pre-computed `realised_effect` as an evidence record. The harness just compares numbers. Minimal in-code logic.
  - (c) Something else.
- **Recommended direction:** (a). It keeps evaluator logic in-process, preserves falsifiability, and matches the freeze-before-compute ordering already stated in plan §8.9 / §3.8.

## Q-06 — `events.contract` partial-freeze strategy across step 0 → step 1

- **What's unclear:** Plan §1bis `[C1]` freezes the cross-module events surface at step 1. Step 0 ships only the Phase-0 subset. The docs do not state how the two are physically laid out: one file that grows, two files that merge, or something else.
- **Tension:** If `events/contract.py` grows between step 0 and step 1, step-0 invariants must be robust to the later members being added without re-sorting or renumbering. If two files are merged, imports in `market_policy` / `strategy_simulation` must work identically under both layouts.
- **Recommended direction:** Single `events/contract.py` with clearly sectioned headers — Phase-0 subset (step 0) and cross-module payloads (step 1). Step 1 only appends; it never reorders or renames step-0 members. Matches `CLAUDE.md BLD-003` version stability.

## Q-07 — "Upper-end values" of exploratory-placeholder buffers

- **What's unclear:** Idea §16 says the required net effect size sits "under exploratory-placeholder buffers at their upper-end values". No numbers appear in any doc.
- **Tension:** Plan §8.10 `[C14]` requires a concrete PASS and FAIL test rehearsal. Without numbers, there is no way to construct a genuine test fixture — only a symbolic one.
- **Recommended direction:** Tests use symbolic Decimal fixtures that are declared in a test-only constants module. Production code must not embed a hard-coded threshold; the threshold flows in via the evidence record. This avoids baking placeholder numerics into shipped code.

## Q-08 — `ReviewerId` / reviewer identity governance

- **What's unclear:** Plan §4.5 requires a `reviewer_id` field on `RecordPhase0Verdict`. No doc pins (a) how reviewers are enrolled, (b) whether reviewers can be removed or rotated, (c) whether the kernel should reject unknown reviewer IDs or accept any opaque string.
- **Tension:** If reviewers are free-form strings, audit trails are weak. If enrolled, step 0 needs an enrolment command and a reviewer registry — extra scope.
- **Recommended direction:** Step 0: `ReviewerId` is opaque `NewType('ReviewerId', str)`; validation is deferred to a downstream slice. Record the decision here so audit trails surface the reviewer as data, not as a first-class domain object, in v1.

## Q-09 — Research-dataset delivery into the harness

- **What's unclear:** Plan §3.8 has `RegisterResearchConfig` and `RunPhase0PreScreen`, but does not pin how the research dataset (prices, features, IS/OOS split) enters the harness. Is it another evidence record? A filesystem path? An injected reader?
- **Tension:** `CLAUDE.md IMP-009` forbids filesystem access in domain code. If the dataset is file-based, it must be read via an injected port.
- **Recommended direction:** Inject a `ResearchDatasetReader` port on `strategy_simulation`. The port's only method returns the dataset as a typed domain object. The step-0 concrete implementation can read a pinned content-hashed file off the filesystem; the port keeps domain code pure.

## Q-10 — `Instant` nanosecond precision vs. `datetime` boundary

- **What's unclear:** Plan §2 says `Instant (UTC nanosecond)`, which is finer than Python's `datetime` microsecond floor. Interaction at boundaries (logging, CI artifacts, display) is not pinned.
- **Tension:** Any adapter that converts `Instant` ↔ `datetime` loses precision. Replay determinism depends on round-trip identity.
- **Recommended direction:** `Instant` internally stores `int` nanoseconds. Conversion helpers live in an adapter layer; domain code never touches `datetime`. Serialization formats use nanosecond ints (or RFC 3339 with nanosecond precision) at the events boundary.

## Q-11 — `EffectiveDateRange` half-open semantics vs. OOS window encoding

- **What's unclear:** Plan §2 pins half-open `[open, close)` range semantics. The OOS window evidence record carries "calendar span, IS/OOS split, decision cadence". Whether the split boundary is inclusive on the IS side or the OOS side is not stated.
- **Tension:** A test that depends on the boundary day's treatment can silently produce a different verdict depending on the convention.
- **Recommended direction:** Follow the half-open convention pinned in plan §2: IS range is `[is_start, is_end)`, OOS range is `[is_end, oos_end)`. Pin this in a kernel docstring and reference it from the evidence-record decoder.

## Q-12 — v1 terminal-state enum location and shape

- **What's unclear:** `CLAUDE.md INV-032` and plan §3.2 `[C14]` state that `V1TerminalState` does **not** carry `FAIL`. The enum's full membership is not written in any doc, and its module owner is not pinned (kernel? events? market_policy?).
- **Tension:** `RecordPhase0Verdict(FAIL)` is required to "not emit any `V1TerminalStateDeclared`". To prove this as a test, the test must be able to observe the event type — which means the type must exist at step 0 even though it belongs to a later flow.
- **Recommended direction:** Define `V1TerminalState = {NARROW_SUCCESS, FULL_SUCCESS}` as a kernel Enum at step 0. Emit-site (`V1TerminalStateDeclared`) need not exist until step 5, but the enum is reachable from the very first invariant test.

## Q-13 — `Outcome` in signatures vs. exceptions in code style

- **What's unclear:** Plan §2 mandates `Outcome[T] = Ok(T) | Err(DomainError)` for command return types. Code style §Errors says "use exceptions for exceptional cases". The two can coexist but the division of responsibility is not pinned.
- **Tension:** If developers default to raising inside command handlers, the `Outcome` surface becomes decorative.
- **Recommended direction:** Commands return `Outcome[T]` for all expected business-rule failures (validation, invariant refusal). Exceptions are reserved for programmer errors, adapter breakages, and genuine bugs. Document this split in a kernel docstring.

## Q-14 — Falsifiability scope on step-0 tests

- **What's unclear:** `CLAUDE.md INV-033` and idea §16 frame Phase-0 around falsifiability — once evaluated, a protocol's parameters cannot be re-tuned against the same data. Plan §8.5 / §8.9 enforce this structurally. But the tests as specified prove refusal on *identical re-registration* — they do not prove refusal on *semantically equivalent but textually different* re-registration (e.g., same rule, different `decision_rule_id`).
- **Tension:** A determined operator could bypass INV-033 by resubmitting the same logic under a new evidence-record id.
- **Recommended direction:** Step 0 enforces the structural rule (same `ResearchConfigId` refused). Semantic falsifiability is deferred to a signed governance policy (reviewer attests that the new protocol is genuinely different). Record this gap explicitly.

## Q-15 — Event envelope fields — causation and correlation

- **What's unclear:** No doc pins the event envelope beyond "events are typed payloads" (plan §1bis). Without a pinned envelope, causation/correlation chains for the Phase-0 flow are ambiguous.
- **Tension:** Plan §8.10 rehearsal wants to prove determinism across re-runs; this is easier if events are correlated to their originating command via a correlation ID.
- **Recommended direction:** Define a standard `Event[T]` envelope in `events.contract` at step 0 carrying `event_id`, `stream_id`, `version`, `occurred_at`, `causation_id`, `correlation_id`. Step 1 inherits the envelope unchanged.

## Q-16 — Where does `Phase0ProtocolReader` live?

- **What's unclear:** Plan §3.8 lists `Phase0ProtocolReader(protocol_id) -> {...}` on `market_policy`. The return type is documented as a dict-like shape. Code style §Types forbids `dict[str, Any]` across module boundaries.
- **Tension:** The plan's informal shorthand `{research_protocol_id, oos_window, required_effect_size, decision_rule_id, signed_at, verdict}` is not a typed return in the §Types sense.
- **Recommended direction:** Return a frozen Pydantic / dataclass `Phase0ProtocolView` from the reader, with the fields named in plan §3.8. Pin this as the public contract surface on `market_policy.contract`.

---

## Disposition

Each Q-nn must be resolved in one of three ways:

1. **Pinned in a doc** — escalate to whichever of `CLAUDE.md` / `idea_11.md` / `staged_plan_11_1.md` owns the topic; update that doc; then move the corresponding entry into `decision_log.md` citing the new source.
2. **Captured as an assumption** — if the tension can be worked around with a local default, move the entry into `assumptions_register.md` and leave a `TODO(Q-nn)` in code.
3. **Deferred** — if the tension does not affect step 0 and a future step can resolve it naturally, note it here as deferred-to-step-N and keep the entry dormant.

Until each Q-nn is disposed of, step-0 code must either refuse the code path that depends on it (fail-closed) or mark the default with a visible `TODO(Q-nn)` at the single call site.
