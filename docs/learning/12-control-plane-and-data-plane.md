# 12. Control plane и data plane

**Control plane** принимает решения: маршрутизация, планирование, ревью, approvals и бюджеты.
**Data plane** исполняет: DeepSeek Harness в bwrap/worktree, Git, checks.

В ai-supervisor:

- control plane — `agents/supervisor.py`, `providers/`, `core/routing.py`,
  `core/permissions.py`, `core/orchestrator.py`;
- data plane — `core/worker_manager.py`, `core/workspace.py`, `core/sandbox.py`,
  `worker_runner.py`.

`RequestEnvelope` фиксирует вход control plane; `ProviderCallIdentity` — конкретный LLM-вызов;
`TaskStatus` — состояние задачи; `EvidenceManifest` — доказательство состояния data plane.

Вопрос: почему LLM не должна сама выполнять Git push?
