# Phase 2.9 — Stabilization, security and benchmarking

Baseline: `dd3a3cf`; starting suite: 125 passed, 4 skipped; Phase 2.9 final: **191 passed, 4 skipped**.

## 1. Architecture audit

Method: source inspection, import graph via AST, coverage run, test audit, security boundary
review. No confirmed P0 issue was found.

### P1 findings and fixes

1. **Quota circuit breaker was missing.** A quota error paused one task, but new requests could
   immediately call the same exhausted provider. Added `ProviderRegistry` quota gate: affected
   role is blocked, `/provider test` performs a safe health check and clears the gate only on
   confirmed availability.
2. **Quota/rate-limit UI exposed raw reason text.** Provider errors could reach Telegram with
   structured JSON. Task quota pauses now use a stable human message: provider quota exhausted,
   task paused, progress saved, restoration time unknown, local commands remain available.
3. **`/usage` HTML path needed explicit coverage.** The handler now has a recorded-notifier test
   asserting `parse_mode="HTML"` and no plain tag rendering. All message paths continue through
   the Presentation Layer.
4. **Dead code.** Removed unused `SupervisorAgent._read_prompt`.
5. **Scheduler/policy coverage was weak.** Added tests for bounded concurrency, cancellation,
   denied actions, single-use approvals and action binding.

### P2/P3 (documented, not refactored)

- `Orchestrator` is a large module (~3.7k lines) combining lifecycle, presentation, Git,
  approvals and recovery. Splitting it requires a separate phase.
- Telegram handlers are large and largely untested at the aiogram dispatch layer.
- `WorkerManager` subprocess lifecycle has low coverage and no real crash-injection tests.
- No disk-full, SQLite lock-hold or corrupted-result test yet.
- Dependency versions are range-pinned but there is no lockfile/artifact checksum policy.
- Bot/HTTP integration is mock-tested; real Telegram visual verification remains separate.

Import graph: no cycles detected across 57 Python modules.

## 2. Security audit

Verified:

- bwrap command construction, user namespaces, read-only system paths, worktree-only writes;
- worker network namespace + allowlisted Unix-socket egress; no direct network;
- approvals: DB-backed, owner-bound, action-bound, single-use, TTL, stage/source hash checks;
- Telegram owner ID and group/private checks;
- path traversal/symlink containment in context/workspace;
- secret redaction in logs, prompts, conversation storage, worker logs and memory UI;
- GitAuthManager host allowlist/temporary askpass/no token in argv/remote/config;
- `.env` and `config.toml` are local-only; published tree contains only templates;
- `.env` mode 600, backups mode 600, DB/log directories under `var/` mode protected.

Residual risks:

- DSH session log retains old credentials from the 2026-11-26 incident until owner-approved
  cleanup; rotation is required. See `docs/INCIDENT_2026-11-26.md`.
- Live provider logs/retention are outside local control.
- Dependency supply chain relies on the package index; no cryptographic lock is present.

## 3. Coverage

Tooling added: `pytest-cov` + branch coverage configuration in `pyproject.toml`.

Command:

```bash
coverage run --branch --source=src/ai_supervisor -m pytest -q
coverage report -m
```

Measured final combined total is **63%**; statement coverage **67.5%**, branch coverage **50.4%** (`coverage run --branch`). The target 85% was
not reached. The initial run before Phase 2.9 tests was 59%. Critical components remain uneven:

- GitAuthManager 87%, models/state validation 90%, permissions/approvals 85%;
- config 85%, recovery tests pass, usage 74%, redaction 71%;
- provider registry 68%, adapter/kimi lower, sandbox 53%;
- WorkerManager/scheduler 24%, Telegram handlers 16%, worker_runner 0%;
- sandbox subprocess and live Telegram paths remain partial.

Uncovered scenarios requiring significant work: real aiogram dispatch/all handlers, worker
subprocess lifecycle (spawn/timeout/SIGKILL/log rotation), bwrap argv failure branches, live
Telegram edits, disk-full/SQLite-lock fault injection, OpenAI live API.

