# Control plane

Control plane: Telegram handlers → RequestEnvelope → Orchestrator → routing/planning/review →
ProviderRegistry. Security/approvals/budget are Python boundaries. Data plane remains DeepSeek
Harness in bwrap. See `docs/PHASE3_ARCHITECTURE.md` for future parallel design; Phase 3 is not
implemented.
