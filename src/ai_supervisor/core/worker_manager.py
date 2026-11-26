from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai_supervisor.config import Settings
from ai_supervisor.core.egress import EgressProxy
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.sandbox import BwrapSandbox
from ai_supervisor.core.workspace import WorkspaceManager
from ai_supervisor.memory.tasks import TaskService
from ai_supervisor.models import ProjectRecord, TaskRecord, TokenUsage, WorkerResult

logger = logging.getLogger(__name__)


class WorkerError(RuntimeError):
    pass


def _system_prompt() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "worker.md"
    return path.read_text(encoding="utf-8")


class WorkerManager:
    def __init__(
        self,
        *,
        settings: Settings,
        sandbox: BwrapSandbox,
        workspace: WorkspaceManager,
        tasks: TaskService,
        redactor: SecretRedactor,
    ) -> None:
        self.settings = settings
        self.sandbox = sandbox
        self.workspace = workspace
        self.tasks = tasks
        self.redactor = redactor
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._cancelled: set[int] = set()

    async def run(
        self,
        *,
        task: TaskRecord,
        project: ProjectRecord,
        prompt: str,
        attempt: int,
        resume: bool,
        timeout_seconds: float,
    ) -> WorkerResult:
        if task.id in self._cancelled:
            raise WorkerError("задача отменена до запуска исполнителя")
        worktree = Path(task.worktree_path or "")
        if not worktree.is_dir():
            raise WorkerError("worktree не существует")
        dsh_home = (
            Path(task.dsh_home or "")
            if task.dsh_home
            else self.settings.dsh_homes_dir / f"task-{task.id:04d}"
        )
        dsh_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        dsh_home.chmod(0o700)
        state_dir = self.settings.task_logs_dir / f"task-{task.id:04d}" / f"attempt-{attempt}"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        session_id = task.session_id or f"session-{uuid.uuid4().hex}"
        worker_id = f"task-{task.id:04d}-attempt-{attempt}"
        await self.tasks.create_worker(
            worker_id=worker_id,
            task_id=task.id,
            kind="deepseek-harness",
            session_id=session_id,
            dsh_home=str(dsh_home),
        )
        await self.tasks.update_worker(worker_id, status="starting")
        paths = self.workspace.sandbox_paths(
            worktree, dsh_home, state_dir, self.workspace.extra_read_paths(project)
        )
        egress_socket: Path | None = None
        egress_proxy: EgressProxy | None = None
        if self.settings.egress.enabled:
            base_url = self.settings.deepseek_base_url or "https://api.deepseek.com"
            parsed = urlparse(base_url)
            allowed = list(self.settings.egress.allowed_hosts)
            ports = list(self.settings.egress.allowed_ports)
            if parsed.hostname:
                allowed.append(parsed.hostname)
            if parsed.port:
                ports.append(parsed.port)
            egress_socket = state_dir / "egress.sock"
            egress_proxy = EgressProxy(
                egress_socket,
                allowed_hosts=allowed,
                allowed_ports=ports,
                max_connections=self.settings.egress.max_connections,
            )

        argv = self.sandbox.worker_argv(
            paths,
            session_id=session_id,
            model=self.settings.deepseek_model,
            provider=self.settings.deepseek.provider,
            profile=self.settings.deepseek.profile,
            max_tokens=self.settings.deepseek.max_tokens,
            reasoning_effort=self.settings.deepseek.reasoning_effort,
            system_prompt=_system_prompt(),
            deepseek_api_key=self.settings.deepseek_key,
            deepseek_base_url=self.settings.deepseek_base_url,
            resume=resume,
            egress_socket="/state/egress.sock" if egress_socket else None,
        )
        stdout_path = state_dir / "worker.stdout.log"
        stderr_path = state_dir / "worker.stderr.log"
        started = time.monotonic()
        try:
            if egress_proxy is not None:
                await egress_proxy.start()
            with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
                os.chmod(stdout_path, 0o600)
                os.chmod(stderr_path, 0o600)
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
                self._processes[task.id] = process
                await self.tasks.update_worker(worker_id, status="running", pid=process.pid)
                if task.id in self._cancelled:
                    self._terminate(task.id, process, signal.SIGTERM)
                    await process.wait()
                    raise WorkerError("задача отменена во время запуска исполнителя")
                timed_out = False
                try:
                    await asyncio.wait_for(
                        process.communicate(prompt.encode("utf-8")), timeout=timeout_seconds
                    )
                except TimeoutError:
                    timed_out = True
                    self._terminate(task.id, process, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=15)
                    except TimeoutError:
                        self._terminate(task.id, process, signal.SIGKILL)
                        await process.wait()
                except asyncio.CancelledError:
                    self._terminate(task.id, process, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10)
                    except TimeoutError:
                        self._terminate(task.id, process, signal.SIGKILL)
                        await process.wait()
                    raise
                finally:
                    self._processes.pop(task.id, None)
        finally:
            if egress_proxy is not None:
                await egress_proxy.stop()
        duration = time.monotonic() - started
        _redact_log_file(stdout_path, self.redactor, max_bytes=2_000_000)
        _redact_log_file(stderr_path, self.redactor, max_bytes=2_000_000)
        stdout_tail = self.redactor.redact(_tail(stdout_path, 20000))
        stderr_tail = self.redactor.redact(_tail(stderr_path, 20000))
        result_path = state_dir / "worker-result.json"
        result = self._read_result(result_path)
        if result is None:
            message = "исполнитель завершился без результата"
            if timed_out:
                message = "исполнитель превысил тайм-аут"
            result = WorkerResult(
                session_id=session_id,
                error=message,
                finish_reason=None,
                timed_out=timed_out,
                return_code=process.returncode,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                duration_seconds=duration,
            )
        else:
            result = result.model_copy(
                update={
                    "timed_out": timed_out or result.timed_out,
                    "return_code": process.returncode,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                    "duration_seconds": duration,
                    "usage": result.usage.model_copy(update={"duration_ms": int(duration * 1000)}),
                    "final_response": self.redactor.redact(result.final_response),
                    "error": self.redactor.redact(result.error) if result.error else None,
                }
            )
            try:
                result_path.write_text(result.model_dump_json(), encoding="utf-8")
                os.chmod(result_path, 0o600)
            except OSError:
                pass
        await self.tasks.update_worker(
            worker_id,
            status="timeout"
            if result.timed_out
            else ("finished" if result.completed else "failed"),
            session_id=result.session_id,
            usage=result.usage,
        )
        await self.tasks.add_event(
            task.id,
            "worker",
            f"Исполнитель {worker_id}: {result.finish_reason or 'no-result'}",
            {
                "worker_id": worker_id,
                "return_code": result.return_code,
                "duration_seconds": round(result.duration_seconds, 1),
                "usage": result.usage.model_dump(),
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
            },
        )
        return result

    def _read_result(self, path: Path) -> WorkerResult | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        usage_raw = data.pop("usage", {})
        if isinstance(usage_raw, dict):
            try:
                data["usage"] = TokenUsage.model_validate(usage_raw)
            except Exception:
                data["usage"] = TokenUsage()
        data.pop("event_types", None)
        try:
            return WorkerResult.model_validate(data)
        except Exception as exc:
            logger.warning("invalid worker result: %s", exc)
            return None

    def is_running(self, task_id: int) -> bool:
        return task_id in self._processes

    async def cancel(self, task_id: int) -> bool:
        self._cancelled.add(task_id)
        process = self._processes.get(task_id)
        if process is None:
            return False
        await self.tasks.add_event(task_id, "cancel", "Запрошена остановка исполнителя")
        self._terminate(task_id, process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            self._terminate(task_id, process, signal.SIGKILL)
        return True

    def clear_cancel(self, task_id: int) -> None:
        self._cancelled.discard(task_id)

    @staticmethod
    def _terminate(task_id: int, process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
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


def _redact_log_file(path: Path, redactor: SecretRedactor, *, max_bytes: int) -> None:
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    cleaned = redactor.redact(text)
    try:
        path.write_text(cleaned, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return


def _tail(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(-limit, os.SEEK_END)
            data = handle.read(limit)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


class WorkerScheduler:
    def __init__(self, max_workers: int) -> None:
        self._semaphore = asyncio.Semaphore(max_workers)
        self.max_workers = max_workers
        self._active: dict[int, asyncio.Task[Any]] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def available_slots(self) -> int:
        return max(0, self.max_workers - self.active_count)

    async def run(self, task_id: int, job: Any) -> Any:
        await self._semaphore.acquire()
        current = asyncio.current_task()
        if current is not None:
            self._active[task_id] = current
        try:
            return await job()
        finally:
            self._active.pop(task_id, None)
            self._semaphore.release()

    async def cancel(self, task_id: int) -> bool:
        task = self._active.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True

    def active_task_ids(self) -> list[int]:
        return list(self._active)
