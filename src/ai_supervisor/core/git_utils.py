from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_OUTPUT = 2_000_000


@dataclass(slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


class ProcessError(RuntimeError):
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        super().__init__(
            f"command failed ({result.returncode}): {' '.join(result.argv[:6])}\n{result.stderr.strip()[:2000]}"
        )


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 60.0,
    input_bytes: bytes | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT,
) -> ProcessResult:
    started = asyncio.get_running_loop().time()
    child_env = dict(os.environ) if env is None else dict(env)
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        env=child_env,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            process.communicate(input_bytes), timeout=timeout_seconds
        )
    except TimeoutError:
        timed_out = True
        _kill_process_group(process, signal.SIGTERM)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError:
            _kill_process_group(process, signal.SIGKILL)
            stdout_b, stderr_b = await process.communicate()
    duration = asyncio.get_running_loop().time() - started
    return ProcessResult(
        argv=tuple(str(arg) for arg in argv),
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=_decode_capped(stdout_b, max_output_bytes),
        stderr=_decode_capped(stderr_b, max_output_bytes),
        duration_seconds=duration,
        timed_out=timed_out,
    )


def _kill_process_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            return


def _decode_capped(raw: bytes, limit: int) -> str:
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace")
    head = raw[: limit // 2]
    tail = raw[-limit // 2 :]
    return (
        head.decode("utf-8", errors="replace")
        + f"\n...[truncated {len(raw) - limit} bytes]...\n"
        + tail.decode("utf-8", errors="replace")
    )


class GitError(RuntimeError):
    pass


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C.UTF-8",
        }
    )
    return env


async def git(
    cwd: Path | str,
    *args: str,
    timeout_seconds: float = 60.0,
    check: bool = True,
) -> ProcessResult:
    result = await run_process(
        ("git", *args),
        cwd=cwd,
        env=git_env(),
        timeout_seconds=timeout_seconds,
        max_output_bytes=4_000_000,
    )
    if check and (result.returncode != 0 or result.timed_out):
        raise GitError(result.stderr.strip() or f"git exited {result.returncode}")
    return result


async def git_output(cwd: Path | str, *args: str, timeout_seconds: float = 60.0) -> str:
    return (await git(cwd, *args, timeout_seconds=timeout_seconds)).stdout.strip()


async def git_toplevel(path: Path) -> Path:
    output = await git_output(path, "rev-parse", "--show-toplevel")
    return Path(output).resolve()


async def is_git_repository(path: Path) -> bool:
    result = await git(path, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"
