# Configuration

AI Supervisor uses two files:

- `config.toml` — all non-secret settings;
- `.env` — only credentials, tokens and personal identifiers.

The normalizer accepts the legacy flat layout (`[app]`, `[kimi]`, `[deepseek]`,
`[limits]`, `[egress]`, ...) and the grouped layout documented below. Grouped values win
over legacy values. `AI_SUPERVISOR_CONFIG` may point to another config file.

Templates:

- `.env.example` — safe placeholders only;
- `config.example.toml` — safe config with a placeholder project root.

Never commit `.env`, `var/`, local backups, or a filled provider configuration.

## Secrets (`.env`)

Grouped by service. Only key names are documented; values stay local.

| Service | Variable | Required when |
|---|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` | always |
| Telegram | `TELEGRAM_OWNER_ID` | always; numeric user ID |
| Kimi | `KIMI_API_KEY` | any role uses `kimi` |
| DeepSeek | `DEEPSEEK_API_KEY` | always (execution backend) |
| OpenAI | `OPENAI_API_KEY` | any role uses `openai` |
| Custom Providers | `OPENAI_COMPATIBLE_API_KEY` | any role uses `openai_compatible` |
| GitHub | `GITHUB_TOKEN` | optional; `/github` and HTTPS Git push |

`.env` must be mode 600 when `security.require_env_mode_600 = true`.
Provider keys are redacted from logs/Telegram and are never passed to worker processes.

## Grouped `config.toml`

### `[application]`

| Key | Default | Meaning |
|---|---|---|
| `timezone` | `Europe/Moscow` | Timezone for reports and daily usage boundaries. |
Recovery settings live in `[recovery]` below.

### `[telegram]`

| Key | Default | Notes |
|---|---|---|
| `private_chat_only` | `true` | Refuse groups/channels. Keep true. |
| `polling_timeout_seconds` | `30` | Long-poll timeout, 5–90. |
| `max_message_chars` | `4000` | Telegram hard limit is 4096; leave room for HTML entities. |

### `[providers.kimi]`

| Key | Default | Notes |
|---|---|---|
| `base_url` | `https://api.kimi.ai/coding/v1` | Must end in `/v1`; Kimi Code and Moonshot URLs are not interchangeable. |
| `model` | `k3.1-pro` | Documented Kimi Code ID; no invented IDs. |
| `reasoning_effort` | `high` | `low`, `high`, `max`; max costs more. |
| `max_completion_tokens` | `32768` | 1024–1048576. |
| `request_timeout_seconds` | `180` | Per-request timeout. |
| `max_retries` | `3` | Transient failures only; quota/authentication are not retried blindly. |
| `max_concurrent_requests` | `2` | Per-provider semaphore. |
| `verify_model_on_startup` | `true` | Calls provider model listing; no completion. |

### `[providers.deepseek]`

DeepSeek Harness is the execution backend, independent of control-provider selection.

| Key | Default | Notes |
|---|---|---|
| `provider` | `deepseek-official` | SDK provider. |
| `base_url` | `""` | Empty = SDK default; non-secret endpoint override. |
| `model` | `deepseek-v4-flash` | Preserved local value after migration is `deepseek-flash`. |
| `profile` | `sdk-minimal` | No managed credentials/settings. |
| `max_tokens` | `32768` | Worker output budget. |
| `reasoning_effort` | `""` | Empty = model default. |
| `initialize_timeout_seconds` | `60` | Runtime startup timeout. |
| `shutdown_timeout_seconds` | `5` | Runtime shutdown timeout. |

### `[providers.openai]` and `[providers.openai_compatible]`

| Key | OpenAI default | Compatible default |
|---|---|---|
| `base_url` | `https://api.openai.com/v1` | empty |
| `model` | `gpt-4.1` | empty |
| `reasoning` | `false` | `false` |
| `prompt_cache` | `true` | `false` |

Enable `reasoning` only for models/endpoints that support the reasoning parameter.
OpenAI uses the Responses API; compatible providers use Chat Completions.

### `[agent_roles]`

