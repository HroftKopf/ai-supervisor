from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_supervisor.core.git_ops import (
    GitOpsError,
    push_branch,
    remote_branch_sha,
    validate_branch_name,
    validate_remote_name,
)


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_push_and_verify_without_force(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    _run("git", "init", "--bare", "-q", str(remote), cwd=tmp_path)
    repo.mkdir()
    _run("git", "init", "-b", "main", "-q", cwd=repo)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run("git", "add", "README.md", cwd=repo)
    _run("git", "commit", "-qm", "initial", cwd=repo)
    _run("git", "remote", "add", "origin", str(remote), cwd=repo)

    sha = await push_branch(repo, "origin", "main")
    assert await remote_branch_sha(repo, "origin", "main") == sha
    with pytest.raises(GitOpsError):
        validate_remote_name("--force")
    with pytest.raises(GitOpsError):
        validate_branch_name("../escape")
