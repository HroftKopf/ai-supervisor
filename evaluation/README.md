# Prompt evaluation

- `scenarios.json` — 24 fixed scenarios across read_only, modify, execute, git, composite,
  bugfix, feature, refactor, permissions, contradiction, already_done, unsupported and
  prompt-injection.
- `src/ai_supervisor/evaluation/harness.py` — deterministic checks: task type, required
  operations, stage count, allowed tools, criteria, unique stage ids/dependencies, routing level.
- `scripts/evaluate_prompts.py` — offline validation by default; `--live` calls Kimi only when
  the owner explicitly authorizes the paid run.

## Baseline vs active prompt profile sizes (characters, not tokens)

| profile | baseline 2.5.1 | active 2.6.0 |
|---|---:|---:|
| supervisor | 2460 | 2460 |
| classifier | 2460 (old inline supervisor) | 510 |
| planner | 5610 | 6063 |
| reviewer | 2309 | 2778 |
| summarizer | 552 | 552 |
| worker | 2028 | 2210 |

The classifier is substantially smaller because classification was split from the supervisor
profile. Planner/reviewer gained explicit minimality/severity rules, so their files are slightly
larger. Character counts are **not** a token measurement and are not evidence of a cost reduction.

## Actual measurements

A live A/B run was not completed: the configured Kimi account reached its 5-hour quota and
returned HTTP 429. No token/cost reduction is claimed. The harness and baseline/active prompts
are ready for a separately authorized paid run:

```bash
python scripts/evaluate_prompts.py --live --prompts baseline --json-out var/eval-baseline.json
python scripts/evaluate_prompts.py --live --prompts active --json-out var/eval-active.json
```
