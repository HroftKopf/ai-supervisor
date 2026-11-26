from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ai_supervisor.config import Settings
from ai_supervisor.core.git_utils import git_output


class GitAuthError(RuntimeError):
    """Base class for a controlled Git authentication failure."""


class GitAuthMissing(GitAuthError):
    """The remote requires GitHub credentials, but no usable token exists."""


class GitAuthRejected(GitAuthError):
    """GitHub rejected the supplied credentials."""


@dataclass(slots=True)
class GitAuthContext:
    url: str
    env: dict[str, str]
    authenticated: bool
    temporary_dir: Path | None = None


_DANGEROUS_CONFIG_PREFIXES = (
    "credential.",
    "http.",
    "url.",
    "include.",
    "includeif.",
    "core.sshcommand",
    "core.hookspath",
    "core.askpass",
    "core.fsmonitor",
    "core.gitproxy",
    "remote.",
)
_AUTH_ERROR_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "askpass",
    "invalid username or password",
    "http 401",
    "http 403",
)


class GitAuthManager:
    """Centralized, host-checked Git HTTPS authentication.

    The token is written only to a private temporary askpass file. It is never
    placed in argv, the remote URL, repository config, Git config, SQLite or logs.
    SSH remotes are passed through without converting them to HTTPS.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tempdirs: set[Path] = set()
        self._root = settings.data_dir / "git-auth"
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        atexit.register(self.close)

    def cleanup_stale(self) -> int:
        """Remove auth temp dirs left by a crashed previous process."""
        removed = 0
        for child in self._root.glob("git-auth-*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    def close(self) -> None:
        for directory in list(self._tempdirs):
            shutil.rmtree(directory, ignore_errors=True)
            self._tempdirs.discard(directory)

    @property
    def allowed_hosts(self) -> set[str]:
        return {host.lower().strip("[]") for host in self.settings.github.push_hosts}

    @staticmethod
    async def raw_remote_url(repo: Path, remote: str) -> str:
        # git config returns the stored URL without network access. It is still
        # validated below; remote.*.pushurl is rejected as a config override.
        result = await git_output(repo, "config", "--local", "--get", f"remote.{remote}.url")
        if not result:
            raise GitAuthError(f"git remote {remote!r} is not configured")
        return result

    async def _check_local_config(self, repo: Path) -> None:
        result = await git_output(repo, "config", "--local", "--includes", "--list", "--name-only")
        offending: list[str] = []
        for raw in result.splitlines():
            key = raw.strip().lower()
            if not key:
                continue
            if key.startswith(_DANGEROUS_CONFIG_PREFIXES):
                # Normal remote URL/fetch metadata is required; remote
                # pushurl/proxy/uploadpack overrides are not trusted.
                if key.startswith("remote.") and key.rsplit(".", 1)[-1] in {
                    "url",
                    "fetch",
                    "tagopt",
                }:
                    continue
                offending.append(key)
        if offending:
            raise GitAuthError(
                "refusing authenticated Git operation: untrusted local config key(s): "
                + ", ".join(sorted(set(offending))[:10])
            )

    async def _token(self) -> str:
        token = self.settings.github_token
        if not token:
            raise GitAuthMissing("GITHUB_TOKEN не настроен; HTTPS-операции GitHub требуют токен")
        return token

    def _safe_base_env(self) -> dict[str, str]:
        # Do not inherit model/Telegram/GitHub secrets from the supervisor env.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_SSH_COMMAND": "ssh",
        }
        if "SSH_AUTH_SOCK" in os.environ:
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        return env

    @asynccontextmanager
    async def context(
        self, repo: Path, remote: str, *, operation: str
    ) -> AsyncIterator[GitAuthContext]:
        repo = repo.resolve()
        url = await self.raw_remote_url(repo, remote)
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().strip("[]")

        if scheme in {"https", "http"}:
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise GitAuthError("remote URL contains credentials/query/fragment")
            if scheme != "https" and host not in {"127.0.0.1", "localhost", "::1"}:
                raise GitAuthError("refusing non-TLS HTTP Git remote outside localhost")
            if host in self.allowed_hosts:
                token = await self._token()
                await self._check_local_config(repo)
                tempdir = Path(tempfile.mkdtemp(prefix="git-auth-", dir=str(self._root)))
                os.chmod(tempdir, 0o700)
                self._tempdirs.add(tempdir)
                token_file = tempdir / "token"
                fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, token.encode("utf-8"))
                finally:
                    os.close(fd)
                askpass = tempdir / "askpass.sh"
                askpass.write_text(
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                    '  *Password*) cat "$(dirname "$0")/token" ;;\n'
                    "  *) exit 1 ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                os.chmod(askpass, 0o700)
                env = self._safe_base_env()
                env.update(
                    {
                        "GIT_ASKPASS": str(askpass),
                        "GIT_ASKPASS_REQUIRE": "force",
                        "GIT_TERMINAL_PROMPT": "0",
                    }
                )
                env.update(_git_config_env(operation))
                try:
                    yield GitAuthContext(
                        url=url,
                        env=env,
                        authenticated=True,
                        temporary_dir=tempdir,
                    )
                finally:
                    shutil.rmtree(tempdir, ignore_errors=True)
                    self._tempdirs.discard(tempdir)
                return
            # Non-GitHub HTTPS remotes get no GitHub token. Public read-only
            # operations may still work; authenticated push will fail clearly.
            await self._check_local_config(repo)
            env = self._safe_base_env()
            env.update(_git_config_env(operation))
            yield GitAuthContext(url=url, env=env, authenticated=False)
            return

        if scheme in {"ssh", "git+ssh", "git"} or (
            parsed.scheme == "" and "@" in url and ":" in url
        ):
            await self._check_local_config(repo)
            env = self._safe_base_env()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_ASKPASS"] = "/bin/false"
            env["GIT_SSH_COMMAND"] = "ssh"
            env.update(_git_config_env(operation))
            yield GitAuthContext(url=url, env=env, authenticated=False)
            return

        if scheme in {"", "file"}:
            env = self._safe_base_env()
            env.update(_git_config_env(operation))
            yield GitAuthContext(url=url, env=env, authenticated=False)
            return

        raise GitAuthError(f"unsupported Git remote scheme: {scheme or '(empty)'}")

    @staticmethod
    def is_auth_error(stderr: str, *, authenticated: bool) -> bool:
        if not authenticated:
            return False
        lowered = stderr.lower()
        return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


def _git_config_env(operation: str) -> dict[str, str]:
    # These command-line-equivalent config entries outrank repo-local config.
    # They clear credential helpers/proxies, disable redirects, enforce TLS and
    # prevent hooks from observing the operation.
    entries = [
        ("core.hooksPath", "/dev/null"),
        ("credential.helper", ""),
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("http.proxy", ""),
        ("http.extraHeader", ""),
    ]
    env: dict[str, str] = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env
