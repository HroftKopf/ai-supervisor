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
- Owner amendments/revisions: `/amend`, `/revisions`, revision history, atomic activation,
  approval binding and artifact invalidation.
- Tests increased from 160 to 201; coverage remains ~68% statements / 51% branches.

## Not completed — do not claim Phase 2.9.3 accepted

- Budget Engine 2.0: no reservation table/lifecycle, no preflight reservation before dispatch, no
  atomic concurrent reservation, no unknown-usage reconciliation.
- Provider recovery is implemented offline with a mock provider: `/provider recover`, single-flight,
  approval binding and persisted clear. Live paid generation probe was not executed.
- Context Engine 2.0: typed fragments, per-role fragment budgets, mandatory retention,
  deduplication pipeline and diagnostics are not implemented. Only memory retrieval and reviewer
  worktree freshness are partial.
- Orchestrator decomposition: not started; orchestrator remains monolithic.
- Request ID is persisted on tasks/requests; stage/usage foreign-key linkage is still partial.
- Full fault-injection matrix, multi-process SQLite, disk-full, and live provider/Telegram tests are
  not complete.
- Learning chapters 12–18 and architecture docs CONTROL_PLANE/PROVIDER_LIFECYCLE/TASK_RECOVERY/
  CONTEXT_ENGINE/BUDGET_ENGINE are not complete.

## Request integrity / deduplication (B.1)

- Durable `requests` table with unique `dedup_key`.
- Dedup key is Telegram-derived: `telegram:<chat_id>:<message_id>:<mode>`.
- Request capture uses an atomic conditional reclaim (`status='captured' AND task_id IS NULL`);
  only the SQLite winner processes.
- Task insert, accepted event and request binding are one SQLite transaction.
- Existing task by `request_id` is discovered before creating a second task/event.
- `/task` and `/auto` both use the request gateway; `/task` preserves `source_message_id`.
- `recovery_reset()` returns crashed `processing` requests to `captured` only when no task was attached.
- Failure lifecycle: `retryable`, `rejected`, `cancelled`, `failed`, `chat`, `task`.
- `/request retry <request_id>` re-queues only retryable/failed/cancelled requests; no automatic paid retry.
- Original owner/project/conversation/request_id are preserved across reclaim and project switch.
- `source_request` remains immutable through normal `update_fields`.
- Original requests over 8000 chars are rejected, not truncated.
- Title hints and request records are redacted.

Regression tests: duplicate Telegram delivery, identical text with different message IDs, 20 concurrent
captures, 20 concurrent reclaims, reclaim keeps request_id/project, restart redelivery, `/task`
source id, transactional task/binding rollback, existing task discovery, classifier quota/network
lifecycle, cancellation, long-request rejection, redaction, immutable fields, owner authorization,
v9→v10 migration preserving tasks/memory.

Current full suite: **201 passed, 4 skipped**; coverage **68% statements / 51% branches**.

## Owner amendments / request revisions

- `request_revisions` stores revision number, previous revision, effective requirements,
  owner amendment, author, origin, fingerprint and timestamp.
- Initial revision is created atomically for new tasks; legacy tasks get a lazy initial revision
  with `author_id=NULL` and origin `initial` (no invented provenance).
- `/amend <task_id> <full new requirements>` shows task/current revision/new text/invalidations and
  requests owner approval. `/amend` is a full replacement, not a model-generated patch.
- `/revisions <task_id>` reads SQLite only; no LLM call.
- Atomic activation updates task active revision, clears TaskSpec/review/evidence pointers, revokes
  pending task approvals, writes an audit event and preserves worktree/commits/logs as historical.
- Approval is bound to task, expected revision and content fingerprint; stale/concurrent approvals
  fail.
- Planner uses active revision and records `TaskSpec.request_revision`; resume invalidates stale
  specs. Amendment never starts a worker automatically.
- Allowed amendment states: paused, interrupted, blocked, retry_wait, waiting_input,
  waiting_approval, failed. Running/reviewing/completed tasks are rejected.

Regression tests: original preserved, revision provenance, planner effective text, running/
completed rejection, unauthorized owner, old approval rejection, concurrent amendments,
spec/review/evidence invalidation, old approval revocation, SQLite rollback, restart persistence,
legacy initial revision, `/revisions` without LLM, no automatic worker.

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