One provider per role: `default_provider`, `chat`, `classifier`, `planner`, `reviewer`,
`summarizer`, `experience`, `analysis`. Allowed providers: `kimi`, `openai`, `openai_compatible`.
There is no automatic fallback between paid APIs. If the selected provider has no credentials,
the request fails explicitly.

### `[routing]` and `[reasoning]`

Routing levels are deterministic in code: native commands, simple, standard, complex.
`[reasoning]` sets per-level effort and output budget:

| Key | Default | Range |
|---|---|---|
| `level1_effort` / `level1_max_tokens` | `low` / `2048` | low/high/max; 512–32768 |
| `level2_effort` / `level2_max_tokens` | `high` / `8192` | 1024–65536 |
| `level3_effort` / `level3_max_tokens` | `max` / `16384` | 2048–131072 |
| `classifier_effort` / `classifier_max_tokens` | `low` / `2048` | 512–16384 |

These are budgets, not a promise of quality. Higher effort increases token use and latency.

### `[task_limits]`

| Key | Default | Meaning |
|---|---|---|
| `max_rework` | `3` | Review/rework cycles before blocking. |
| `max_task_minutes` | `45` | Wall-clock task cap. |
| `max_iterations` | `12` | Executor iteration cap. |
| `max_task_kimi_requests` | `40` | Legacy name; control-model requests per task. |
| `max_task_total_tokens` | `0` | 0 = unlimited; total input+output per task. |
| `daily_token_limit` | `0` | 0 = unlimited. |
| `daily_request_limit` | `0` | 0 = unlimited. |
| `warn_at_percent` | `80` | Usage warning threshold, 1–100. |

### `[scheduler]`

`max_workers` (default `3`, 1–16) controls parallel execution slots. Raise only after measuring
CPU, memory and provider limits.

### `[memory]`

| Key | Default | Meaning |
|---|---|---|
| `max_context_messages` | `16` | Recent messages included in context. |
| `max_context_chars` | `24000` | Conversation context character cap. |
| `summarize_after_messages` | `40` | Trigger rolling summary. |
| `summary_max_chars` | `6000` | Summary length cap. |

### `[strategies]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Select relevant strategy files dynamically. |
| `max_selected` | `2` | Maximum strategies per planner request (0–5). |

Whole strategy library is never sent.

### `[context]`

| Key | Default | Meaning |
|---|---|---|
| `max_project_context_chars` | `30000` | Project snapshot cap. |
| `max_file_read_bytes` | `262144` | Single file read cap. |
| `max_diff_chars` | `80000` | Diff size cap before truncation. |

These are context/resource limits; they do not weaken sandbox or approvals.

### `[security]`

| Key | Default | Security note |
|---|---|---|
| `allowed_project_roots` | `["/home/youruser/Projects"]` | Strict project roots; no `/` or home. |
| `require_env_mode_600` | `true` | Refuse group/world-readable `.env`. |
| `allow_unsafe_no_sandbox` | `false` | Never enable in production. |

### `[sandbox]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `bwrap` | Only `bwrap` is safe; `none` requires explicit unsafe flag. |
| `memory_mb` | `4096` | Per-worker memory cap. |
| `cpu_seconds_per_worker` | `3000` | Per-worker CPU cap. |
| `max_open_files` | `1024` | File descriptor cap. |
| `max_processes` | `1024` | Process cap. |

### `[network]` and `[network.egress]`

| Key | Default | Meaning |
|---|---|---|
| `sandbox_network` | `true` | Worker network namespace/proxy availability. |
| `egress.enabled` | `true` | Allowlist proxy. |
| `egress.allowed_hosts` | `["api.deepseek.com"]` | Only these hosts. |
| `egress.allowed_ports` | `[80, 443]` | Allowed ports. |
| `egress.max_connections` | `32` | Proxy connection cap. |

Do not disable egress; the worker has no direct shared network.

### `[git]`

| Key | Default | Meaning |
|---|---|---|
| `author_name` | `AI Supervisor` | Commit author. |
| `author_email` | `ai-supervisor@localhost` | Commit email. |
| `base_branch` | `main` | Base branch for worktrees. |
| `commit_prefix` | `ai-supervisor` | Branch/commit prefix. |

