# Phase 2.9.1 — Infrastructure Hardening report

Baseline: `a2e5584`; starting suite: 134 passed, 4 skipped; starting combined coverage: 61%.
Final suite: **154 passed, 4 skipped**; final coverage: **67.5% statements / 50.4% branches (63% combined)**.

No paid API calls were made. All tests used temporary directories, synthetic credentials and
mock providers/workers. `.env` values were not read or printed during this work.

## Findings

### P1 — fixed

1. **Failed SQLite initialization leaked a connection/thread.** `Database.connect()` opened
   `aiosqlite` before schema/migration work. On a corrupt database the exception was raised
   without closing the connection, leaving a non-daemon thread and hanging pytest shutdown.
   `connect()` now closes the connection on every setup exception.
2. **`/usage` HTML was not used by all menu/callback paths.** `cmd_usage` was already fixed,
   but `menu:usage` and `use:<task>` callbacks sent `<b>…</b>` without `parse_mode="HTML"`.
   Both paths now use the Presentation Layer convention and a mock-notifier regression test.
3. **No quota circuit breaker across requests.** A task paused on quota, but a new request
   could immediately call the exhausted provider. `ProviderRegistry` now blocks the provider
   account after quota, keeps the task, and only `/provider test` can clear the block after a
   successful health check.
4. **Quota/rate errors exposed raw provider text.** Task pause now uses a human message:
   provider quota exhausted, task paused, progress saved, no invented recovery time, local
   commands available.
5. **Worker lifecycle had no subprocess tests.** Added fake workers for success, nonzero exit,
   corrupt result, timeout, SIGTERM/cancel, cancellation before start and child process-group
   cleanup.
6. **No fault-injection boundary.** Added `core/fault_injection.py` with named points and an
   `after_git_push` hook. Retry logic already verifies remote SHA before re-pushing.

### P2/P3 — documented

- WorkerManager coverage is 78%, not the 85% target: real DSH signal races, disk-full and
  supervisor SIGKILL at every boundary are not fully exercised.
- Sandbox coverage is 57%, target 90%: real bwrap adversarial mount/namespace tests are limited
  to DSH integration and argv/env inspection; symlink/path checks are unit-tested elsewhere.
- ProviderRegistry is 59% (`registry.py`), target 90%: account-level multi-tenant and live
  provider health paths are not covered.
- Handlers are 19%, target 75%: aiogram dispatch and many command branches still lack tests.
- Redaction is 75%, target 90%; Usage is 74%; Permissions/Approvals 85% vs 95%.
- No multi-process SQLite writer test; application intentionally uses one aiosqlite connection
  with `BEGIN IMMEDIATE` and `busy_timeout=5000`. External-writer contention, disk-full and
  WAL recovery are not tested.
- No complete fault matrix at every listed point; framework exists and the critical
  after-external-push boundary is hooked.
- Dependency lock is exact-version only, without hashes; vulnerability database is unavailable
  (`pip-audit` not installed), so no vulnerability-free claim is made.

## Architecture

No import cycles detected (AST check, 57 modules). Removed dead
`SupervisorAgent._read_prompt`. No Phase 3.0 parallel implementation was added.
Proposal remains in `docs/PHASE3_ARCHITECTURE.md`.

## Security tests added

- synthetic worker env contains only `DEEPSEEK_API_KEY`; no control-provider/Telegram/GitHub keys;
- `--clearenv`, `--unshare-net`, `--die-with-parent` present; host home not mounted;
- symlink outside worktree is not added as a host mount;
- worker child process in same process group is terminated on timeout;
- streaming/chunked redaction buffers until safe; URL-encoded/nested values redacted;
- logging filter masks messages, args, stack and exception text;
- provider quota gate and approval single-use/action binding tests.

These are tests of the Python boundary and command construction, not proof of universal
prompt-injection or sandbox invulnerability.

## SQLite/concurrency tests

- 40 concurrent inserts and 20 concurrent reads through the serialized Database API;
- transaction rollback on arbitrary exception;
- newer schema version refused without downgrade;
- corrupt DB raises `sqlite3.DatabaseError` and now closes its connection;
- existing recovery tests cover interruption marking and COMPOSITE stage persistence.

Not tested: two independent OS processes writing, disk-full, WAL corruption, crash exactly
between an external Git operation and result save (the `after_git_push` hook models it).

## Fault-injection hooks

`core/fault_injection.py` defines: task create, worker start/end, validation, Git commit/push,
notification and memory-update points. Opt-in only through
`AI_SUPERVISOR_FAULT_POINTS`; production default is off. Critical idempotency:
`_stage_publish` returns `already_published` when remote SHA equals local; a retry after a
post-push failure therefore verifies remote before re-pushing.

## Provider registry / quotas

Covered:

- circuit breaker/quota gate;
- Retry-After extraction and bounded exponential backoff with jitter;
- rate limit vs quota vs auth/provider-unavailable/network classification;
- no paid-provider fallback;
- `/provider test` health check clears quota only on success;
- deterministic commands do not need an LLM.

Not fully covered: multiple accounts for the same provider name, live streaming interruption,
active-task provider-switch behavior beyond handler refusal, full `/provider` command matrix.

## Benchmarks

Offline benchmark, temporary DB, no model calls. Baseline Phase 2.9 sample vs 2.9.1:

| metric | Phase 2.9 | 2.9.1 |
|---|---:|---:|
| memory search | 0.654 ms | 0.649 ms |
| context build | 0.351 ms | 0.340 ms |
| state-machine cycle median | 2.313 ms | 2.299 ms |
| 20 scheduler jobs | 0.222 ms | 0.198 ms |
| recovery | 0.378 ms | 0.452 ms |
| context peak | 25,693 B | 25,693 B |

These are environment-specific infrastructure numbers, not model speed or token savings.
Recovery variance is noise at this sample size; no improvement is claimed.

## Exit criteria / readiness

- No confirmed P0.
- P1 issues above fixed.
- Required security/recovery scenarios partially pass; multi-process SQLite, disk-full,
  complete crash matrix, live Telegram and live provider tests remain.
- Not ready to claim Phase 2.9.1 “accepted” for all targets: coverage targets are not met.
- Ready for further Phase 2.9.2 hardening, not yet for Phase 3.0 parallel execution.

## Update and rollback

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
python -m pip install -r requirements.lock
ai-supervisor config-audit
python scripts/audit_secrets.py
pytest -q
```

The lock is exact-version, not hash-pinned; see `docs/CONFIGURATION.md`. Rollback the commit;
configuration/schema changes are additive. Never delete logs/backups without owner approval.

## Required commands

```bash
pytest -q
ruff check .
coverage run --branch --source=src/ai_supervisor -m pytest -q
coverage report -m
AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py
python benchmarks/run_offline.py
python scripts/audit_secrets.py
ai-supervisor config-audit
```
