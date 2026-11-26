# Task recovery

Task status remains the state machine; durable evidence checkpoints are separate. Resume verifies
request revision, spec hash, worktree manifest and HEAD/index/tracked/untracked state. Stale
evidence blocks for owner decision. COMPOSITE resumes from persisted stages; publish verifies
remote SHA before repeating. Full checkpoint parity and all crash boundaries remain partial.
