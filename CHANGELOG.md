# Changelog

## Phase 2.9.3 — Control plane optimization
- Immutable RequestEnvelope and task intent provenance (schema v7).
- Provider-scoped quota isolation, canonical roles and per-call raw usage ContextVar.
- Review checkpoint resume without worker rerun.
- Explicit `/chat`, `/task`, `/auto` modes.
- Relevance-based memory retrieval and chat-specific routing.
- No hidden structured-output token escalation.
- Generation readiness separated from model-list health.

## Phase 2.9.1 — Infrastructure hardening
- Fixed failed SQLite connection cleanup.
- Fixed `/usage` HTML in menu/callback paths.
- Added provider quota circuit breaker and human quota/rate messages.
- Added worker lifecycle + child-process cleanup tests.
- Added sandbox env/mount adversarial tests.
- Added SQLite concurrency/rollback/corrupt-schema tests.
- Added fault-injection framework and post-push boundary.
- Added streaming/chunked redaction and nested JSON coverage.
- Added coverage configuration and exact-version requirements lock.
- Added hardening learning chapter and Phase 2.9.1 report.

## Phase 2.9 — Stabilization, security and benchmarking
- Provider quota gate, usage FX estimates, scheduler/policy tests, benchmark suite,
  architecture audit, learning path and Phase 3 proposal.

## Phase 2.8 — Provider-agnostic intelligence
- Kimi/OpenAI/OpenAI-compatible providers, role configuration, capability negotiation,
  `/models`/`/provider`, Telegram typography and Markdown conversion.

## Phase 2.7 — Adaptive planning and experience memory
- Structured memory v5, deterministic experience extraction, reviewer root cause,
  capability registry, strategies, prompt evaluation.

## Phase 2.6 — Presentation layer
- Compact cards, live progress editing, modes, prompt profiles and adaptive routing.

## Earlier
- Phase 2.5.1 COMPOSITE, GitAuthManager, approvals, sandbox/egress and recovery.
