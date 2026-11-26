# Phase 2.9.3 — Control Plane Optimization report

Baseline: `a8fcb07`. No paid API calls, no production credentials used, no production DB modified.

## Findings confirmed from the audit

1. **Provider quota isolation was broken.** Quota handling marked every role in
   `ProviderRegistry`, so a Kimi failure could block OpenAI-independent roles.
2. **Usage attribution used non-canonical role strings.** `planning`, `classification`,
   `review`, `analysis` did not resolve as `planner`, `classifier`, `reviewer`, etc.; failures
   silently fell back to Kimi metadata. `last_raw_usage` was shared mutable state.
3. **Task intent was mutable.** `_plan_task` used the latest conversation message, so a message
   arriving after task creation could change requirements.
4. **Review checkpoint recovery reran the worker.** Evidence could be persisted, review could
   fail on quota, and resume would execute DeepSeek again instead of reviewing the checkpoint.
5. **`/usage` menu/callback paths did not set HTML parse mode**, despite `cmd_usage` doing so.
6. **Health check conflated model listing with paid-generation readiness.**
7. **Memory used the latest 100 records instead of retrieval relevance.**
8. **Reviewer received a project snapshot taken before the worker.**
9. **Structured retry silently doubled `max_completion_tokens`** beyond the approved routing
   budget.

## Implemented

### Request Gateway / immutable intent

- `core/request_gateway.py` builds a `RequestEnvelope` with request/correlation IDs, owner/chat/
  conversation/project IDs, source message ID, normalized redacted text, attachment refs,
  timestamp, mode, source revision and policy ID.
- SQLite schema v7 adds `tasks.request_id`, `source_request`, `request_revision`,
  `intent_source`.
- New tasks store the original request at creation. Planner uses `task.source_request`; new
  conversation messages cannot replace it.
- Legacy tasks default to `legacy-inferred` when no original request exists.
- `/task` and `/auto` paths preserve the original request; `/chat` bypasses planner.

### Review checkpoint

`_execute_task` detects persisted evidence without a review and resumes at review without
starting a worker. COMPOSITE tasks are explicitly excluded. If the checkpoint cannot be accepted,
evidence is preserved and the task is blocked for an owner decision rather than silently rerun.

### Provider roles and quota

- Canonical `AgentRole` enum (chat, classifier, planner, reviewer, summarizer, experience,
  analysis), with legacy aliases at the boundary.
- Usage records canonical roles and actual provider/model.
- Raw usage is stored in a `ContextVar`, not shared mutable Supervisor state.
- Quota state is provider-scoped: Kimi quota does not block OpenAI.
- Providers expose generation readiness separately from `models.list`; model listing does not
  clear a quota block.
- `/provider test` explicitly reports connectivity, model listing and that generation was not
  probed.

### Routing and reasoning

- `/chat`, `/task`, `/auto` explicit Telegram modes. `/task` does not grant permissions or bypass
  approvals.
- `RoutingPolicy.for_chat`: conversational questions such as “Что такое архитектура?” use a
  lightweight profile; explicit engineering requests can still escalate.
- Structured `finish_reason=length` no longer silently increases the token budget; it returns a
  clear error and does not retry with a larger limit.
- Reviewer project context is rebuilt after worker changes.
- Memory retrieval uses FTS relevance for the current request, filtered by project/conversation
  and stale status.

### Schema

SQLite schema v7 adds request provenance fields. Migration backup is
`pre-migration-v7-*.db`; additive columns preserve existing data.

## Verification

- **160 passed, 4 skipped**, synthetic credentials only.
- Offline benchmark after the change (single samples, current host): memory search 0.88–1.04 ms,
  context build 0.43–0.54 ms, state transitions median 3.5–3.6 ms, 20 scheduler jobs 0.28–0.30 ms,
  recovery 0.53–0.57 ms, peak context 25,693 B. These are slower than the Phase 2.9 sample
  (0.65/0.34/2.3/0.22/0.45 ms); the measurements are not controlled for host load, so no
  regression or improvement claim is made.
- New tests: request envelope, immutable intent, review checkpoint, provider quota isolation,
  canonical role resolution, generation-readiness separation, explicit modes, relevance memory.
- Existing COMPOSITE/publish/approval tests pass.
- Full suite and DSH integration executed separately; no live provider calls.

## Coverage

Coverage is not at the Phase 2.9.3 target. The current measured result is approximately **68%
statements / 51% branches (64% combined)**. Telegram handlers, WorkerManager edge branches,
sandbox mount failures and a full fault matrix remain uncovered. No exclusions were used to
inflate the result.

## Remaining limitations

- Full Context Engine typed fragments and per-role token budgets are not implemented; only
  relevance retrieval and refreshed reviewer context.
- Usage reservation (`reserve → dispatch → reconcile`) is not implemented; preflight is still
  conservative token/request checks. No concurrency-safe financial hard cap is claimed.
- Provider `account_scope_id`, endpoint/model-scoped quota and multi-account support are not
  implemented. Current state is provider-name scoped.
- `request_id` is not yet stored on every usage event/stage/request.
- Deterministic fast path for ambiguous free-form text is intentionally limited; `/auto` still
  uses classifier.
- Orchestrator remains large; handlers and worker lifecycle coverage remain below target.
- DeepSeek control provider is available through the compatible adapter only when explicitly
  configured; no automatic fallback exists.
- Real Telegram visual verification and live provider generation probes were not performed.

## Update / rollback

Update normally; schema v7 is additive with backup. Rollback the commit; existing tasks retain
request provenance columns and legacy tasks remain readable. Do not delete logs or backups without
owner approval.
