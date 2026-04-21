## Code Style Guide

### Purpose

Write code that is correct, explicit, readable, testable, deterministic, and safe to change.

### Rule priority

When rules conflict, prefer:

1. Correctness
2. Clarity
3. Maintainability
4. Testability
5. Consistency
6. Concision

Never make code shorter at the cost of readability or predictable behavior.

### Repository baseline

Every Python repo must include:

* Ruff for linting and formatting
* Pyright in strict mode
* Pytest
* CI running lint, type checks, and tests on every change

Code is incomplete unless all three pass.

### Language scope

Use modern Python and standard-library features when they improve clarity. Prefer one clear idiom per repository.

### Types

* All new code must be fully typed.
* Public functions, methods, class attributes, constants, and module-level variables must have explicit types.
* Avoid `Any`; use it only at unavoidable third-party boundaries, keep it local, and document it.
* Do not leave code partially typed.
* Use precise types instead of generic containers where possible.

Preferred types:

* `Pydantic` for untrusted external data and for trusted internal domain objects
* `ABC` for injected behavior
* `Enum` or `Literal` for closed sets
* `TypedDict` only for compatible dict-shaped third-party interfaces
* Plain `dict` only for genuinely dynamic key-value data

Forbidden:

* `dict[str, Any]` across module, service, persistence, or API boundaries
* Ambiguous return shapes
* Boolean flag combinations that create unclear state

### Validation

* Validate untrusted data once at the boundary.
* Convert it immediately into typed internal structures.
* Rely on validated invariants internally instead of repeating checks.
* Validate early, fail fast, and emit explicit actionable errors.

Boundaries include HTTP, queues, CLI, config, env, files, and DB hydration.

### Function design

* Each function should do one clear thing.
* Prefer explicit inputs and outputs over hidden mutation.
* Separate pure logic from I/O, side effects, and orchestration.
* Keep control flow simple and local.
* Introduce helpers when they improve readability or remove real duplication.
* Do not abstract before needed, unless isolation reduces risk or hides an external dependency.
* Prefer deterministic functions over ones that depend on ambient state.

Parameters and returns:

* Use explicit parameter and return types.
* Prefer domain types over primitive bundles.
* If a function needs several related parameters, introduce a typed object.
* Return one stable shape.
* Prefer result types or exceptions over sentinel values.

### State and determinism

* Same inputs should produce predictable behavior.
* Make ordering explicit when it matters.
* Do not rely on incidental dict, DB, filesystem, or network ordering.
* Inject time, randomness, and external clients.
* Avoid implicit global state.
* Keep side effects at system edges.
* Make operations idempotent when practical.

### Errors and exceptions

* Use exceptions for exceptional cases, not normal branching.
* Raise specific exceptions.
* Catch only what you can handle meaningfully.
* Keep `try` blocks narrow.
* Do not swallow errors.
* Do not guess in ambiguous situations; fail closed.

Required behavior:

* Preserve context.
* Include enough detail to diagnose failures.
* Do not log and re-raise the same error unless the log adds boundary context.
* Do not collapse everything into a generic application error unless an external interface requires it.

### Logging

* Use the repository’s standard logger pattern.
* Log boundary events, major workflow transitions, and abnormal conditions.
* Keep normal hot paths quiet.
* Use stable structure and meaningful fields.
* Never log secrets, credentials, tokens, or sensitive personal data.

Levels:

* `ERROR`: operation failed
* `WARNING`: unusual but defined best-effort result
* `INFO`: important lifecycle or boundary event
* `DEBUG`: diagnostic detail for investigation

### External libraries and formats

* Use canonical parsers/serializers for standard formats.
* Do not hand-build structured formats when a solid library exists.
* Prefer standard or de facto standard libraries over ad hoc code.
* In tests, derive version-sensitive expected formatting from the same canonical library unless formatting itself is under test.

### Documentation

* Public modules, classes, and functions must have docstrings.
* Docstrings should cover purpose, inputs, outputs, invariants, side effects, and important failure modes when not obvious.
* Internal helpers need docstrings only when behavior is subtle.
* Use inline comments only for non-obvious reasoning, tradeoffs, or edge cases.
* Do not write comments that merely restate the code.

### Naming and style

* Avoid Hungarian notation. 
* Use concise, specific, domain-meaningful names.
* Prefer business meaning over implementation detail.
* Avoid non-standard abbreviations.
* Keep nesting shallow.
* Prefer guard clauses over deep nesting.
* Avoid decorative comments and noise.

### Testing

Tests are part of implementation.

Required:

* Unit tests for new pure logic
* Integration tests for new boundary code
* At least one acceptance-style test for critical user-visible or business-critical workflows
* Regression tests for bug fixes

Unit tests must:

* Run fast
* Avoid real network, time, randomness, and shared mutable state
* Mock only true boundaries
* Prefer fakes or injected test doubles over patching internals

Integration tests should cover real behavior for:

* DB access
* Filesystem I/O
* Serialization/deserialization
* External service adapters
* Queue/event integration
* Configuration loading

Test quality:

* Test behavior, not implementation trivia
* Prefer clear arrange-act-assert structure
* Each test should fail for one clear reason
* Keep helper logic minimal and trustworthy
* Name tests by scenario and expected outcome

### Dependency injection

Inject external dependencies instead of constructing them inside business logic, especially:

* Time
* Randomness
* HTTP/API clients
* DB/session handles
* File/storage adapters
* Env/config providers

This is required for determinism and testability.

### Configuration

* Load and validate config once at startup or entrypoint time.
* Represent config as a typed object.
* Do not read env vars throughout the codebase.
* Do not pass raw config dicts through business logic.

### Public interfaces

* Keep public interfaces explicit and stable.
* Treat APIs, events, CLIs, and persisted schemas as versioned boundaries.
* Do not make silent breaking changes.
* If a breaking change is required, make it explicit in code, tests, and release documentation.

### Forbidden shortcuts

Unless explicitly justified, do not:

* Introduce `Any`
* Return `dict[str, Any]` from business logic
* Read env vars outside config loading
* Create network clients inside pure logic
* Mix parsing, validation, I/O, and business rules in one function
* Catch broad exceptions without meaningful translation or re-raise
* Rely on global mutable state
* Write code that requires patching internals to test
* Merge code without appropriate lint, type, and test coverage

### Definition of done

A change is complete only when:

* Code is correct and readable
* Types are explicit and pass strict checking
* External data is validated at boundaries
* Ruff passes
* Appropriate tests exist and pass
* Logging and errors are meaningful
* Behavior is deterministic and maintainable

### Default expectation

Write code that a new engineer can understand, modify, and verify safely without guessing.