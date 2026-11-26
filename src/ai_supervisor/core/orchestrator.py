from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_supervisor.agents.supervisor import SupervisorAgent, SupervisorError
from ai_supervisor.config import Settings
from ai_supervisor.core.context import ContextError, ProjectContextBuilder
from ai_supervisor.core.git_ops import GitOpsError, push_branch, remote_url, verify_remote_branch
from ai_supervisor.core.github import GitHubClient, GitHubError
from ai_supervisor.core.interfaces import Button, IncomingMessage, Notifier
from ai_supervisor.core.permissions import (
    Action,
    ApprovalError,
    ApprovalService,
    PermissionDenied,
    PermissionManager,
)
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.reviewer import ReviewGate, build_report
from ai_supervisor.core.usage import LimitExceeded, UsageService
from ai_supervisor.core.worker_manager import WorkerError, WorkerManager, WorkerScheduler
from ai_supervisor.core.workspace import WorkspaceError, WorkspaceManager
from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.projects import ProjectError, ProjectService, validate_remote_url
from ai_supervisor.memory.tasks import TaskError, TaskService
from ai_supervisor.models import (
    Evidence,
    IntentKind,
    ProjectRecord,
    ReviewResult,
    ReviewVerdict,
    RiskLevel,
    TaskRecord,
    TaskSpec,
    TaskStatus,
    TaskType,
    TransitionError,
    ensure_transition,
)
from ai_supervisor.providers.kimi import (
    KimiError,
    KimiQuotaError,
    KimiRateLimitError,
)

logger = logging.getLogger(__name__)

ALLOWED_TOOL_NAMES = {
    "filesystem",
    "git",
    "terminal",
    "exec",
    "external",
    "github",
    "config_import",
}

ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".rs",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".html",
    ".sh",
    ".bash",
    ".zsh",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".go",
    ".java",
    ".kt",
    ".sql",
    ".ini",
    ".cfg",
    ".conf",
    ".gitignore",
}


class OrchestratorError(RuntimeError):
    pass


