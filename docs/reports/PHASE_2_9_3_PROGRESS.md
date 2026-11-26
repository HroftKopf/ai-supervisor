# Phase 2.9.3 progress

Baseline for this continuation: `800053c`.
Current state after this work: see final commit/report.

## Completed in this session

- Provider call identity is constructed before the request and carried on
  `ProviderResponse.identity`; usage carries request/provider/model/role/attempt/stage fields.
- Canonical roles and aliases; quota is provider-scoped instead of all-role.
- Provider quota state is persisted in `provider_states` and hydrated at startup. A persisted
  `QUOTA_EXHAUSTED` blocks only that provider.
- Request provenance: immutable `source_request`, request ID/revision/provenance; planner uses the
  original request.
- Review checkpoint uses persisted evidence and skips worker on resume.
- EvidenceManifest binds HEAD, index, tracked changes, untracked metadata, evidence/spec hashes,
  request revision. Modified worktree invalidates checkpoint.
- Reviewer context is built from the task worktree.
- Explicit `/chat`, `/task`, `/auto`; `/usage` menu/callback HTML fixed.
- Memory retrieval filters scope before limit and uses query relevance.
- Health/generation readiness separated; model listing does not clear quota.
- Owner-approved `/provider recover` with minimal generation probe, single-flight lock,
  provider/model/generation-bound approval, usage identity registration and atomic persisted
  AVAILABLE transition. Persistence is written before the in-memory block is cleared.
- No hidden structured-output token escalation.
- Tests increased from 160 to 163; coverage remains ~68% statements / 51% branches.

## Not completed — do not claim Phase 2.9.3 accepted

- Budget Engine 2.0: no reservation table/lifecycle, no preflight reservation before dispatch, no
  atomic concurrent reservation, no unknown-usage reconciliation.
- Provider recovery is implemented offline with a mock provider: `/provider recover`, single-flight,
  approval binding and persisted clear. Live paid generation probe was not executed.
- Context Engine 2.0: typed fragments, per-role fragment budgets, mandatory retention,
  deduplication pipeline and diagnostics are not implemented. Only memory retrieval and reviewer
  worktree freshness are partial.
- Orchestrator decomposition: not started; orchestrator remains monolithic.
- Owner amendments/revisions and approval invalidation are not implemented.
- Dedup is still middleware-based; no SQLite unique dedup key on task creation.
- Request ID is not persisted on every stage/usage event; usage has identity columns but stage
  foreign-key linkage is not complete.
- Full fault-injection matrix, multi-process SQLite, disk-full, and live provider/Telegram tests are
  not complete.
- Learning chapters 12–18 and architecture docs CONTROL_PLANE/PROVIDER_LIFECYCLE/TASK_RECOVERY/
  CONTEXT_ENGINE/BUDGET_ENGINE are not complete.

## Provider recovery test matrix

| Test | Result |
|---|---|
| models.list does not clear quota | PASS |
| successful probe clears persisted block | PASS |
| restart after successful recovery keeps AVAILABLE | PASS |
| failed probe keeps block | PASS |
| network error does not confirm recovery | PASS (direct probe failure/block retained) |
| probe without approval | PASS (no provider call before approval) |
| repeated approval cannot generate twice | PASS |
| concurrent probes are single-flight | PASS |
| usage belongs to fake provider | PASS |
| changed provider config invalidates approval | PASS |
| two approvals for one block → one probe | PASS |
| stale approval from prior generation → no probe | PASS |
| concurrent approvals → one probe | PASS |
| SQLite persistence failure retains block | PASS |
| unknown quota identity does not block planner | PASS |
| independent provider quota isolation | PASS |

Live generation probe: NOT RUN (no paid API authorization).

## Exact next step

Implement Milestone C in a new change set:

1. Add `budget_reservations` schema and atomic SQLite reservation service.
2. Wire preflight `reserve → dispatch → reconcile/release` around every Supervisor LLM call.
3. Add concurrent reservation tests and unknown-usage tests.
4. Then decompose Orchestrator into request/planning/execution/review/recovery facades with
   characterization tests before moving code.

No live API calls or production SQLite changes should be made for these steps.
