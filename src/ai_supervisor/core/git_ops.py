from __future__ import annotations

import re
from pathlib import Path

from ai_supervisor.core.git_auth import (
    GitAuthManager,
    GitAuthRejected,
)
from ai_supervisor.core.git_utils import git, git_output

REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


class GitOpsError(RuntimeError):
    pass


def validate_remote_name(remote: str) -> str:
    if not REMOTE_RE.fullmatch(remote):
        raise GitOpsError(f"invalid git remote name: {remote!r}")
    return remote


def validate_branch_name(branch: str) -> str:
    if not BRANCH_RE.fullmatch(branch) or ".." in branch or branch.endswith("/"):
        raise GitOpsError(f"invalid git branch name: {branch!r}")
    return branch


async def remote_url(repo: Path, remote: str) -> str:
    validate_remote_name(remote)
    result = await git(repo, "remote", "get-url", remote, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise GitOpsError(f"git remote {remote!r} is not configured")
    return result.stdout.strip()


async def ensure_remote(repo: Path, remote: str, url: str) -> bool:
    """Create or update a remote, returning True when the configuration changed."""
    validate_remote_name(remote)
    if not url.strip():
        raise GitOpsError("remote URL is empty")
    current = await git(repo, "remote", "get-url", remote, check=False)
    if current.returncode == 0 and current.stdout.strip() == url.strip():
        return False
    if current.returncode == 0:
        result = await git(repo, "remote", "set-url", remote, url, check=False)
    else:
        result = await git(repo, "remote", "add", remote, url, check=False)
    if result.returncode != 0:
        raise GitOpsError(f"cannot configure git remote {remote!r}: {result.stderr.strip()[:1000]}")
    return True


def _raise_git_failure(
    *, operation: str, remote: str, stderr: str, stdout: str, authenticated: bool
) -> None:
    detail = (stderr.strip() or stdout.strip())[:2000]
    if authenticated and GitAuthManager.is_auth_error(detail, authenticated=True):
        raise GitAuthRejected(f"GitHub authentication failed during {operation}: {detail}")
    raise GitOpsError(f"{operation} failed for {remote!r}: {detail}")


async def remote_branch_sha(
    repo: Path,
    remote: str,
    branch: str,
    *,
    auth: GitAuthManager | None = None,
) -> str | None:
    validate_remote_name(remote)
    validate_branch_name(branch)
    if auth is not None:
        async with auth.context(repo, remote, operation="ls-remote") as ctx:
            result = await git(
                repo,
                "ls-remote",
                "--heads",
                ctx.url,
                f"refs/heads/{branch}",
                timeout_seconds=60,
                check=False,
                env=ctx.env,
            )
            if result.returncode != 0:
                _raise_git_failure(
                    operation="ls-remote",
                    remote=remote,
                    stderr=result.stderr,
                    stdout=result.stdout,
                    authenticated=ctx.authenticated,
                )
    else:
        result = await git(
            repo,
            "ls-remote",
            "--heads",
            remote,
            f"refs/heads/{branch}",
            timeout_seconds=60,
            check=False,
        )
        if result.returncode != 0:
            raise GitOpsError(f"cannot read remote {remote!r}: {result.stderr.strip()[:1000]}")
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    return line[0].split()[0]


async def push_branch(
    repo: Path,
    remote: str,
    branch: str,
    *,
    auth: GitAuthManager | None = None,
) -> str:
    """Push without force; return the verified remote SHA."""
    validate_remote_name(remote)
    validate_branch_name(branch)
    local = (await git_output(repo, "rev-parse", f"refs/heads/{branch}")).strip()
    if auth is not None:
        async with auth.context(repo, remote, operation="push") as ctx:
            result = await git(
                repo,
                "push",
                "--porcelain",
                ctx.url,
                f"refs/heads/{branch}:refs/heads/{branch}",
                timeout_seconds=180,
                check=False,
                env=ctx.env,
            )
            if result.returncode != 0:
                _raise_git_failure(
                    operation="push",
                    remote=remote,
                    stderr=result.stderr,
                    stdout=result.stdout,
                    authenticated=ctx.authenticated,
                )
    else:
        result = await git(
            repo,
            "push",
            "--porcelain",
            remote,
            f"refs/heads/{branch}:refs/heads/{branch}",
            timeout_seconds=180,
            check=False,
        )
        if result.returncode != 0:
            raise GitOpsError(
                "git push failed: " + (result.stderr.strip() or result.stdout.strip())[:2000]
            )
    remote_sha = await remote_branch_sha(repo, remote, branch, auth=auth)
    if remote_sha != local:
        raise GitOpsError(
            f"push verification failed: local {local[:12]} != remote {str(remote_sha)[:12]}"
        )
    return local


async def fetch_remote(
    repo: Path,
    remote: str,
    refspec: str = "",
    *,
    auth: GitAuthManager | None = None,
) -> None:
    validate_remote_name(remote)
    if auth is not None:
        async with auth.context(repo, remote, operation="fetch") as ctx:
            command = ["fetch", "--no-tags", ctx.url]
            if refspec:
                command.append(refspec)
            result = await git(repo, *command, timeout_seconds=180, check=False, env=ctx.env)
            if result.returncode != 0:
                _raise_git_failure(
                    operation="fetch",
                    remote=remote,
                    stderr=result.stderr,
                    stdout=result.stdout,
                    authenticated=ctx.authenticated,
                )
        return
    command = ["fetch", "--no-tags", remote]
    if refspec:
        command.append(refspec)
    result = await git(repo, *command, timeout_seconds=180, check=False)
    if result.returncode != 0:
        raise GitOpsError(f"git fetch failed: {result.stderr.strip()[:1000]}")


async def verify_remote_branch(
    repo: Path,
    remote: str,
    branch: str,
    *,
    auth: GitAuthManager | None = None,
) -> tuple[str, str | None]:
    local = (await git_output(repo, "rev-parse", f"refs/heads/{branch}")).strip()
    remote_sha = await remote_branch_sha(repo, remote, branch, auth=auth)
    return local, remote_sha
