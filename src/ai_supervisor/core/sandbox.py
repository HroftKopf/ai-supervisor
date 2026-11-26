from __future__ import annotations

import asyncio
import os
import pwd
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ai_supervisor.config import Settings
from ai_supervisor.core.git_utils import run_process

SYSTEM_DIRS = ("/usr",)
ETC_FILES = (
    "resolv.conf",
    "hosts",
    "nsswitch.conf",
    "passwd",
    "group",
    "localtime",
    "ld.so.cache",
    "gai.conf",
    "mime.types",
    "gitconfig",
)
ETC_DIRS = ("ssl", "ca-certificates", "pki")


class SandboxError(RuntimeError):
    pass


class SandboxUnavailable(SandboxError):
    pass


@dataclass(slots=True)
class SandboxPaths:
    worktree: Path
    dsh_home: Path
    state_dir: Path
    git_common_dir: Path | None = None
    git_worktree_name: str | None = None
    readonly_roots: tuple[Path, ...] = ()


@dataclass(slots=True)
class SandboxEnv:
    values: dict[str, str] = field(default_factory=dict)


class BwrapSandbox:
    """Bubblewrap-based filesystem/PID isolation for DSH workers.

    The network is intentionally shared in Phase 1 because the worker must reach
    its model API. File effects are confined to the worktree, the per-task DSH
    home and the state directory; the host home is never mounted.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._venv = Path(sys.prefix).resolve()
        self._app_src = Path(__file__).resolve().parents[1]
        self._config_dir = settings.root / "config"
        self._bwrap = "bwrap"

    async def check_available(self) -> None:
        if sys.prefix == sys.base_prefix:
            raise SandboxUnavailable(
                "AI Supervisor должен работать из venv: worker использует /venv/bin/python"
            )
        if not (self._app_src / "worker_runner.py").is_file():
            raise SandboxUnavailable(f"package source is unavailable: {self._app_src}")
        if not (self._config_dir / "dsh" / "disable-session-log-upload.yml").is_file():
            raise SandboxUnavailable(f"DSH privacy patch is missing: {self._config_dir}")
        if not Path("/usr/bin/bwrap").exists():
            raise SandboxUnavailable(
                "bubblewrap не установлен; исполнители не будут запущены без песочницы "
                "(установите пакет bubblewrap)"
            )
        result = await run_process(
            (
                self._bwrap,
                "--die-with-parent",
                "--unshare-pid",
                "--ro-bind",
                "/usr",
                "/usr",
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--",
                "/bin/true",
            ),
            timeout_seconds=15,
        )
        if result.returncode != 0 or result.timed_out:
            raise SandboxUnavailable(
                "bubblewrap не может создать изолированное окружение: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )

    def worker_argv(
        self,
        paths: SandboxPaths,
        *,
        session_id: str,
        model: str,
        provider: str,
        profile: str,
        max_tokens: int,
        reasoning_effort: str,
        system_prompt: str,
        deepseek_api_key: str,
        deepseek_base_url: str,
        resume: bool,
        egress_socket: str | None = None,
    ) -> list[str]:
        inner: list[str] = [
            "/venv/bin/python",
            "-m",
            "ai_supervisor.worker_runner",
            "--workspace",
            "/workspace",
            "--dsh-home",
            "/dsh-home",
            "--state-dir",
            "/state",
            "--session-id",
            session_id,
            "--provider",
            provider,
            "--profile",
            profile,
            "--model",
            model,
            "--max-tokens",
            str(max_tokens),
            "--memory-mb",
            str(self.settings.sandbox.memory_mb),
            "--cpu-seconds",
            str(self.settings.sandbox.cpu_seconds_per_worker),
            "--max-open-files",
            str(self.settings.sandbox.max_open_files),
            "--max-processes",
            str(self.settings.sandbox.max_processes),
            "--patch",
            "/config/dsh/disable-session-log-upload.yml",
        ]
        if reasoning_effort:
            inner.extend(["--reasoning-effort", reasoning_effort])
        if resume:
            inner.append("--resume")
        if egress_socket:
            inner.extend(["--egress-socket", egress_socket])
        env = {
            "PATH": "/venv/bin:/usr/bin:/bin",
            "HOME": "/dsh-home",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "/app",
            "DSH_HOME": "/dsh-home",
            "DSH_SYSTEM_PROMPT": system_prompt,
            "DEEPSEEK_API_KEY": deepseek_api_key,
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "NO_COLOR": "1",
        }
        if deepseek_base_url:
            env["DEEPSEEK_BASE_URL"] = deepseek_base_url
        if paths.git_common_dir is not None and paths.git_worktree_name:
            env["GIT_DIR"] = f"/repo-git/worktrees/{paths.git_worktree_name}"
            env["GIT_WORK_TREE"] = "/workspace"
        return self._wrap(paths, inner=inner, env=env, include_secrets=True)

    def check_argv(self, paths: SandboxPaths, command: str) -> list[str]:
        env = {
            "PATH": "/venv/bin:/usr/bin:/bin",
            "HOME": "/dsh-home",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONPATH": "/app",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "NO_COLOR": "1",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        }
        if paths.git_common_dir is not None and paths.git_worktree_name:
            env["GIT_DIR"] = f"/repo-git/worktrees/{paths.git_worktree_name}"
            env["GIT_WORK_TREE"] = "/workspace"
        argv = self._wrap(
            paths,
            inner=["/bin/bash", "-lc", command],
            env=env,
            include_secrets=False,
        )
        # Resource limits are enforced by prlimit around bwrap; the worker itself
        # applies the same limits in worker_runner. Check commands are untrusted
        # project configuration and must not be able to exhaust the host.
        if Path("/usr/bin/prlimit").exists():
            memory = max(256, self.settings.sandbox.memory_mb) * 1024 * 1024
            return [
                "/usr/bin/prlimit",
                f"--as={memory}",
                f"--cpu={self.settings.sandbox.cpu_seconds_per_worker}",
                f"--nofile={self.settings.sandbox.max_open_files}",
                f"--nproc={self.settings.sandbox.max_processes}",
                "--",
                *argv,
            ]
        return argv

    def _wrap(
        self,
        paths: SandboxPaths,
        *,
        inner: Sequence[str],
        env: Mapping[str, str],
        include_secrets: bool,
    ) -> list[str]:
        if not include_secrets:
            # Defence in depth: never allow model/Telegram credentials into a
            # project-supplied check command.
            env = {
                key: value
                for key, value in env.items()
                if key not in {"DEEPSEEK_API_KEY", "KIMI_API_KEY", "TELEGRAM_BOT_TOKEN"}
            }
        args: list[str] = [
            self._bwrap,
            "--die-with-parent",
            "--new-session",
        ]
        if self.settings.egress.enabled:
            # User namespace root is required to bring loopback up in the new
            # network namespace; it has no host capabilities or routes.
            args.extend(["--unshare-user", "--uid", "0", "--gid", "0"])
        args.extend(
            [
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-cgroup-try",
                "--cap-drop",
                "ALL",
            ]
        )
        if self.settings.egress.enabled:
            args.extend(["--cap-add", "CAP_NET_ADMIN"])
        args.extend(
            [
                "--hostname",
                "ai-supervisor-worker",
                "--clearenv",
                "--ro-bind",
                "/usr",
                "/usr",
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/bin",
                "/sbin",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--ro-bind",
                str(self._venv),
                "/venv",
                "--ro-bind",
                str(self._app_src),
                "/app/ai_supervisor",
                "--ro-bind",
                str(self._config_dir),
                "/config",
                "--bind",
                str(paths.worktree),
                "/workspace",
                "--bind",
                str(paths.dsh_home),
                "/dsh-home",
                "--bind",
                str(_home_cache_dir(paths.dsh_home)),
                "/" + str(_host_cache_dir()).lstrip("/"),
                "--bind",
                str(paths.state_dir),
                "/state",
                "--tmpfs",
                "/tmp",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--chdir",
                "/workspace",
            ]
        )
        for rel in ETC_FILES:
            self._append_etc_file(args, rel)
        for rel in ETC_DIRS:
            self._append_etc_dir(args, rel)
        for readonly in paths.readonly_roots:
            # Never shadow writable task paths with a read-only parent mount.
            if any(
                target == readonly or target.is_relative_to(readonly)
                for target in (paths.worktree, paths.dsh_home, paths.state_dir)
            ):
                continue
            # Mount only the explicitly allowed path, at its real absolute path.
            args.extend(["--ro-bind", str(readonly), str(readonly)])
        if paths.git_common_dir is not None:
            args.extend(["--ro-bind", str(paths.git_common_dir), "/repo-git"])
            sanitized = _sanitized_git_config(paths.git_common_dir, paths.state_dir)
            if sanitized is not None:
                args.extend(["--ro-bind", str(sanitized), "/repo-git/config"])
        if self.settings.egress.enabled or not self.settings.sandbox.network:
            args.insert(args.index("--cap-drop"), "--unshare-net")
        for key, value in env.items():
            args.extend(["--setenv", key, value])
        args.append("--")
        args.extend(inner)
        return args

    @staticmethod
    def _append_etc_file(args: list[str], rel: str) -> None:
        source = Path("/etc") / rel
        if not source.exists():
            return
        try:
            real = source.resolve()
        except OSError:
            return
        args.extend(["--ro-bind", str(real), f"/etc/{rel}"])

    @staticmethod
    def _append_etc_dir(args: list[str], rel: str) -> None:
        source = Path("/etc") / rel
        if not source.is_dir():
            return
        args.extend(["--ro-bind", str(source.resolve()), f"/etc/{rel}"])

    @staticmethod
    def sanitized_env_for_worker(
        base: Mapping[str, str], *, deepseek_api_key: str
    ) -> dict[str, str]:
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "TERM",
            "HOME",
            "TMPDIR",
            "DSH_HOME",
            "DEEPSEEK_BASE_URL",
            "SSL_CERT_FILE",
        }
        env = {key: value for key, value in base.items() if key in allowed}
        env["DEEPSEEK_API_KEY"] = deepseek_api_key
        return env


class UnsafeSandbox:
    """Explicit test-only backend. Refused unless the config opts in."""

    def __init__(self, settings: Settings) -> None:
        if not settings.security.allow_unsafe_no_sandbox:
            raise SandboxUnavailable("unsafe sandbox backend is not permitted")
        self.settings = settings

    async def check_available(self) -> None:
        return None

    def worker_argv(self, paths: SandboxPaths, **kwargs: object) -> list[str]:
        raise SandboxUnavailable("unsafe backend does not provide a worker command")


def _sanitized_git_config(common_dir: Path, state_dir: Path) -> Path | None:
    source = common_dir / "config"
    if not source.is_file():
        return None
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    kept: list[str] = []
    skip_section = False
    section = ""
    sensitive_keys = {
        "extraheader",
        "password",
        "token",
        "secret",
        "clientsecret",
        "authorization",
        "proxy",
        "sslcert",
        "sslkey",
    }
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").lower()
            skip_section = section.startswith(("credential", "url", "include"))
            if not skip_section:
                kept.append(line)
            continue
        if skip_section or not stripped or stripped.startswith(("#", ";")):
            continue
        key = stripped.split("=", 1)[0].strip().lower()
        if key in sensitive_keys or "credential" in key:
            continue
        if "=" in stripped:
            left, value = stripped.split("=", 1)
            value = value.strip()
            # Strip userinfo from URLs (https://user:token@host/...).
            if "://" in value and "@" in value:
                scheme, rest = value.split("://", 1)
                if "@" in rest:
                    value = scheme + "://" + rest.split("@", 1)[1]
            line = f"{left}={value}"
        kept.append(line)
    target = state_dir / "git-config-sanitized"
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def _host_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path.home()


def _host_cache_dir() -> Path:
    return _host_home() / ".cache"


def _home_cache_dir(dsh_home: Path) -> Path:
    path = dsh_home / "host-home-cache"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def create_sandbox(settings: Settings) -> BwrapSandbox:
    if settings.sandbox.backend == "bwrap":
        return BwrapSandbox(settings)
    raise SandboxUnavailable("sandbox backend must be bwrap; refusing to run without isolation")


async def path_exists(path: Path) -> bool:
    return await asyncio.to_thread(path.exists)
