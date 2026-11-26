# 16. Checkpoints и idempotency

`EvidenceManifest` (`core/evidence_manifest.py`) связывает evidence с project/task/worktree,
HEAD, index, tracked diff, untracked metadata, spec hash и request revision.
`_resume_review_checkpoint` принимает evidence только при совпадении manifest; иначе задача
блокируется, а worker не перезапускается.

`_stage_publish` сначала проверяет remote SHA. Fault hook `after_git_push` моделирует падение
после внешнего действия. Это пример idempotency: повтор проверяет факт, а не слепо выполняет
операцию снова.

Вопрос: почему один HEAD SHA недостаточен для валидности evidence?
