from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from ai_supervisor.core.git_auth import GitAuthError, GitAuthManager, GitAuthMissing
from ai_supervisor.core.git_ops import fetch_remote, push_branch, remote_branch_sha


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_with_remote(repo: Path, url: str) -> None:
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-qm", "initial")
    subprocess.run(
        ["git", "remote", "remove", "origin"], cwd=repo, check=False, capture_output=True
    )
    _git(repo, "remote", "add", "origin", url)


@pytest.mark.asyncio
async def test_https_github_askpass_and_cleanup(settings, repo: Path) -> None:
    _repo_with_remote(repo, "https://github.com/owner/repo.git")
    settings.secrets.github_token = SecretStr("test-token-abcdefghijklmnop")
    settings.github.push_hosts = ["github.com"]
    manager = GitAuthManager(settings)
    async with manager.context(repo, "origin", operation="push") as ctx:
        assert ctx.authenticated is True
        assert ctx.url == "https://github.com/owner/repo.git"
        assert "test-token" not in str(ctx.env)
        assert "GITHUB_TOKEN" not in ctx.env
        askpass = Path(ctx.env["GIT_ASKPASS"])
        token_file = askpass.parent / "token"
        assert token_file.stat().st_mode & 0o777 == 0o600
        result = subprocess.run(
            [str(askpass), "Password for 'https://x-access-token@github.com':"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "test-token-abcdefghijklmnop"
        assert "test-token" not in _git(repo, "config", "--list")
        tempdir = askpass.parent
    assert not tempdir.exists()
    manager.close()


@pytest.mark.asyncio
async def test_third_party_host_never_receives_github_credentials(settings, repo: Path) -> None:
    _repo_with_remote(repo, "https://git.example.test/owner/repo.git")
    settings.secrets.github_token = SecretStr("test-token-abcdefghijklmnop")
    manager = GitAuthManager(settings)
    async with manager.context(repo, "origin", operation="push") as ctx:
        assert ctx.authenticated is False
        assert ctx.env["GIT_ASKPASS"] == "/bin/false"
        assert "test-token" not in str(ctx.env)
        assert ctx.url == "https://git.example.test/owner/repo.git"
    manager.close()


@pytest.mark.asyncio
async def test_ssh_remote_is_not_rewritten(settings, repo: Path) -> None:
    _repo_with_remote(repo, "git@github.com:owner/repo.git")
    settings.secrets.github_token = SecretStr("test-token-abcdefghijklmnop")
    manager = GitAuthManager(settings)
    async with manager.context(repo, "origin", operation="push") as ctx:
        assert ctx.authenticated is False
        assert ctx.url == "git@github.com:owner/repo.git"
        assert ctx.env["GIT_ASKPASS"] == "/bin/false"
        assert ctx.env["GIT_SSH_COMMAND"] == "ssh"
        assert "test-token" not in str(ctx.env)
    manager.close()


@pytest.mark.asyncio
async def test_missing_token_is_resumable_auth_error(settings, repo: Path) -> None:
    _repo_with_remote(repo, "https://github.com/owner/repo.git")
    settings.secrets.github_token = SecretStr("")
    manager = GitAuthManager(settings)
    with pytest.raises(GitAuthMissing):
        async with manager.context(repo, "origin", operation="push"):
            pass
    manager.close()


@pytest.mark.asyncio
async def test_untrusted_local_git_config_is_rejected(settings, repo: Path) -> None:
    _repo_with_remote(repo, "https://github.com/owner/repo.git")
    _git(repo, "config", "--local", "url.https://evil.example/.insteadOf", "https://github.com/")
    settings.secrets.github_token = SecretStr("test-token-abcdefghijklmnop")
    manager = GitAuthManager(settings)
    with pytest.raises(GitAuthError, match="untrusted local config"):
        async with manager.context(repo, "origin", operation="push"):
            pass
    manager.close()


@pytest.mark.asyncio
async def test_ls_remote_fetch_push_share_authenticated_env(
    settings, repo: Path, monkeypatch
) -> None:
    _repo_with_remote(repo, "https://github.com/owner/repo.git")
    settings.secrets.github_token = SecretStr("test-token-abcdefghijklmnop")
    manager = GitAuthManager(settings)
    local_sha = _git(repo, "rev-parse", "HEAD")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def fake_git(cwd, *args, timeout_seconds=60.0, check=True, env=None):
        from ai_supervisor.core.git_utils import ProcessResult

        calls.append((tuple(str(arg) for arg in args), dict(env or {})))
        if "ls-remote" in args:
            return ProcessResult(
                argv=("git", *args),
                returncode=0,
                stdout=f"{local_sha}\trefs/heads/main\n",
                stderr="",
                duration_seconds=0,
            )
        return ProcessResult(
            argv=("git", *args), returncode=0, stdout="", stderr="", duration_seconds=0
        )

    monkeypatch.setattr("ai_supervisor.core.git_ops.git", fake_git)
    assert await remote_branch_sha(repo, "origin", "main", auth=manager) == local_sha
    await fetch_remote(repo, "origin", "refs/heads/main", auth=manager)
    assert await push_branch(repo, "origin", "main", auth=manager) == local_sha
    assert len(calls) == 4
    for args, env in calls:
        assert "https://github.com/owner/repo.git" in args
        assert "GIT_ASKPASS" in env
        assert "test-token" not in str(env)
        assert "test-token" not in " ".join(args)
    manager.close()


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried(settings, repo: Path, monkeypatch) -> None:
    _repo_with_remote(repo, "https://github.com/owner/repo.git")
    settings.secrets.github_token = SecretStr("test-token-abcdefghijklmnop")
    manager = GitAuthManager(settings)
    calls = 0

    async def fake_git(cwd, *args, timeout_seconds=60.0, check=True, env=None):
        from ai_supervisor.core.git_utils import ProcessResult

        nonlocal calls
        calls += 1
        return ProcessResult(
            argv=("git", *args),
            returncode=128,
            stdout="",
            stderr="fatal: Authentication failed for 'https://github.com/owner/repo.git/'",
            duration_seconds=0,
        )

    monkeypatch.setattr("ai_supervisor.core.git_ops.git", fake_git)
    with pytest.raises(Exception) as exc:
        await push_branch(repo, "origin", "main", auth=manager)
    assert type(exc.value).__name__ == "GitAuthRejected"
    assert calls == 1
    manager.close()


def test_default_git_environment_does_not_inherit_secrets(monkeypatch) -> None:
    from ai_supervisor.core.git_utils import git_env

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-abcdefghijklmnop")
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-secret-value")
    env = git_env()
    assert "GITHUB_TOKEN" not in env
    assert "KIMI_API_KEY" not in env