This is a real limitation, not a claim of completion. The added tests target behavior that
matters (scheduler bounds/cancel, approvals, quota classification, HTML usage, redaction), not
empty lines.

## 4. Resilience

Verified by tests/design review:

- startup marks active tasks/workers interrupted and auto-resume uses persisted COMPOSITE stages;
- task transitions and approvals are serialized; approvals are single-use;
- Git publish verifies remote SHA and does not repeat after a completed stage;
- provider 401/403/429/500–503/network mappings are explicit;
- quota pauses preserve task state; local commands do not require an LLM;
- rate limits use bounded exponential backoff + jitter and honor Retry-After;
- no automatic paid-provider fallback.

Not fully proven: process kill at every instruction boundary, disk-full, SQLite lock held by an
external process, corrupted worker result files, real network partition. These remain P2 tests.

## 5. Quotas and limits

Normalized provider classes:

- `RATE_LIMIT` (retryable, Retry-After-aware);
- `QUOTA_EXHAUSTED`;
- `INSUFFICIENT_BALANCE` (maps to quota-class provider failure where documented);
- `AUTH_ERROR`;
- `PROVIDER_UNAVAILABLE`;
- `NETWORK_ERROR`.

Kimi `exceeded_current_quota_error` is detected from both message and structured error code.
Quota gates block the affected role; `/provider test` clears only after a successful health check.
No retry time is invented. Deterministic commands (`/usage`, `/report`, `/status`, `/projects`,
`/memory`) do not call an LLM.

## 6. Usage and cost

Existing ledger records provider/model/role/prompt/routing, input, output, cached, cache-write,
reasoning, requests, duration, cost and raw usage JSON. Reasoning/cache are stored separately and
not added to input/output. Pricing is optional; zero means unavailable, never invented.
Added optional display conversion (`usage.display_currency` + `usage.fx_rates`) shown explicitly
as a local estimate, never as provider fact.

Statistics: task, project, day/week/month, model, role. Synthetic tests cover per-model prices,
period totals, roles and FX estimates.

## 7. Telegram presentation

- `/usage` now sends `parse_mode="HTML"` and has a regression test.
- Shared Presentation Layer escapes dynamic content once; no double escaping in cards.
- Markdown provider answers are converted to Telegram HTML; code blocks are preserved.
- Long messages fall back to plain text rather than unbalanced HTML.
- Provider quota/rate messages are human-readable and contain no raw JSON.
- Real Telegram visual check is still required; mock/notifier tests are not visual proof.

## 8. Offline benchmarks

Added `benchmarks/run_offline.py`; no network/paid calls. It measures memory search, context
build, state-machine transitions, scheduler latency, recovery and peak Python memory on a
temporary DB.

A sample run produced (numbers are environment-specific, not model performance):

```json
{
  "memory_search_ms": 0.654,
  "context_build_ms": 0.351,
  "state_machine_cycles_ms_median": 2.313,
  "scheduler_20_jobs_ms": 0.222,
  "recovery_ms": 0.378,
  "context_peak_bytes": 25693
}
```

Future live A/B matrix: baseline prompts vs adaptive prompts; reasoning low/high/max; Kimi vs
OpenAI vs compatible provider. Do not run without budget approval and quota recovery. No model
speed/token claim is made from these offline measurements.

## 9. Long-term memory audit

Tests cover project isolation, stale exclusion, verified flag, deterministic deduplication,
redaction and experience extraction. Unverified/hypothesis entries are labeled in context;
stale records are excluded from retrieval; memory is never a permission. Memory poisoning via
files/history is treated as data and redacted/labeled. Remaining gap: a dedicated adversarial
retrieval benchmark.

## 10. Phase 3 preparation

See `docs/PHASE3_ARCHITECTURE.md`: worktree isolation, stage locks, DAG readiness, SQLite
contention, per-stage budgets, integration/approval model and acceptance criteria.

## 11. Learning documentation

Beginner-oriented guides are in `docs/learning/` (10 chapters with actual code references).