def _locked_action(
    method: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Serialize state-changing actions for the same task (accept vs stop etc.)."""

    @functools.wraps(method)
    async def wrapper(self: Orchestrator, task_id: int, *args: Any, **kwargs: Any) -> None:
        lock = self._action_locks.setdefault(int(task_id), asyncio.Lock())
        async with lock:
            await method(self, task_id, *args, **kwargs)

    return wrapper


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        projects: ProjectService,
        conversations: ConversationService,
        tasks: TaskService,
        usage: UsageService,
        approvals: ApprovalService,
        supervisor: SupervisorAgent,
        workspace: WorkspaceManager,
        workers: WorkerManager,
        scheduler: WorkerScheduler,
        context_builder: ProjectContextBuilder,
        redactor: SecretRedactor,
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.projects = projects
        self.conversations = conversations
        self.tasks = tasks
        self.usage = usage
        self.approvals = approvals
        self.supervisor = supervisor
        self.workspace = workspace
        self.workers = workers
        self.scheduler = scheduler
        self.context_builder = context_builder
        self.redactor = redactor
        self.notifier = notifier
        self.permissions = PermissionManager()
        self._jobs: dict[int, asyncio.Task[Any]] = {}
        self._project_locks: dict[int, asyncio.Lock] = {}
        self._creation_lock = asyncio.Lock()
        self._action_locks: dict[int, asyncio.Lock] = {}
        self._last_usage_warning: str | None = None
        self._pause_requests: set[int] = set()
        self._stopping = False
        self.github = (
            GitHubClient(token=settings.github_token, config=settings.github)
            if settings.github_token
            else None
        )
        self._summaries: dict[int, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        await self.approvals.expire_old()
        await self._cleanup_old_task_logs()
        await self.tasks.mark_workers_interrupted_on_startup()
        interrupted = await self.tasks.mark_interrupted_on_startup()
        if interrupted:
            logger.warning(
                "marked %s task(s) interrupted after restart: %s", len(interrupted), interrupted
            )

    async def _cleanup_old_task_logs(self) -> None:
        import shutil
        import time

        cutoff = time.time() - self.settings.app.task_log_retention_days * 86400
        removed = 0

        def clean() -> int:
            count = 0
            root = self.settings.task_logs_dir
            if not root.exists():
                return 0
            for child in root.glob("task-*"):
                try:
                    if child.stat().st_mtime < cutoff:
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                        count += 1
                except OSError:
                    continue
            return count

        removed = await asyncio.to_thread(clean)
        if removed:
            logger.info("removed %s expired task log entries", removed)

    async def shutdown(self) -> None:
        self._stopping = True
        for task_id in list(self._jobs):
            await self.workers.cancel(task_id)
        for job in list(self._jobs.values()):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        for job in list(self._summaries.values()):
            job.cancel()
        if self._summaries:
            await asyncio.gather(*self._summaries.values(), return_exceptions=True)
        if self.github is not None:
            await self.github.close()

    # ------------------------------------------------------------------
    # incoming conversation
    # ------------------------------------------------------------------

    async def handle_incoming(self, message: IncomingMessage) -> None:
        self._assert_owner(message.user_id)
        if message.chat_id != message.user_id:
            # Telegram private chats have chat.id == user.id. Keep the check explicit.
            await self.notifier.send_text(message.chat_id, "Разрешён только личный чат владельца.")
            return
        project = await self._current_project(message.chat_id)
        if project is None:
            await self.notifier.send_text(
                message.chat_id,
                "Сначала зарегистрируйте проект: /project add <имя> <абсолютный путь>\n"
                "или создайте новый: /project create <имя>",
            )
            return
        conversation = await self.conversations.get_or_create_conversation(
            message.chat_id, project.id
        )
        attachment_text, images = await self._prepare_attachments(message)
        text = (message.text or "").strip()
        if text:
            text = self.redactor.redact(text)
        if attachment_text:
            text = (text + "\n\n" + attachment_text).strip()
        if not text and not images:
            await self.notifier.send_text(message.chat_id, "Пустое сообщение не обработано.")
            return
        user_message = await self.conversations.add_message(
            conversation.id,
            "user",
            text or "(изображение без текста)",
            telegram_message_id=message.telegram_message_id,
            reply_to_message_id=message.reply_to_message_id,
            meta={
                "has_images": bool(images),
                "attachments": [attachment.filename for attachment in message.attachments],
            },
        )
        project_block = self._project_block(project)
        summary, context_messages = await self.conversations.build_context(conversation.id)
        history_messages = [item for item in context_messages if item.id != user_message.id]
        conversation_block = self._conversation_block(history_messages)
        intent_result = None
        try:
            await self.usage.ensure_kimi_budget(0)
            intent_result, classify_usage = await self.supervisor.classify(
                project_block=project_block,
                conversation_block=conversation_block,
                latest_user_message=text or "(изображение)",
                conversation_id=conversation.id,
            )
            await self._record_kimi_usage(classify_usage, task_id=None, project_id=project.id)
        except KimiQuotaError as exc:
            await self._notify_quota(message.chat_id, exc)
            return
        except (KimiError, LimitExceeded) as exc:
            await self.notifier.send_text(message.chat_id, self._friendly_error(exc))
            return

        ambiguous = intent_result.clarification or intent_result.confidence < 0.6
        if intent_result.kind is IntentKind.TASK and not ambiguous:
            await self._begin_task(
                project=project,
                conversation_id=conversation.id,
                chat_id=message.chat_id,
                title_hint=intent_result.task_title_hint or "",
            )
            return

        await self._chat(
            project=project,
            conversation=conversation,
            summary=summary,
            context_messages=context_messages,
            images=images,
            chat_id=message.chat_id,
        )

    async def _chat(
        self,
        *,
        project: ProjectRecord,
        conversation: Any,
        summary: str | None,
        context_messages: list[Any],
        images: list[tuple[bytes, str]],
        chat_id: int,
    ) -> None:
        memory_block = await self.conversations.memory_context(
            project_id=project.id, conversation_id=conversation.id
        )
        api_messages = self.conversations.messages_for_api(context_messages)
        try:
            await self.usage.ensure_kimi_budget(0)
            response = await self.supervisor.chat(
                project_block=self._project_block(project),
                memory_block=memory_block,
                summary=summary,
                messages=api_messages,
                images=images,
                conversation_id=conversation.id,
            )
            await self._record_kimi_usage(response.usage, task_id=None, project_id=project.id)
        except KimiQuotaError as exc:
            await self._notify_quota(chat_id, exc)
            return
        except (KimiError, LimitExceeded) as exc:
            await self.notifier.send_text(chat_id, self._friendly_error(exc))
            return
        content = self.redactor.redact(response.content.strip())
        if not content:
            content = "Kimi не вернул текстовый ответ."
        await self.conversations.add_message(
            conversation.id,
            "assistant",
            content,
            meta={
                "reasoning_content": response.reasoning_content[:200_000],
                "model": response.model,
                "usage": response.usage.model_dump(),
            },
        )
        await self._send_long(chat_id, content)
        warning = await self.usage.warning()
        if warning:
            await self.notifier.send_text(chat_id, "⚠️ " + warning)
        self._schedule_summary(conversation.id, project.id)

    # ------------------------------------------------------------------
    # attachments / context helpers
    # ------------------------------------------------------------------

    async def _prepare_attachments(
        self, message: IncomingMessage
    ) -> tuple[str, list[tuple[bytes, str]]]:
        parts: list[str] = []
        images: list[tuple[bytes, str]] = []
        for attachment in message.attachments:
            if attachment.is_image:
                if attachment.mime_type not in ALLOWED_IMAGE_MIME:
                    parts.append(
                        f"[Изображение {attachment.filename} пропущено: формат {attachment.mime_type}]"
                    )
                    continue
                if attachment.size > 10 * 1024 * 1024:
                    parts.append(f"[Изображение {attachment.filename} пропущено: больше 10 МБ]")
                    continue
                images.append((attachment.data, attachment.mime_type))
                continue
            suffix = Path(attachment.filename).suffix.lower()
            is_text = attachment.mime_type.startswith("text/") or suffix in TEXT_SUFFIXES
            if not is_text:
                parts.append(
                    f"[Вложение {attachment.filename} ({attachment.mime_type}) не передано: "
                    "поддерживаются текстовые файлы и изображения]"
                )
                continue
            if attachment.size > self.settings.security.max_file_read_bytes:
                parts.append(
                    f"[Вложение {attachment.filename} пропущено: больше "
                    f"{self.settings.security.max_file_read_bytes} байт]"
                )
                continue
            try:
                content = attachment.data.decode("utf-8")
            except UnicodeDecodeError:
                parts.append(f"[Вложение {attachment.filename} не является UTF-8 текстом]")
                continue
            content = self.redactor.redact(content)
            parts.append(
                f"[НЕДОВЕРЕННОЕ ВЛОЖЕНИЕ: {attachment.filename}]\n"
                f"{content[: self.settings.security.max_file_read_bytes]}\n"
                f"[КОНЕЦ ВЛОЖЕНИЯ: {attachment.filename}]"
            )
        return "\n\n".join(parts), images

    def _project_block(self, project: ProjectRecord) -> str:
        instructions = project.instructions[:4000] if project.instructions else "(нет)"
        return (
            f"name: {project.name}\n"
            f"path: {project.path}\n"
            f"description: {project.description or '(нет)'}\n"
            f"stack: {project.stack or '(не указан)'}\n"
            f"base_branch: {project.base_branch}\n"
            f"instructions:\n{instructions}\n"
            f"extra_read_paths: {project.extra_read_paths_json}"
        )

    @staticmethod
    def _conversation_block(messages: list[Any]) -> str:
        lines: list[str] = []
        for message in messages[-12:]:
            content = message.content.strip()
            if not content:
                continue
            lines.append(f"{message.role.upper()}: {content[:3500]}")
        return "\n\n".join(lines) or "(история пуста)"

    async def current_project(self, chat_id: int) -> ProjectRecord | None:
        return await self._current_project(chat_id)

    async def _current_project(self, chat_id: int) -> ProjectRecord | None:
        project_id = await self.conversations.get_active_project_id(chat_id)
        if project_id is not None:
            project = await self.projects.get(project_id)
            if project is not None:
                return project
        project = await self.projects.default_project()
        if project is not None:
            await self.conversations.set_active_project(chat_id, project.id)
        return project

    # ------------------------------------------------------------------
    # task planning and execution
    # ------------------------------------------------------------------

    async def _begin_task(
        self,
        *,
        project: ProjectRecord,
        conversation_id: int,
        chat_id: int,
        title_hint: str,
    ) -> None:
        async with self._creation_lock:
            await self._begin_task_locked(
                project=project,
                conversation_id=conversation_id,
                chat_id=chat_id,
                title_hint=title_hint,
            )

    async def _begin_task_locked(
        self,
        *,
        project: ProjectRecord,
        conversation_id: int,
        chat_id: int,
        title_hint: str,
    ) -> None:
        active = await self.tasks.active(project.id)
        if active:
            ids = ", ".join(f"#{task.id:03d}" for task in active)
            await self.notifier.send_text(
                chat_id,
                f"В проекте {project.name} уже есть активная задача {ids}. "
                "Дождитесь её завершения, примите/отклоните изменения или остановите её через /stop.",
            )
            return
        title = (title_hint or "Новая задача").strip()[:120]
        task = await self.tasks.create(
            project.id,
            conversation_id,
            title,
            max_rework=self.settings.limits.max_rework,
        )
        await self.tasks.add_event(task.id, "accepted", "Задача принята от владельца")
        await self.notifier.send_text(
            chat_id,
            f"Задача #{task.id:03d} принята. Формирую техническое задание.",
            [[Button("Остановить", f"stop:{task.id}")]],
        )
        self._spawn_job(task.id, plan_if_needed=True, manual_instruction=None)

    def _spawn_job(
        self, task_id: int, *, plan_if_needed: bool, manual_instruction: str | None
    ) -> None:
        existing = self._jobs.get(task_id)
        if existing is not None and not existing.done():
            raise OrchestratorError("задача уже выполняется")
        job = asyncio.create_task(
            self._run_task(
                task_id, plan_if_needed=plan_if_needed, manual_instruction=manual_instruction
            ),
            name=f"ai-supervisor-task-{task_id}",
        )
        self._jobs[task_id] = job

        def _done(completed: asyncio.Task[Any]) -> None:
            self._jobs.pop(task_id, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error("task #%s job crashed: %s", task_id, error, exc_info=error)

        job.add_done_callback(_done)

    async def _run_task(
        self,
        task_id: int,
        *,
        plan_if_needed: bool,
        manual_instruction: str | None,
    ) -> None:
        task = await self.tasks.get(task_id)
        project = await self.projects.get(task.project_id)
        if project is None:
            await self._fail_without_project(task, "проект задачи не найден")
            return
        lock = self._project_locks.setdefault(project.id, asyncio.Lock())
        try:
            async with lock:
                task = await self.tasks.get(task_id)
                if task.status is TaskStatus.CANCELLED:
                    return
                if plan_if_needed or not task.spec:
                    task = await self._plan_task(task, project)
                    if task.status is not TaskStatus.QUEUED:
                        return
                if task.status in {TaskStatus.CREATED, TaskStatus.PLANNING}:
                    await self._try_transition(task.id, TaskStatus.QUEUED)
                    task = await self.tasks.get(task.id)
                if task.status is not TaskStatus.QUEUED:
                    logger.warning(
                        "task #%s not queued (status=%s); not executing", task.id, task.status
                    )
                    return
                project_context = await self.context_builder.build(project)
                await self.scheduler.run(
                    task.id,
                    lambda: self._execute_task(
                        task.id,
                        project=project,
                        project_context=project_context,
                        manual_instruction=manual_instruction,
                    ),
                )
        except asyncio.CancelledError:
            await self._handle_cancel(task_id)
        except LimitExceeded as exc:
            await self._pause_task(task_id, str(exc))
        except KimiQuotaError as exc:
            await self._pause_task(task_id, str(exc))
        except KimiRateLimitError as exc:
            await self._retry_wait(task_id, str(exc))
        except (
            KimiError,
            SupervisorError,
            WorkspaceError,
            WorkerError,
            ContextError,
            ProjectError,
        ) as exc:
            await self._fail_task(task_id, str(exc))
        except (TaskError, TransitionError) as exc:
            logger.error("task #%s state error: %s", task_id, exc)
            await self._fail_task(task_id, f"ошибка состояния задачи: {exc}")
        except Exception as exc:
            logger.exception("task #%s failed unexpectedly", task_id)
            await self._fail_task(task_id, f"{type(exc).__name__}: {exc}")

    async def _plan_task(self, task: TaskRecord, project: ProjectRecord) -> TaskRecord:
        if task.status is TaskStatus.CREATED:
            task = await self.tasks.transition(task.id, TaskStatus.PLANNING)
        elif task.status not in {
            TaskStatus.PLANNING,
            TaskStatus.QUEUED,
            TaskStatus.INTERRUPTED,
            TaskStatus.PAUSED,
        }:
            if task.spec:
                if task.status is TaskStatus.QUEUED:
                    return task
                await self._try_transition(task.id, TaskStatus.QUEUED)
                return await self.tasks.get(task.id)
            raise OrchestratorError(f"нельзя планировать задачу в статусе {task.status.value}")
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d}: собираю контекст проекта и формирую ТЗ.",
        )
        conversation = await self.conversations.get(task.conversation_id)
        _summary, context_messages = await self.conversations.build_context(task.conversation_id)
        user_messages = [message for message in context_messages if message.role == "user"]
        latest_user = user_messages[-1].content if user_messages else task.title
        memory_block = await self.conversations.memory_context(
            project_id=project.id, conversation_id=conversation.id
        )
        project_context = await self.context_builder.build(project)
        await self.usage.ensure_kimi_budget(task.id)
        spec, plan_usage = await self.supervisor.plan(
            project_block=self._project_block(project),
            conversation_block=self._conversation_block(context_messages),
            memory_block=memory_block,
            project_context=project_context,
            latest_user_message=latest_user,
            conversation_id=conversation.id,
        )
        await self._record_kimi_usage(plan_usage, task_id=task.id, project_id=project.id)
        task = await self.tasks.save_spec(task.id, spec)
        await self.tasks.add_event(
            task.id,
            "plan",
            f"ТЗ сформировано: {spec.title}",
            {"acceptance_criteria": spec.acceptance_criteria, "steps": spec.steps},
        )
        if task.status is not TaskStatus.QUEUED:
            task = await self.tasks.transition(task.id, TaskStatus.QUEUED)
        preflight = await self._preflight_contract(task, project, spec)
        if preflight.status is not TaskStatus.QUEUED:
            return preflight
        task = preflight
        criteria = "\n".join(f"• {item}" for item in spec.acceptance_criteria[:5])
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d}: план сформирован.\n\n{spec.title}\n{criteria}",
            [[Button("Остановить", f"stop:{task.id}")]],
        )
        return task

    async def _preflight_contract(
        self, task: TaskRecord, project: ProjectRecord, spec: TaskSpec
    ) -> TaskRecord:
        """Fail closed before the worker sees a contradictory or privileged contract."""
        unknown_tools = [tool for tool in spec.required_tools if tool not in ALLOWED_TOOL_NAMES]
        if unknown_tools:
            reason = "Неизвестные инструменты в контракте: " + ", ".join(unknown_tools)
            await self._block_task(task.id, reason)
            return await self.tasks.get(task.id)
        if RiskLevel.CRITICAL in spec.required_permissions:
            reason = "Контракт требует CRITICAL-полномочия; автоматическое выполнение запрещено."
            await self._block_task(task.id, reason)
            return await self.tasks.get(task.id)
        if spec.task_type in {
            TaskType.SYSTEM_OPERATION,
            TaskType.EXTERNAL_OPERATION,
            TaskType.COMPOSITE,
        }:
            reason = (
                f"Тип {spec.task_type.value} требует отдельного привилегированного исполнителя "
                "или декомпозиции; Phase 2 пока не исполняет его автоматически. "
                "Для Git-публикации используйте завершённую задачу и /push."
            )
            await self._block_task(task.id, reason)
            return await self.tasks.get(task.id)
        needs_approval = RiskLevel.HIGH in spec.required_permissions or spec.task_type in {
            TaskType.SYSTEM_OPERATION,
            TaskType.EXTERNAL_OPERATION,
        }
        if needs_approval:
            await self._request_task_approval(
                task,
                "Контракт требует подтверждения владельца: "
                f"тип={spec.task_type.value}, permissions="
                + ",".join(level.value for level in spec.required_permissions)
                + (", tools=" + ",".join(spec.required_tools) if spec.required_tools else ""),
            )
            return await self.tasks.get(task.id)
        return task

    async def _request_task_approval(self, task: TaskRecord, reason: str) -> int:
        if task.status is not TaskStatus.WAITING_APPROVAL:
            await self._try_transition(task.id, TaskStatus.WAITING_APPROVAL)
        approval_id = await self.approvals.request(
            action=Action.TASK_APPROVAL,
            payload={"task_id": task.id, "reason": reason},
            created_by=self.settings.owner_id,
            task_id=task.id,
            ttl_seconds=1800,
        )
        await self.tasks.add_event(
            task.id, "approval-requested", reason[:2000], agent="orchestrator"
        )
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d}: требуется подтверждение владельца.\n{reason[:1500]}",
            [
                [
                    Button("Разрешить один раз", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
                [Button("Показать diff", f"dif:{task.id}"), Button("Отменить", f"stop:{task.id}")],
            ],
        )
        return approval_id

    async def _execute_task(
        self,
        task_id: int,
        *,
        project: ProjectRecord,
        project_context: str,
        manual_instruction: str | None,
    ) -> None:
        task = await self.tasks.get(task_id)
        if task.cancel_requested:
            await self._try_transition(task_id, TaskStatus.CANCELLED)
            await self.notifier.send_text(
                self._chat_for_task(task), "Задача остановлена до запуска исполнителя."
            )
            return
        task = await self.tasks.transition(task_id, TaskStatus.RUNNING)
        await self.tasks.add_event(task_id, "running", "Работа началась")
        prepared = await self.workspace.prepare(task, project)
        dsh_home = self.settings.dsh_homes_dir / f"task-{task.id:04d}"
        dsh_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        task = await self.tasks.update_fields(
            task_id,
            worker_name="DeepSeek Harness",
            worktree_path=prepared["worktree_path"],
            branch=prepared["branch"],
            base_ref=prepared["base_ref"],
            base_commit=prepared["base_commit"],
            dsh_home=str(dsh_home),
        )
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d}: исполнитель начал работу в изолированном worktree.\n"
            f"Ветка: {task.branch}",
            [[Button("Остановить", f"stop:{task.id}")]],
        )
        attempt = 0
        resume = bool(task.session_id)
        while True:
            now = datetime.now(UTC)
            spec = self._spec_for(task)
            limit_minutes = spec.max_minutes or self.settings.limits.max_task_minutes
            deadline = (task.started_at or now).astimezone(UTC) + timedelta(minutes=limit_minutes)
            if spec.token_budget > 0:
                totals = await self.usage.task_totals(task.id)
                if totals.total_tokens >= spec.token_budget:
                    await self._block_task(
                        task.id,
                        f"Исчерпан token_budget задачи ({totals.total_tokens}/{spec.token_budget}).",
                    )
                    return
            if now >= deadline:
                await self._fail_task(
                    task_id, "Превышен максимальный лимит времени выполнения задачи."
                )
                return
            attempt += 1
            if attempt > self.settings.limits.max_iterations:
                await self._block_task(task_id, "Превышен максимальный лимит итераций исполнителя.")
                return
            task = await self.tasks.update_fields(task.id, attempts=task.attempts + 1)
            state_dir = self.settings.task_logs_dir / f"task-{task.id:04d}" / f"attempt-{attempt}"
            timeout_seconds = max(30.0, (deadline - now).total_seconds())
            await self.usage.ensure_kimi_budget(task.id)
            worker_prompt = self._worker_prompt(
                task=task,
                project=project,
                manual_instruction=manual_instruction,
            )
            await self.tasks.add_agent_message(
                task.id, "supervisor", self.redactor.redact(worker_prompt)
            )
            result = await self.workers.run(
                task=task,
                project=project,
                prompt=worker_prompt,
                attempt=attempt,
                resume=resume,
                timeout_seconds=timeout_seconds,
            )
            if result.usage.total_tokens or result.usage.requests:
                await self.usage.record(
                    provider="deepseek",
                    model=self.settings.deepseek_model,
                    usage=result.usage,
                    task_id=task.id,
                    project_id=project.id,
                )
            task = await self.tasks.update_fields(
                task.id,
                session_id=result.session_id,
                logs_path=str(state_dir),
            )
            await self.tasks.add_agent_message(
                task.id, "worker", self.redactor.redact(result.final_response or result.error or "")
            )
            if task.cancel_requested:
                await self._try_transition(task.id, TaskStatus.CANCELLED)
                await self.notifier.send_text(
                    self._chat_for_task(task),
                    "Задача остановлена. Незавершённые изменения сохранены в worktree.",
                )
                return
            await self._try_transition(task.id, TaskStatus.VALIDATING)
            evidence = await self.workspace.collect_evidence(
                task=task,
                project=project,
                worktree=Path(task.worktree_path or ""),
                dsh_home=Path(task.dsh_home or self.settings.dsh_homes_dir / f"task-{task.id:04d}"),
                state_dir=state_dir,
                worker=result,
                check_timeout_seconds=max(30.0, (deadline - datetime.now(UTC)).total_seconds()),
                diff_path=state_dir / "diff.patch",
            )
            head_commit = await self.workspace.commit_changes(
                task=task, worktree=Path(task.worktree_path or ""), changed=evidence.has_changes
            )
            evidence = evidence.model_copy(update={"head_commit": head_commit})
            task = await self.tasks.update_fields(
                task.id,
                head_commit=head_commit,
                diff_path=str(state_dir / "diff.patch"),
                evidence_json=evidence.model_dump_json(),
            )
            await self.tasks.add_event(
                task.id,
                "worker-finished",
                f"Исполнитель завершил попытку {attempt}",
                {
                    "finish_reason": result.finish_reason,
                    "changed_files": evidence.changed_files,
                    "checks": [check.model_dump() for check in evidence.checks],
                },
            )
            await self.notifier.send_text(
                self._chat_for_task(task),
                f"Задача #{task.id:03d}: исполнитель завершил работу. Провожу проверку diff и результатов.",
            )
            spec = self._spec_for(task)
            precheck = ReviewGate.precheck(evidence, spec, worktree=Path(task.worktree_path or ""))
            if precheck is None:
                await self._try_transition(task.id, TaskStatus.REVIEWING)
                await self.usage.ensure_kimi_budget(task.id)
                review, review_usage = await self.supervisor.review(
                    project_block=self._project_block(project),
                    spec=spec,
                    evidence=evidence,
                    project_context=project_context,
                )
                await self._record_kimi_usage(review_usage, task_id=task.id, project_id=project.id)
            else:
                review = precheck
            review = ReviewGate.enforce(
                review, evidence, spec, worktree=Path(task.worktree_path or "")
            )
            task = await self.tasks.update_fields(
                task.id,
                review_json=review.model_dump_json(),
            )
            await self.tasks.add_event(
                task.id,
                "review",
                f"Ревью: {review.verdict.value}",
                {
                    "summary": review.summary,
                    "issues": [item.model_dump() for item in review.issues],
                },
            )
            if review.verdict is ReviewVerdict.APPROVED:
                await self._finish_for_acceptance(
                    task.id, project, spec_evidence=evidence, review=review
                )
                return
            if review.verdict is ReviewVerdict.BLOCKED:
                await self._block_task(task.id, review.summary, review=review)
                return
            if review.verdict is ReviewVerdict.APPROVAL_REQUIRED:
                await self._await_required_approval(task.id, review)
                return
            if review.verdict is ReviewVerdict.FAILED:
                await self._fail_task(
                    task.id, "Ревьюер отклонил работу: " + review.summary, review=review
                )
                return
            exhausted = (
                task.attempts >= self._max_attempts(task) or task.rework_count >= task.max_rework
            )
            if exhausted:
                critical_or_major = any(
                    issue.severity.value in {"critical", "major"} for issue in review.issues
                )
                if review.issues and not critical_or_major:
                    await self._partial_task(task.id, review)
                    return
                await self._block_task(
                    task.id,
                    "Исчерпан лимит доработок/попыток. Требуется решение владельца.\n"
                    + review.summary,
                    review=review,
                )
                return
            new_hash = self._progress_hash(evidence, review)
            same_state = bool(task.progress_hash) and new_hash == task.progress_hash
            stalled = task.stalled_count + 1 if same_state else 0
            if stalled >= 2:
                await self._block_task(
                    task.id,
                    "Нет прогресса: повторная попытка дала то же состояние и те же замечания.",
                    review=review,
                )
                return
            task = await self.tasks.update_fields(
                task.id,
                rework_count=task.rework_count + 1,
                stalled_count=stalled,
                progress_hash=new_hash,
            )
            await self._try_transition(task.id, TaskStatus.REWORK)
            await self._try_transition(task.id, TaskStatus.QUEUED)
            task = await self.tasks.get(task.id)
            await self.tasks.add_event(
                task.id,
                "rework",
                f"Доработка {task.rework_count}/{task.max_rework}",
                {"required_changes": review.required_changes},
            )
            await self.notifier.send_text(
                self._chat_for_task(task),
                f"Задача #{task.id:03d}: отправлена на доработку "
                f"({task.rework_count}/{task.max_rework}).\n"
                + "\n".join(
                    f"• {item}"
                    for item in (
                        review.required_changes
                        or [issue.recommendation or issue.evidence for issue in review.issues]
                    )[:5]
                ),
            )
            await self._try_transition(task.id, TaskStatus.RUNNING)
            task = await self.tasks.get(task.id)
            resume = True
            manual_instruction = None

    def _max_attempts(self, task: TaskRecord) -> int:
        spec = self._spec_for(task)
        return spec.max_attempts or self.settings.limits.max_iterations

    @staticmethod
    def _progress_hash(evidence: Evidence, review: ReviewResult) -> str:
        digest = hashlib.sha256()
        digest.update(evidence.diff.encode())
        digest.update(evidence.head_commit.encode())
        for name in sorted(evidence.changed_files):
            digest.update(name.encode())
        for check in evidence.checks:
            digest.update(f"{check.name}:{check.exit_code}:{check.timed_out}".encode())
        digest.update(review.verdict.value.encode())
        for issue in review.issues:
            digest.update(f"{issue.criterion}:{issue.evidence}".encode())
        return digest.hexdigest()

    async def _retry_wait(self, task_id: int, reason: str) -> None:
        task = await self.tasks.get_optional(task_id)
        if task is None or task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            return
        try:
            await self.tasks.transition(task.id, TaskStatus.RETRY_WAIT)
        except (TransitionError, TaskError):
            await self._pause_task(task_id, reason)
            return
        safe = self.redactor.redact(reason)[:4000]
        await self.tasks.update_fields(task.id, error=safe)
        await self.tasks.add_event(task.id, "retry_wait", safe, agent="orchestrator")
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d}: временная ошибка API; ожидаю повтора.\n{safe[:1000]}",
            [[Button("Повторить", f"res:{task.id}"), Button("Отменить", f"stop:{task.id}")]],
        )

    async def _partial_task(self, task_id: int, review: ReviewResult) -> None:
        task = await self.tasks.get(task_id)
        try:
            await self.tasks.transition(task.id, TaskStatus.PARTIALLY_COMPLETED)
        except (TransitionError, TaskError):
            await self._block_task(task_id, review.summary, review=review)
            return
        task = await self.tasks.update_fields(task.id, review_json=review.model_dump_json())
        report = await self._final_report(task, None, TaskStatus.PARTIALLY_COMPLETED)
        await self.tasks.update_fields(task.id, report=report)
        await self.notifier.send_text(
            self._chat_for_task(task),
            report,
            [[Button("Повторить", f"res:{task.id}"), Button("Откатить", f"rb:{task.id}")]],
        )

    async def _block_task(
        self, task_id: int, reason: str, *, review: ReviewResult | None = None
    ) -> None:
        task = await self.tasks.get_optional(task_id)
        if task is None or task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            return
        try:
            await self.tasks.transition(task.id, TaskStatus.BLOCKED)
        except (TransitionError, TaskError):
            logger.warning("could not transition task #%s to blocked", task_id)
        safe_reason = self.redactor.redact(reason)[:8000]
        task = await self.tasks.update_fields(
            task.id,
            blocked_reason=safe_reason,
            error=safe_reason,
            review_json=review.model_dump_json() if review else None,
        )
        await self.tasks.add_event(task.id, "blocked", safe_reason, agent="orchestrator")
        report = await self._final_report(task, None, TaskStatus.BLOCKED)
        await self.tasks.update_fields(task.id, report=report)
        await self.notifier.send_text(
            self._chat_for_task(task),
            report,
            [
                [Button("Продолжить", f"res:{task.id}"), Button("Показать diff", f"dif:{task.id}")],
                [Button("Логи", f"log:{task.id}"), Button("Откатить", f"rb:{task.id}")],
            ],
        )

    async def _await_required_approval(self, task_id: int, review: ReviewResult) -> None:
        task = await self.tasks.get(task_id)
        await self.tasks.update_fields(task.id, review_json=review.model_dump_json())
        await self._request_task_approval(task, review.summary)

    async def _finish_for_acceptance(
        self,
        task_id: int,
        project: ProjectRecord,
        *,
        spec_evidence: Evidence,
        review: ReviewResult,
    ) -> None:
        task = await self.tasks.get(task_id)
        await self._try_transition(task.id, TaskStatus.WAITING_APPROVAL)
        task = await self.tasks.get(task.id)
        usage_totals = await self.usage.by_provider(task_id=task.id)
        task = await self.tasks.update_fields(task.id, review_json=review.model_dump_json())
        report = build_report(
            task=task,
            project_name=project.name,
            spec=self._spec_for(task),
            evidence=spec_evidence,
            review=review,
            usage_service=self.usage,
            usage_totals=usage_totals,
            final_status=TaskStatus.WAITING_APPROVAL,
        )
        await self.tasks.update_fields(task.id, report=report)
        await self.tasks.add_event(task.id, "waiting_approval", "Ожидает решения владельца")
        await self._maybe_usage_warning(task_id)
        await self._send_report_with_buttons(task, report)
        await self.conversations.add_message(
            task.conversation_id,
            "assistant",
            report,
            meta={
                "task_id": task.id,
                "kind": "report",
                "status": TaskStatus.WAITING_APPROVAL.value,
            },
        )

    @_locked_action
    async def accept_task(self, task_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status is not TaskStatus.WAITING_APPROVAL:
            await self.notifier.send_text(chat_id, f"Задача #{task.id:03d} не ожидает принятия.")
            return
        project = await self.projects.get(task.project_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Проект задачи не найден.")
            return
        if await self.workspace.is_applied(task, project):
            warnings = await self.workspace.cleanup(task, project, remove_branch=True, strict=False)
            await self._try_transition(task.id, TaskStatus.COMPLETED)
            task = await self.tasks.get(task.id)
            await self.tasks.add_event(
                task.id,
                "merged-recovered",
                "Изменения уже присутствуют в базовой ветке; задача завершена идемпотентно.",
            )
            report = await self._final_report(task, project, TaskStatus.COMPLETED)
            report += "\n\nПримечание: merge уже был выполнен; повторная операция не запускалась."
            await self.tasks.update_fields(task.id, report=report)
            await self.notifier.send_text(chat_id, report)
            for warning in warnings:
                logger.warning("post-merge cleanup: %s", warning)
            return
        try:
            self.permissions.require_allowed(Action.APPLY_MERGE)
            approval_id = await self.approvals.grant_once(
                action=Action.APPLY_MERGE,
                payload={"task_id": task.id},
                decided_by=user_id,
                task_id=task.id,
                ttl_seconds=600,
            )
            await self.approvals.consume(approval_id, user_id=user_id, action=Action.APPLY_MERGE)
            new_head = await self.workspace.apply(task, project)
        except (ApprovalError, PermissionDenied, WorkspaceError) as exc:
            await self.notifier.send_text(
                chat_id,
                f"Не удалось применить изменения задачи #{task.id:03d}: {self.redactor.redact(str(exc))}",
            )
            return
        await self._try_transition(task.id, TaskStatus.COMPLETED)
        task = await self.tasks.update_fields(task.id, head_commit=new_head)
        await self.tasks.add_event(task.id, "merged", f"Изменения слиты в {project.base_branch}")
        report = await self._final_report(task, project, TaskStatus.COMPLETED)
        await self.tasks.update_fields(task.id, report=report)
        buttons = (
            [[Button("Опубликовать в Git", f"push:{task.id}")]] if project.remote_url else None
        )
        await self.notifier.send_text(chat_id, report, buttons)
        await self.conversations.add_message(
            task.conversation_id,
            "assistant",
            report,
            meta={"task_id": task.id, "kind": "completed"},
        )

    @_locked_action
    async def rework_task(self, task_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status is not TaskStatus.WAITING_APPROVAL:
            await self.notifier.send_text(chat_id, f"Задача #{task.id:03d} не ожидает доработки.")
            return
        review = task.review
        instruction = ""
        if review and review.required_changes:
            instruction = "\n".join(f"- {item}" for item in review.required_changes)
        elif review:
            instruction = (
                "\n".join(
                    f"- {issue.criterion}: {issue.recommendation or issue.evidence}"
                    for issue in review.issues
                )
                or review.summary
            )
        # A manual rework is an explicit owner decision; allow one extra cycle.
        if task.rework_count >= task.max_rework:
            await self.tasks.update_fields(task.id, max_rework=task.rework_count + 1)
        await self.tasks.update_fields(task.id, rework_count=task.rework_count + 1)
        await self._try_transition(task.id, TaskStatus.REWORK)
        await self._try_transition(task.id, TaskStatus.QUEUED)
        await self.tasks.reset_started_at(task.id)
        await self.notifier.send_text(
            chat_id,
            f"Задача #{task.id:03d} возвращена на доработку. Исполнитель получит замечания ревью.",
        )
        self._spawn_job(task.id, plan_if_needed=False, manual_instruction=instruction or None)

    @_locked_action
    async def resume_task(self, task_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status not in {
            TaskStatus.PAUSED,
            TaskStatus.INTERRUPTED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.WAITING_INPUT,
        }:
            await self.notifier.send_text(
                chat_id,
                f"Задача #{task.id:03d} не может быть продолжена из статуса {task.status.value}.",
            )
            return
        other_active = [
            item for item in await self.tasks.active(task.project_id) if item.id != task.id
        ]
        if other_active:
            await self.notifier.send_text(
                chat_id,
                "Нельзя продолжить задачу: в проекте есть другая активная задача "
                + ", ".join(f"#{item.id:03d}" for item in other_active),
            )
            return
        if task.status is TaskStatus.FAILED and task.rework_count >= task.max_rework:
            await self.tasks.update_fields(task.id, max_rework=task.rework_count + 1)
        await self._try_transition(task.id, TaskStatus.QUEUED)
        await self.tasks.update_fields(task.id, cancel_requested=False)
        await self.tasks.reset_started_at(task.id)
        self._pause_requests.discard(task.id)
        self.workers.clear_cancel(task.id)
        await self.tasks.add_event(task.id, "resumed", "Задача продолжена владельцем")
        await self.notifier.send_text(chat_id, f"Задача #{task.id:03d} продолжается.")
        self._spawn_job(task.id, plan_if_needed=task.spec is None, manual_instruction=None)

    @_locked_action
    async def pause_task(self, task_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }:
            await self.notifier.send_text(chat_id, f"Задача #{task.id:03d} уже завершена.")
            return
        if task.status is TaskStatus.PAUSED:
            await self.notifier.send_text(chat_id, f"Задача #{task.id:03d} уже приостановлена.")
            return
        if task.status in {
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_INPUT,
            TaskStatus.RETRY_WAIT,
        }:
            await self.notifier.send_text(
                chat_id, f"Задача #{task.id:03d} не выполняется ({task.status.value})."
            )
            return
        self._pause_requests.add(task.id)
        job = self._jobs.get(task.id)
        if job is not None and not job.done():
            await self.workers.cancel(task.id)
            await self.scheduler.cancel(task.id)
            job.cancel()
            await self.notifier.send_text(chat_id, f"Приостанавливаю задачу #{task.id:03d}…")
            return
        await self.workers.cancel(task.id)
        await self._try_transition(task.id, TaskStatus.PAUSED)
        await self.notifier.send_text(
            chat_id,
            f"Задача #{task.id:03d} приостановлена. /resume — продолжить.",
            [[Button("Продолжить", f"res:{task.id}"), Button("Отменить", f"stop:{task.id}")]],
        )

    @_locked_action
    async def stop_task(self, task_id: int, chat_id: int) -> None:
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
            await self.notifier.send_text(
                chat_id, f"Задача #{task.id:03d} уже завершена ({task.status.value})."
            )
            return
        await self.tasks.request_cancel(task.id)
        job = self._jobs.get(task.id)
        if (
            task.status in {TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED, TaskStatus.INTERRUPTED}
            or job is None
        ):
            await self.workers.cancel(task.id)
            await self._try_transition(task.id, TaskStatus.CANCELLED)
            await self.notifier.send_text(
                chat_id,
                f"Задача #{task.id:03d} остановлена. Worktree сохранён; изменения можно посмотреть через /diff.",
            )
            return
        await self.workers.cancel(task.id)
        await self.scheduler.cancel(task.id)
        if job is not None and not job.done():
            job.cancel()
        await self.notifier.send_text(chat_id, f"Останавливаю задачу #{task.id:03d}…")

    async def github_info(self, chat_id: int, owner: str, repo: str) -> None:
        if self.github is None:
            await self.notifier.send_text(chat_id, "GITHUB_TOKEN не настроен.")
            return
        try:
            info = await self.github.repository_info(owner, repo)
        except GitHubError as exc:
            await self.notifier.send_text(chat_id, f"GitHub: {exc}")
            return
        if info is None:
            await self.notifier.send_text(chat_id, f"Репозиторий {owner}/{repo} не найден.")
            return
        await self.notifier.send_text(
            chat_id,
            f"GitHub {owner}/{repo}:\n"
            f"private={info.get('private')} default_branch={info.get('default_branch')}\n"
            f"url={info.get('html_url')}",
        )

    async def request_github_create(
        self, name: str, private: bool, user_id: int, chat_id: int
    ) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        if self.github is None:
            await self.notifier.send_text(chat_id, "GITHUB_TOKEN не настроен.")
            return
        approval_id = await self.approvals.request(
            action=Action.GITHUB_REPOSITORY_CREATE,
            payload={"name": name, "private": private},
            created_by=user_id,
            ttl_seconds=600,
        )
        await self.notifier.send_text(
            chat_id,
            f"Создание GitHub-репозитория {name}.\n"
            f"Видимость: {'private' if private else 'public'}\n"
            "Репозиторий будет создан от имени владельца токена. Подтвердите.",
            [
                [Button("Разрешить один раз", f"apv:{approval_id}")],
                [Button("Отклонить", f"rej:{approval_id}")],
            ],
        )

    @_locked_action
    async def request_push(self, task_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status is not TaskStatus.COMPLETED:
            await self.notifier.send_text(
                chat_id,
                f"Публикация доступна только для завершённой задачи; сейчас {task.status.value}.",
            )
            return
        project = await self.projects.get(task.project_id)
        if project is None or not project.remote_url:
            await self.notifier.send_text(
                chat_id, "У проекта не задан remote. Укажите: /project remote <repo-url>."
            )
            return
        try:
            actual_remote = await remote_url(Path(project.path), "origin")
            validate_remote_url(actual_remote)
        except (GitOpsError, ProjectError) as exc:
            await self.notifier.send_text(chat_id, f"Remote не проверен: {exc}")
            return
        approval_id = await self.approvals.request(
            action=Action.GIT_PUSH,
            payload={
                "task_id": task.id,
                "remote": "origin",
                "branch": project.base_branch,
                "remote_url": self.redactor.redact(project.remote_url),
            },
            created_by=user_id,
            task_id=task.id,
            ttl_seconds=600,
        )
        await self.notifier.send_text(
            chat_id,
            f"Задача #{task.id:03d}: публикация в Git.\n"
            f"Проект: {project.name}\n"
            f"Операция: git push origin {project.base_branch}\n"
            f"Remote: {project.remote_url}\n"
            "Force-push не используется. Подтвердите операцию.",
            [
                [
                    Button("Разрешить один раз", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
                [Button("Показать diff", f"dif:{task.id}"), Button("Отмена", f"rej:{approval_id}")],
            ],
        )

    @_locked_action
    async def request_rollback(self, task_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        task = await self.tasks.get(task_id)
        if task.status is TaskStatus.COMPLETED:
            await self.notifier.send_text(
                chat_id,
                "Задача уже слита в основную ветку. Автоматический откат Phase 1 не выполняет; "
                "используйте git revert вручную.",
            )
            return
        if task.status not in {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.FAILED,
            TaskStatus.PAUSED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }:
            await self.notifier.send_text(chat_id, "Сначала остановите задачу (/stop).")
            return
        approval_id = await self.approvals.request(
            action=Action.ROLLBACK,
            payload={"task_id": task.id},
            created_by=user_id,
            task_id=task.id,
            ttl_seconds=600,
        )
        await self.notifier.send_text(
            chat_id,
            f"Откат задачи #{task.id:03d} удалит worktree и ветку. "
            "Патч будет сохранён локально. Подтвердите действие.",
            [
                [
                    Button("Подтвердить откат", f"apv:{approval_id}"),
                    Button("Отмена", f"rej:{approval_id}"),
                ]
            ],
        )

    async def approve_action(self, approval_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        try:
            approval = await self.approvals.approve_pending(approval_id, user_id=user_id)
            payload = await self.approvals.consume(approval_id, user_id=user_id)
        except (ApprovalError, PermissionDenied) as exc:
            await self.notifier.send_text(chat_id, f"Подтверждение отклонено: {exc}")
            return
        action = Action(approval["action"])
        if action is Action.ROLLBACK:
            task_id = int(payload["task_id"])
            task = await self.tasks.get(task_id)
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.PARTIALLY_COMPLETED,
                TaskStatus.CANCELLED,
            }:
                await self.notifier.send_text(
                    chat_id,
                    f"Задача #{task.id:03d} уже завершена ({task.status.value}); "
                    "откат по старому подтверждению не выполняется.",
                )
                return
            project = await self.projects.get(task.project_id)
            if project is None:
                await self.notifier.send_text(chat_id, "Проект задачи не найден.")
                return
            await self.workers.cancel(task_id)
            try:
                patch_path = await self.workspace.rollback(task, project)
            except WorkspaceError as exc:
                await self.notifier.send_text(chat_id, f"Откат не выполнен: {exc}")
                return
            await self._try_transition(task.id, TaskStatus.CANCELLED)
            await self.tasks.add_event(task.id, "rollback", f"Патч сохранён: {patch_path}")
            await self.notifier.send_text(
                chat_id,
                f"Задача #{task.id:03d} откатана. Патч сохранён: {patch_path}",
            )
        elif action is Action.GITHUB_REPOSITORY_CREATE:
            if self.github is None:
                await self.notifier.send_text(chat_id, "GITHUB_TOKEN не настроен.")
                return
            try:
                repo = await self.github.create_repository(
                    str(payload["name"]), private=bool(payload.get("private", True))
                )
            except GitHubError as exc:
                await self.notifier.send_text(chat_id, f"GitHub: {exc}")
                return
            await self.notifier.send_text(
                chat_id,
                "GitHub-репозиторий создан: "
                f"{repo.get('full_name') or payload['name']} ({repo.get('html_url')}).\n"
                "Remote проекта не изменялся; при необходимости задайте /project remote <url>.",
            )
        elif action is Action.GIT_PUSH:
            task = await self.tasks.get(int(payload["task_id"]))
            project = await self.projects.get(task.project_id)
            if project is None or task.status is not TaskStatus.COMPLETED:
                await self.notifier.send_text(
                    chat_id, "Публикация отменена: задача/проект не в нужном состоянии."
                )
                return
            branch = str(payload.get("branch") or project.base_branch)
            remote = str(payload.get("remote") or "origin")
            try:
                actual = await remote_url(Path(project.path), remote)
                validate_remote_url(actual)
                sha = await push_branch(Path(project.path), remote, branch)
                _local, remote_sha = await verify_remote_branch(Path(project.path), remote, branch)
            except (GitOpsError, ProjectError) as exc:
                await self.notifier.send_text(chat_id, f"Публикация не выполнена: {exc}")
                return
            await self.tasks.add_event(
                task.id,
                "pushed",
                f"{remote}/{branch} -> {sha[:12]}; remote={remote_sha[:12]}",
                agent="orchestrator",
            )
            await self.notifier.send_text(
                chat_id,
                f"Задача #{task.id:03d}: опубликовано.\n"
                f"{remote}/{branch}: {sha[:12]}\n"
                f"Удалённая вершина подтверждена: {str(remote_sha)[:12]}",
            )
        elif action is Action.APPLY_MERGE:
            await self.accept_task(int(payload["task_id"]), user_id, chat_id)
        elif action is Action.TASK_APPROVAL:
            target = await self.tasks.get(int(payload["task_id"]))
            if target.status is not TaskStatus.WAITING_APPROVAL:
                await self.notifier.send_text(
                    chat_id,
                    f"Задача #{target.id:03d} уже не ожидает подтверждения ({target.status.value}).",
                )
                return
            if not await self._try_transition(target.id, TaskStatus.QUEUED):
                await self.notifier.send_text(chat_id, "Не удалось перевести задачу в очередь.")
                return
            await self.tasks.reset_started_at(target.id)
            await self.tasks.update_fields(target.id, cancel_requested=False)
            await self.notifier.send_text(
                chat_id, f"Задача #{target.id:03d}: подтверждение получено, продолжаю."
            )
            self._spawn_job(target.id, plan_if_needed=False, manual_instruction=None)
        else:
            await self.notifier.send_text(
                chat_id, f"Действие {action.value} пока не поддерживается."
            )

    async def reject_action(self, approval_id: int, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        changed = await self.approvals.reject_pending(approval_id, user_id=user_id)
        await self.notifier.send_text(
            chat_id,
            "Подтверждение отклонено."
            if changed
            else "Подтверждение не найдено или уже обработано.",
        )

    async def approve_latest(self, user_id: int, chat_id: int, task_id: int | None = None) -> None:
        pending = await self.approvals.latest_pending(user_id=user_id, task_id=task_id)
        if pending is None:
            await self.notifier.send_text(chat_id, "Нет ожидающих подтверждений.")
            return
        await self.approve_action(int(pending["id"]), user_id, chat_id)

    async def reject_latest(self, user_id: int, chat_id: int, task_id: int | None = None) -> None:
        pending = await self.approvals.latest_pending(user_id=user_id, task_id=task_id)
        if pending is None:
            await self.notifier.send_text(chat_id, "Нет ожидающих подтверждений.")
            return
        await self.reject_action(int(pending["id"]), user_id, chat_id)

    # ------------------------------------------------------------------
    # task failure / pause / cancel helpers
    # ------------------------------------------------------------------

    async def _pause_task(self, task_id: int, reason: str) -> None:
        task = await self.tasks.get_optional(task_id)
        if task is None:
            return
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
            return
        try:
            ensure_transition(task.status, TaskStatus.PAUSED)
            await self.tasks.transition(task.id, TaskStatus.PAUSED)
        except (TransitionError, TaskError):
            await self._fail_task(task_id, reason)
            return
        await self.tasks.update_fields(task.id, error=self.redactor.redact(reason)[:8000])
        await self.tasks.add_event(task.id, "paused", self.redactor.redact(reason)[:2000])
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d} приостановлена.\n{self.redactor.redact(reason)}\n"
            "После устранения лимита/проблемы: /resume",
            [[Button("Продолжить", f"res:{task.id}"), Button("Откатить", f"rb:{task.id}")]],
        )

    async def _fail_task(
        self,
        task_id: int,
        reason: str,
        *,
        review: ReviewResult | None = None,
    ) -> None:
        task = await self.tasks.get_optional(task_id)
        if task is None:
            return
        if task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.CANCELLED,
        }:
            return
        if task.status is TaskStatus.WAITING_APPROVAL:
            await self._block_task(task_id, reason, review=review)
            return
        if task.status is not TaskStatus.FAILED:
            try:
                ensure_transition(task.status, TaskStatus.FAILED)
                await self.tasks.transition(task.id, TaskStatus.FAILED)
            except (TransitionError, TaskError) as exc:
                logger.error("cannot mark task #%s failed: %s", task_id, exc)
                if task.status not in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
                    await self._block_task(task_id, reason, review=review)
                return
        safe_reason = self.redactor.redact(reason)[:8000]
        task = await self.tasks.update_fields(task.id, error=safe_reason)
        await self.tasks.add_event(task.id, "failed", safe_reason[:2000])
        if review is not None:
            await self.tasks.update_fields(task.id, review_json=review.model_dump_json())
        report = await self._final_report(task, None, TaskStatus.FAILED)
        await self.tasks.update_fields(task.id, report=report)
        await self._maybe_usage_warning(task_id)
        await self.notifier.send_text(
            self._chat_for_task(task),
            report,
            [
                [Button("Показать diff", f"dif:{task.id}"), Button("Логи", f"log:{task.id}")],
                [Button("Повторить", f"res:{task.id}"), Button("Откатить", f"rb:{task.id}")],
            ],
        )

    async def _fail_without_project(self, task: TaskRecord, reason: str) -> None:
        await self._fail_task(task.id, reason)

    async def _handle_cancel(self, task_id: int) -> None:
        task = await self.tasks.get_optional(task_id)
        if task is None:
            return
        if task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }:
            return
        if task_id in self._pause_requests:
            target = TaskStatus.PAUSED
        else:
            target = TaskStatus.INTERRUPTED if self._stopping else TaskStatus.CANCELLED
        try:
            ensure_transition(task.status, target)
            await self.tasks.transition(task.id, target)
        except (TransitionError, TaskError):
            logger.warning("could not record cancellation for task #%s", task.id)
        await self.tasks.add_event(
            task.id,
            "cancelled" if target is TaskStatus.CANCELLED else "interrupted",
            "Задача остановлена",
        )
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d} остановлена. Worktree и незавершённые изменения сохранены.",
            [[Button("Показать diff", f"dif:{task.id}"), Button("Откатить", f"rb:{task.id}")]],
        )

    async def _try_transition(self, task_id: int, status: TaskStatus) -> bool:
        try:
            await self.tasks.transition(task_id, status)
            return True
        except (TransitionError, TaskError) as exc:
            logger.debug("transition for task #%s to %s skipped: %s", task_id, status.value, exc)
            return False

    # ------------------------------------------------------------------
    # reporting / commands helpers
    # ------------------------------------------------------------------

    async def _final_report(
        self, task: TaskRecord, project: ProjectRecord | None, status: TaskStatus
    ) -> str:
        project = project or await self.projects.get(task.project_id)
        if project is None:
            return f"Задача #{task.id:03d}: проект не найден. Статус: {status.value}."
        spec = self._spec_for(task)
        evidence = self._load_evidence(task)
        fallback_verdict = {
            TaskStatus.FAILED: ReviewVerdict.FAILED,
            TaskStatus.BLOCKED: ReviewVerdict.BLOCKED,
            TaskStatus.PARTIALLY_COMPLETED: ReviewVerdict.REWORK_REQUIRED,
        }.get(status, ReviewVerdict.APPROVED)
        review = task.review or ReviewResult(
            verdict=fallback_verdict,
            summary=task.error or "Ревью не проводилось.",
        )
        if evidence is None:
            return (
                f"Задача #{task.id:03d} — {status.value}\n"
                f"Проект: {project.name}\n"
                f"Ошибка: {task.error or 'нет данных'}\n"
                f"Стоимость: данные об использовании — /usage"
            )
        usage_totals = await self.usage.by_provider(task_id=task.id)
        return build_report(
            task=task,
            project_name=project.name,
            spec=spec,
            evidence=evidence,
            review=review,
            usage_service=self.usage,
            usage_totals=usage_totals,
            final_status=status,
        )

    @staticmethod
    def _spec_for(task: TaskRecord) -> TaskSpec:
        return task.spec or TaskSpec(
            title=task.title[:120] or "Задача",
            goal=f"Выполнить задачу: {task.title}"[:8000],
            acceptance_criteria=["Задача выполнена и подтверждена фактическими результатами."],
        )

    @staticmethod
    def _load_evidence(task: TaskRecord) -> Evidence | None:
        if not task.evidence_json:
            return None
        try:
            return Evidence.model_validate_json(task.evidence_json)
        except Exception:
            return None

    async def _send_report_with_buttons(self, task: TaskRecord, report: str) -> None:
        chat_id = self._chat_for_task(task)
        buttons = [
            [
                Button("Принять изменения", f"acc:{task.id}"),
                Button("На доработку", f"rew:{task.id}"),
            ],
            [
                Button("Показать diff", f"dif:{task.id}"),
                Button("Логи", f"log:{task.id}"),
                Button("Откатить", f"rb:{task.id}"),
            ],
        ]
        await self.notifier.send_text(chat_id, report, buttons)

    def _worker_prompt(
        self,
        *,
        task: TaskRecord,
        project: ProjectRecord,
        manual_instruction: str | None,
    ) -> str:
        spec = task.spec
        if spec is None:
            raise OrchestratorError("task has no spec")
        review = task.review
        previous = ""
        if review is not None and task.rework_count > 0:
            findings = review.required_changes or [
                f"{issue.criterion}: {issue.recommendation or issue.evidence}"
                for issue in review.issues
            ]
            previous = (
                "\n## Замечания предыдущего ревью\n"
                + "\n".join(f"- {item}" for item in findings)[:8000]
                + "\n"
            )
        if manual_instruction:
            previous += f"\n## Дополнительное указание владельца\n{manual_instruction}\n"
        return (
            f"# Задача #{task.id:03d}: {spec.title}\n\n"
            f"Проект: {project.name}\n"
            f"Рабочий каталог: /workspace (изолированный git worktree, ветка {task.branch})\n"
            f"Базовая ветка: {project.base_branch}\n\n"
            f"## Техническое задание\n{json.dumps(spec.model_dump(), ensure_ascii=False, indent=2)}\n\n"
            f"## Инструкции проекта\n{project.instructions or '(нет)'}\n"
            f"## Дополнительные read-only пути\n"
            f"{project.extra_read_paths_json if project.extra_read_paths_json != '[]' else '(нет)'}\n"
            f"{previous}\n"
            "## Правила\n"
            "- Работай только в /workspace. Не читай и не изменяй данные вне него.\n"
            "- Не делай commit, merge, push. Оркестратор соберёт diff сам.\n"
            "- Запусти доступные проверки и приведи точные команды и exit codes.\n"
            "- Верни фактический отчёт; не выдумывай результаты.\n"
            "- Если задача требует запрещённого действия, остановись и объясни.\n"
        )

    def _chat_for_task(self, task: TaskRecord) -> int:
        # chat_id is the conversation owner's chat. All usage is owner-only.
        return self.settings.owner_id

    async def _maybe_usage_warning(self, task_id: int) -> None:
        warning = await self.usage.warning()
        if not warning or warning == self._last_usage_warning:
            return
        self._last_usage_warning = warning
        task = await self.tasks.get_optional(task_id)
        if task is not None:
            await self.notifier.send_text(self._chat_for_task(task), "⚠️ " + warning)

    async def _record_kimi_usage(
        self, usage: Any, *, task_id: int | None, project_id: int | None
    ) -> None:
        await self.usage.record(
            provider="kimi",
            model=self.settings.kimi.model,
            usage=usage,
            task_id=task_id,
            project_id=project_id,
        )

    async def _notify_quota(self, chat_id: int, exc: Exception) -> None:
        await self.notifier.send_text(
            chat_id,
            "Лимит/квота Kimi исчерпаны. Новые запросы приостановлены; проверьте лимиты аккаунта.\n"
            + self.redactor.redact(str(exc))[:1000],
        )

    def _friendly_error(self, exc: Exception) -> str:
        text = self.redactor.redact(str(exc))[:1500]
        if isinstance(exc, LimitExceeded):
            return "Лимит: " + text
        if isinstance(exc, KimiError):
            return "Ошибка Kimi: " + text
        return "Ошибка: " + text

    async def _send_long(self, chat_id: int, text: str) -> None:
        limit = self.settings.telegram.max_message_chars
        remaining = text
        while remaining:
            chunk = remaining[:limit]
            if len(remaining) > limit:
                split = chunk.rfind("\n")
                if split > limit // 2:
                    chunk = chunk[:split]
            await self.notifier.send_text(chat_id, chunk)
            remaining = remaining[len(chunk) :].lstrip("\n")

    def _assert_owner(self, user_id: int) -> None:
        if user_id != self.settings.owner_id:
            raise PermissionDenied("управление разрешено только владельцу")

    def _assert_owner_chat(self, chat_id: int) -> None:
        if chat_id != self.settings.owner_id:
            raise PermissionDenied("управление разрешено только в личном чате владельца")

    def _schedule_summary(self, conversation_id: int, project_id: int) -> None:
        existing = self._summaries.get(conversation_id)
        if existing is not None and not existing.done():
            return

        async def summarize(prompt: str) -> str:
            await self.usage.ensure_kimi_budget(0)
            text, usage = await self.supervisor.summarize(prompt)
            await self._record_kimi_usage(usage, task_id=None, project_id=project_id)
            return text

        async def run() -> None:
            try:
                await self.conversations.maybe_summarize(conversation_id, summarize)
            except Exception as exc:
                logger.warning("conversation summary failed: %s", exc)

        job = asyncio.create_task(run(), name=f"summary-{conversation_id}")
        self._summaries[conversation_id] = job

        def _done(completed: asyncio.Task[Any]) -> None:
            if self._summaries.get(conversation_id) is completed:
                self._summaries.pop(conversation_id, None)

        job.add_done_callback(_done)

    # ------------------------------------------------------------------
    # formatting for commands
    # ------------------------------------------------------------------

    async def create_task_from_text(self, chat_id: int, user_id: int, text: str) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        project = await self._current_project(chat_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Сначала выберите или создайте проект.")
            return
        conversation = await self.conversations.get_or_create_conversation(chat_id, project.id)
        await self._begin_task(
            project=project,
            conversation_id=conversation.id,
            chat_id=chat_id,
            title_hint=text.strip()[:120] or "Задача",
        )

    async def queue_text(self, chat_id: int) -> str:
        project = await self._current_project(chat_id)
        if project is None:
            return "Нет проектов."
        statuses = [
            TaskStatus.PLANNING.value,
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
            TaskStatus.VALIDATING.value,
            TaskStatus.REVIEWING.value,
            TaskStatus.REWORK.value,
            TaskStatus.WAITING_APPROVAL.value,
            TaskStatus.WAITING_INPUT.value,
            TaskStatus.RETRY_WAIT.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.PAUSED.value,
        ]
        tasks = await self.tasks.list(project_id=project.id, statuses=statuses, limit=50)
        lines = [
            f"Очередь {project.name}:",
            f"Свободных слотов исполнителей: {self.scheduler.available_slots}/{self.scheduler.max_workers}",
        ]
        if not tasks:
            lines.append("Активных задач нет.")
        for task in tasks:
            lines.append(f"#{task.id:03d} [{task.status.value}] {task.title[:80]}")
        return "\n".join(lines)

    async def tasks_text(self, chat_id: int) -> str:
        project = await self._current_project(chat_id)
        if project is None:
            return "Нет проектов."
        tasks = await self.tasks.list(project_id=project.id, limit=15)
        if not tasks:
            return f"Проект {project.name}: задач пока нет."
        lines = [f"Задачи проекта {project.name}:"]
        for task in tasks:
            lines.append(f"#{task.id:03d} [{task.status.value}] {task.title[:80]}")
        return "\n".join(lines)

    async def status_text(self, chat_id: int) -> str:
        project = await self._current_project(chat_id)
        lines = ["AI Supervisor"]
        if project:
            active = await self.tasks.active(project.id)
            lines.append(f"Проект: {project.name} ({project.path})")
            lines.append(f"Ветка: {project.base_branch}")
            lines.append(f"Активных задач: {len(active)}")
            for task in active:
                lines.append(f"• #{task.id:03d} {task.status.value}: {task.title[:60]}")
        else:
            lines.append("Проект не выбран.")
        lines.append(
            f"Исполнители: {self.scheduler.active_count}/{self.scheduler.max_workers} "
            f"(свободно {self.scheduler.available_slots})"
        )
        lines.append(f"Модель Kimi: {self.settings.kimi.model}")
        lines.append(f"Модель DeepSeek: {self.settings.deepseek_model}")
        return "\n".join(lines)

    async def agents_text(self, chat_id: int) -> str:
        project = await self._current_project(chat_id)
        if project is None:
            return "Нет проектов."
        active = await self.tasks.active(project.id)
        if not active:
            return "Активных исполнителей нет."
        lines: list[str] = []
        for task in active:
            lines.append(f"#{task.id:03d} {task.status.value}: {task.title[:60]}")
            workers = await self.tasks.workers(task.id)
            for worker in workers:
                lines.append(
                    f"  {worker['id']}: {worker['status']} "
                    f"(session {str(worker.get('session_id') or '—')[:24]})"
                )
        return "\n".join(lines)

    async def usage_text(
        self, chat_id: int, period: str = "today", task_id: int | None = None
    ) -> str:
        project = await self._current_project(chat_id)
        lines = ["Расходы (фактические значения API)"]
        if task_id is not None:
            task = await self.tasks.get(task_id)
            totals = await self.usage.task_totals(task.id)
            lines.append(f"Задача #{task.id:03d}: {self.usage.format_totals(totals)}")
            models = await self.usage.by_model(task_id=task.id)
            for (provider, model), value in models.items():
                lines.append(f"• {provider}/{model}: {self.usage.format_totals(value)}")
            return "\n".join(lines)
        zone = ZoneInfo(self.settings.app.timezone)
        now = datetime.now(zone)
        if period == "week":
            start = datetime.combine(
                (now - timedelta(days=6)).date(), time.min, tzinfo=zone
            ).astimezone(UTC)
            label = "7 дней"
        elif period == "month":
            start = datetime.combine(
                (now - timedelta(days=29)).date(), time.min, tzinfo=zone
            ).astimezone(UTC)
            label = "30 дней"
        else:
            start = datetime.combine(now.date(), time.min, tzinfo=zone).astimezone(UTC)
            label = "сегодня"
        end = start + timedelta(days=7 if period == "week" else 30 if period == "month" else 1)
        totals = await self.usage.range_totals(start, end)
        lines.append(f"{label.capitalize()}: {self.usage.format_totals(totals)}")
        models = await self.usage.by_model(start=start, end=end)
        for (provider, model), value in models.items():
            lines.append(f"• {provider}/{model}: {self.usage.format_totals(value)}")
        if project:
            provider_totals = await self.usage.by_provider(project_id=project.id)
            lines.append(f"Проект {project.name} (все время):")
            for provider, value in provider_totals.items():
                lines.append(f"• {provider}: {self.usage.format_totals(value)}")
        if not self.settings.usage.configured:
            lines.append("Цены не заданы в config.toml, стоимость не рассчитывается.")
        lines.append(
            f"Лимит/сутки: токены {self.settings.limits.daily_token_limit or 'без лимита'}, "
            f"запросы {self.settings.limits.daily_request_limit or 'без лимита'}."
        )
        return "\n".join(lines)

    async def memory_text(self, chat_id: int, query: str) -> str:
        project = await self._current_project(chat_id)
        if query.strip():
            memories = await self.conversations.search_memories(query.strip())
            if not memories:
                return "Совпадений в памяти нет."
            return "\n".join(f"#{item.id} [{item.scope}] {item.content[:500]}" for item in memories)
        items = await self.conversations.get_memories(project_id=project.id if project else None)
        if not items:
            return "Память пуста. Добавить: /memory add <текст>"
        lines = ["Память (последние записи):"]
        for item in items[:15]:
            confirmed = "" if item.user_confirmed else " (неподтверждено)"
            lines.append(f"#{item.id} [{item.scope}{confirmed}] {item.content[:300]}")
        return "\n".join(lines)

    async def doctor_text(self, chat_id: int) -> str:
        import importlib.metadata
        import shutil

        project = await self._current_project(chat_id)
        lines = ["AI Supervisor doctor"]
        try:
            sdk = importlib.metadata.version("deepseek-harness-sdk")
        except Exception:
            sdk = "unknown"
        lines.append(f"Python: {__import__('sys').version.split()[0]}")
        lines.append(f"DSH SDK: {sdk}")
        lines.append(f"bubblewrap: {shutil.which('bwrap') or 'MISSING'}")
        lines.append(f"Git: {shutil.which('git') or 'MISSING'}")
        lines.append(
            f"DB: {self.settings.db_path}; FTS5={getattr(self.usage.db, 'fts_available', '?')}"
        )
        try:
            check = await self.usage.db.fetchone("PRAGMA quick_check")
            lines.append(f"DB quick_check: {check[0] if check else 'no result'}")
        except Exception as exc:
            lines.append(f"DB quick_check: error {type(exc).__name__}")
        unfinished = await self.tasks.list(
            statuses=[
                TaskStatus.PLANNING.value,
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.VALIDATING.value,
                TaskStatus.REVIEWING.value,
                TaskStatus.REWORK.value,
                TaskStatus.WAITING_APPROVAL.value,
                TaskStatus.WAITING_INPUT.value,
                TaskStatus.RETRY_WAIT.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.PAUSED.value,
                TaskStatus.INTERRUPTED.value,
            ],
            limit=100,
        )
        lines.append(f"Unfinished tasks: {len(unfinished)}")
        lines.append(f"Worker slots: {self.scheduler.available_slots}/{self.scheduler.max_workers}")
        lines.append(
            f"Kimi: {self.settings.kimi.model} (key configured: {bool(self.settings.kimi_key)})"
        )
        lines.append(
            f"DeepSeek: {self.settings.deepseek_model} (key configured: {bool(self.settings.deepseek_key)})"
        )
        lines.append(f"Telegram owner: {self.settings.owner_id}")
        lines.append(f"GitHub API: {'configured' if self.github is not None else 'not configured'}")
        lines.append(f"Project: {project.name if project else 'none'}")
        lines.append(
            "CLI `ai-supervisor doctor` additionally checks Kimi /models, Telegram getMe and bwrap."
        )
        return "\n".join(lines)

    async def settings_text(self, chat_id: int) -> str:
        summary = self.settings.public_summary()
        return (
            "Настройки (секреты скрыты):\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)[:3500]
        )

    async def send_diff(self, task_id: int, chat_id: int) -> None:
        task = await self.tasks.get(task_id)
        if not task.diff_path or not Path(task.diff_path).is_file():
            await self.notifier.send_text(chat_id, "Diff для этой задачи не сохранён.")
            return
        text = Path(task.diff_path).read_text(encoding="utf-8", errors="replace")
        text = self.redactor.redact(text)
        if len(text) <= 3500:
            await self._send_long(chat_id, f"```diff\n{text}\n```")
        else:
            await self.notifier.send_document(
                chat_id, Path(task.diff_path), f"Diff задачи #{task.id:03d}"
            )

    async def send_logs(self, task_id: int, chat_id: int) -> None:
        task = await self.tasks.get(task_id)
        events = await self.tasks.events(task_id, limit=80)
        evidence = self._load_evidence(task)
        lines = [f"Логи задачи #{task.id:03d} [{task.status.value}]", ""]
        for event in events:
            lines.append(f"{event['created_at']} {event['event_type']}: {event['message'][:500]}")
        if evidence:
            lines.append("")
            lines.append("Исполнитель stdout (хвост):")
            lines.append(evidence.worker.stdout_tail[-2000:] or "(пусто)")
            lines.append("Исполнитель stderr (хвост):")
            lines.append(evidence.worker.stderr_tail[-2000:] or "(пусто)")
        text = self.redactor.redact("\n".join(lines))
        if len(text) > 3500:
            path = self.settings.task_logs_dir / f"task-{task.id:04d}-logs.txt"
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            await self.notifier.send_document(chat_id, path, f"Логи задачи #{task.id:03d}")
        else:
            await self._send_long(chat_id, text)

    async def send_report(self, task_id: int, chat_id: int) -> None:
        task = await self.tasks.get(task_id)
        if task.report:
            await self._send_long(chat_id, task.report)
        else:
            await self.notifier.send_text(chat_id, "Отчёт ещё не сформирован.")

    async def send_detailed_report(self, task_id: int, chat_id: int) -> None:
        task = await self.tasks.get(task_id)
        project = await self.projects.get(task.project_id)
        spec = self._spec_for(task)
        evidence = self._load_evidence(task)
        review = task.review
        events = await self.tasks.events(task_id, limit=80)
        lines = [
            f"Подробный отчёт задачи #{task.id:03d}",
            f"Проект: {project.name if project else '—'}",
            f"Статус: {task.status.value}",
            f"Тип: {spec.task_type.value}",
            f"Ветка: {task.branch or '—'}",
            f"Base: {task.base_commit or '—'}",
            f"Head: {task.head_commit or '—'}",
            "",
            "План:",
            f"- {spec.goal}",
        ]
        for step in spec.steps[:20]:
            lines.append(f"  • {step}")
        lines += ["", "Критерии:"]
        for criterion in spec.acceptance_criteria:
            lines.append(f"- {criterion}")
        if evidence:
            lines += [
                "",
                "Изменённые файлы:",
                *[f"- {name}" for name in evidence.changed_files[:50]],
                "",
                f"Diff stat: {evidence.diff_stat[:1000] or '(нет)'}",
                f"Worker finish: {evidence.worker.finish_reason}; exit={evidence.worker.return_code}",
                f"Worker report: {(evidence.worker.final_response or '(нет)')[:1500]}",
                "",
                "Проверки:",
            ]
            for check in evidence.checks:
                lines.append(
                    f"- {check.name}: exit={check.exit_code}, timeout={check.timed_out}, "
                    f"{check.command[:200]}"
                )
                if check.stdout_tail:
                    lines.append(f"  stdout: {check.stdout_tail[-500:]}")
                if check.stderr_tail:
                    lines.append(f"  stderr: {check.stderr_tail[-500:]}")
        if review:
            lines += ["", f"Ревью: {review.verdict.value}", f"- {review.summary}"]
            for issue in review.issues[:20]:
                lines.append(
                    f"- [{issue.severity.value}] {issue.criterion}: {issue.evidence} "
                    f"→ {issue.recommendation}"
                )
        lines += ["", "Журнал (последние события):"]
        for event in events[-40:]:
            lines.append(f"- {event['created_at']} {event['event_type']}: {event['message'][:300]}")
        if task.error:
            lines += ["", f"Ошибка: {task.error[:1500]}"]
        text = self.redactor.redact("\n".join(lines))
        if len(text) > 3500:
            path = self.settings.task_logs_dir / f"task-{task.id:04d}-detailed-report.md"
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            await self.notifier.send_document(
                chat_id, path, f"Подробный отчёт задачи #{task.id:03d}"
            )
        else:
            await self._send_long(chat_id, text)

    async def latest_task(self, chat_id: int, project_id: int | None = None) -> TaskRecord | None:
        project = (
            await self.projects.get(project_id)
            if project_id
            else await self._current_project(chat_id)
        )
        if project is None:
            return None
        return await self.tasks.latest(project_id=project.id)

    async def stop_latest_or(self, chat_id: int, task_id: int | None) -> None:
        task = await self.tasks.get(task_id) if task_id else await self.latest_task(chat_id)
        if task is None:
            await self.notifier.send_text(chat_id, "Активная задача не найдена.")
            return
        await self.stop_task(task.id, chat_id)

    async def resume_by_id(self, chat_id: int, task_id: int | None, user_id: int) -> None:
        task = await self.tasks.get(task_id) if task_id else await self.latest_task(chat_id)
        if task is None:
            await self.notifier.send_text(chat_id, "Задача не найдена.")
            return
        await self.resume_task(task.id, user_id, chat_id)