### `[github]`

| Key | Default | Meaning |
|---|---|---|
| `api_url` | `https://api.github.com` | GitHub API URL. |
| `default_private` | `true` | New repositories private by default. |
| `push_hosts` | `["github.com"]` | Hosts allowed to receive `GITHUB_TOKEN`. |
| `timeout_seconds` | `30` | API timeout. |

### `[usage]`

Pricing is optional and explicit. Zero means cost is not calculated; no invented prices.

```toml
[usage.pricing."openai/gpt-4.1"]
input_per_mtok = 0.0
output_per_mtok = 0.0
cache_read_per_mtok = 0.0
cache_write_per_mtok = 0.0
currency = "USD"
```

Usage stores actual provider/model, normalized tokens, raw provider usage JSON, cache/reasoning
metrics and cost when a price is configured. Cache/reasoning tokens are not double-counted.

### `[recovery]`

`auto_resume_composite`, `auto_resume_git_auth_blocked` — same semantics as `[application]`.

### `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `log_level` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL. No credentials in any level. |
| `task_log_retention_days` | `14` | Worker log retention. |
| `max_worker_log_chars` | `20000` | Worker output tail cap. |

### `[storage]`

`data_dir` (default `var`) stores SQLite, worktrees, logs, uploads and runtime homes.
Relative paths resolve against the project root.

### `[backups]`

| Key | Default | Meaning |
|---|---|---|
| `directory` | `var/backups` | Local backup directory, gitignored. |
| `keep` | `20` | Number of pre-migration database backups retained. |
| `before_schema_migration` | `true` | Backup before adding schema columns. |

Backups are created mode 600. Config backups are stored under `var/backups/config/` and are not
tracked by Git.

## Legacy migration

Old flat keys are accepted and mapped:

| Legacy | Grouped |
|---|---|
| `[app]` | `[application]`, `[storage]`, `[logging]`, `[recovery]` |
| `[kimi]` | `[providers.kimi]` |
| `[deepseek]` | `[providers.deepseek]` |
| `[llm]` | `[providers.openai]`, `[providers.openai_compatible]`, `[agent_roles]` |
| `[routing]` | `[reasoning]` |
| `[limits]` | `[task_limits]`, `[scheduler]`, `[usage]` |
| `[security] max_*` | `[context]` |
| `[sandbox] network` | `[network] sandbox_network` |
| `[egress]` | `[network.egress]` |
| `[usage] prices` | `[usage.pricing]` |

Old `KIMI_BASE_URL`, `KIMI_MODEL`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` environment variables
remain explicit overrides for compatibility, but they are non-secret and belong in `config.toml`.
New `.env` files should contain none of them.

Removed after usage audit: `memory.max_search_results` and
`context.max_merge_conflict_chars` (not referenced anywhere). `max_task_kimi_requests` is kept
because it is enforced, but its historical name is retained for compatibility.

## Validation and diagnostics

Startup fails with a clear `ConfigError` for:

- unknown top-level section;
- unknown setting in grouped sections;
- wrong type/range;
- unknown provider or agent role;
- unsupported provider role;
- malformed/credential-bearing provider URL;
- unsafe sandbox without explicit opt-in.

`ai-supervisor doctor` reports:

- config path and section/provider coverage;
- presence of `config.example.toml`;
- whether non-secret keys remain in `.env`;
- backup directory/retention;
- each role’s provider/model availability without printing keys.

`/models`, `/provider info`, `/provider test [role]` expose the same model diagnostics in Telegram;
credentials are never shown.


## Safe diagnostics

Never print `.env` or pass it through `cat`/`grep`. Use:

```bash
ai-supervisor config-audit
```

The command reads only variable names/status, config structure, file modes and provider roles.
It prints no secret values or lengths. `ai-supervisor doctor` uses the same safe metadata and
never prints credentials.

The application also masks provider/Telegram/GitHub secrets in log messages, expression
arguments, stack traces and model context. For a local working-tree scan that reports only
file paths/key names, use:

```bash
python scripts/audit_secrets.py
```

If a credential is accidentally exposed, rotate it; do not rely on local log deletion alone.
