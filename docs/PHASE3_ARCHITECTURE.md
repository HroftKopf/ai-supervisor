# Phase 3.0 architecture proposal — parallel workers

This is a design document, not an implementation. Phase 2.9 remains sequential at the
`WorkerScheduler` level per task; multiple tasks can occupy worker slots.

## Goals

- Execute independent DAG stages concurrently when their dependencies are complete.
- Keep one Git worktree per task and, for parallel stages, a deterministic isolated workspace.
- Bound resource usage by CPU, memory, provider budget and approvals.
- Preserve deterministic recovery and idempotency after crashes.

## Proposed model

```text
Task DAG
  stage A ──┐
  stage B ──┼──> merge/validate junction
  stage C ──┘

Scheduler
  ready queue: stages with all dependencies completed
  worker pool: N isolated DSH runtimes
  per-stage state: pending/ready/running/completed/failed/blocked
```

### Git/worktrees

- One task branch/worktree remains the source of truth.
- Parallel stages must not edit the same files. The planner declares `allowed_paths` per stage;
  conflicting stages are serialized or rejected by a deterministic conflict check.
- Each parallel stage receives a separate worktree derived from the task base commit, then
  results are integrated in dependency order via an explicit integration stage. Never run two
  writers in the same worktree.
- Integration is commit/cherry-pick-based; force-push remains forbidden.

### Locks and resources

- A project lock protects branch/base updates; a task lock protects lifecycle transitions.
- Stage locks are keyed by `(task_id, normalized_path_set)`; overlapping sets cannot run together.
- SQLite stays the single writer connection with short transactions; stage results are committed
  after each stage. A per-stage lease prevents duplicate execution after recovery.
- Provider budgets are per stage/task and use the existing usage ledger; the scheduler refuses
  to start work that would exceed a task budget.

### Failure/recovery

- A running stage is marked `interrupted` on startup; only stages with no completed result are
  eligible to re-run.
- Idempotency keys are `(task_id, stage_id, base_commit, input_hash)`.
- A failed integration leaves completed stage worktrees/results available but never publishes.
- Approval payloads bind stage id, source hash, repository, branch and commit.

## Limits of the current code

- `TaskStageService` already persists stage state, but execution is sequential in
  `Orchestrator._execute_composite`.
- `WorkspaceManager` assumes one worktree per task.
- `WorkerScheduler` limits concurrent tasks, not stages.
- SQLite uses one shared connection and serialized operations; parallel stage transactions need
  bounded queueing and contention tests.
- Provider rate-limit/quota state is per runtime and must be coordinated across workers.

## Acceptance criteria for implementation

- deterministic conflict rejection;
- no duplicate external action after crash;
- per-stage budgets/approvals;
- integration tests with two real worktrees and a mock provider;
- no weakening of bwrap, egress, GitAuthManager or approvals.
