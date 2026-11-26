from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ai_supervisor.agents.supervisor import SupervisorAgent, SupervisorError
from ai_supervisor.config import Settings
from ai_supervisor.core.capabilities import capability_block
from ai_supervisor.core.config_import import ConfigImportError, ConfigImportService
from ai_supervisor.core.context import ContextError, ProjectContextBuilder
from ai_supervisor.core.evidence_manifest import build_manifest, parse_manifest
from ai_supervisor.core.fault_injection import FaultInjector
from ai_supervisor.core.git_auth import (
    GitAuthError,
    GitAuthManager,
    GitAuthMissing,
    GitAuthRejected,
)
from ai_supervisor.core.git_ops import (
    GitOpsError,
    ensure_remote,
    push_branch,
    remote_branch_sha,
    remote_url,
    verify_remote_branch,
)
from ai_supervisor.core.git_utils import git, git_output
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
from ai_supervisor.core.request_gateway import RequestTooLarge, build_envelope
from ai_supervisor.core.reviewer import ReviewGate, build_report
from ai_supervisor.core.routing import RoutingLevel, RoutingPolicy
from ai_supervisor.core.strategies import StrategyLibrary
from ai_supervisor.core.usage import LimitExceeded, UsageService
from ai_supervisor.core.validation import TaskValidator
from ai_supervisor.core.worker_manager import WorkerError, WorkerManager, WorkerScheduler
from ai_supervisor.core.workspace import WorkspaceError, WorkspaceManager
from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.experience import ExperienceExtractor
from ai_supervisor.memory.projects import ProjectError, ProjectService, validate_remote_url
from ai_supervisor.memory.requests import RequestService
from ai_supervisor.memory.stages import StageError, TaskStageService
from ai_supervisor.memory.tasks import TaskError, TaskService
from ai_supervisor.models import (
    Evidence,
    IntentKind,
    ProjectRecord,
    ReviewResult,
    ReviewVerdict,
    RiskLevel,
    RootCause,
    StageOperation,
    StageRecord,
    StageStatus,
    TaskRecord,
    TaskSpec,
    TaskStatus,
    TaskType,
    TransitionError,
    WorkerResult,
    ensure_transition,
)
from ai_supervisor.presentation.formatting import markdown_to_telegram_html
from ai_supervisor.presentation.progress import ProgressManager
from ai_supervisor.presentation.renderer import (
    completion_card,
    details_view,
    error_card,
)
from ai_supervisor.providers.base import (
    ProviderCallIdentity,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    canonical_role,
)
from ai_supervisor.providers.kimi import (
    KimiError,
    KimiQuotaError,
    KimiRateLimitError,
)
from ai_supervisor.providers.registry import (
    ProviderRecoveryBusy,
    ProviderRecoveryPersistenceError,
    ProviderRecoveryStateError,
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


class CompositeError(RuntimeError):
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
        self.stages = TaskStageService(tasks.db)
        self.experience = ExperienceExtractor(conversations)
        self.requests = RequestService(tasks.db)
        self.progress = ProgressManager(
            settings=settings,
            tasks=tasks,
            projects=projects,
            conversations=conversations,
            stages=self.stages,
            notifier=notifier,
            redactor=redactor,
        )
        self.usage = usage
        self.approvals = approvals
        self.supervisor = supervisor
        self.providers = getattr(supervisor, "providers", None)
        self.workspace = workspace
        self.workers = workers
        self.scheduler = scheduler
        self.context_builder = context_builder
        self.redactor = redactor
        self.notifier = notifier
        self.permissions = PermissionManager()
        self.strategies = StrategyLibrary(Path(__file__).resolve().parent.parent / "strategies")
        self.routing = RoutingPolicy(
            level1_effort=settings.routing.level1_effort,
            level1_max_tokens=settings.routing.level1_max_tokens,
            level2_effort=settings.routing.level2_effort,
            level2_max_tokens=settings.routing.level2_max_tokens,
            level3_effort=settings.routing.level3_effort,
            level3_max_tokens=settings.routing.level3_max_tokens,
            classifier_effort=settings.routing.classifier_effort,
            classifier_max_tokens=settings.routing.classifier_max_tokens,
        )
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
        self.config_import = ConfigImportService(settings)
        self.git_auth = GitAuthManager(settings)
        self._summaries: dict[int, asyncio.Task[Any]] = {}
        self.faults = FaultInjector.from_env()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        await self.approvals.expire_old()
        removed_auth = self.git_auth.cleanup_stale()
        if removed_auth:
            logger.warning("removed %s stale Git auth temp dirs", removed_auth)
        await self._cleanup_old_task_logs()
        await self.tasks.mark_workers_interrupted_on_startup()
        interrupted = await self.tasks.mark_interrupted_on_startup()
        if interrupted:
            logger.warning(
                "marked %s task(s) interrupted after restart: %s", len(interrupted), interrupted
            )
            if self.settings.app.auto_resume_composite:
                for task_id in interrupted:
                    task = await self.tasks.get_optional(task_id)
                    if task is None or task.cancel_requested:
                        continue
                    spec = task.spec
                    if spec is not None and spec.task_type is TaskType.COMPOSITE:
                        await self.tasks.transition(task.id, TaskStatus.QUEUED)
                        await self.tasks.add_event(
                            task.id,
                            "composite-resumed",
                            "Составная задача автоматически продолжена после перезапуска",
                            agent="orchestrator",
                        )
                        self._spawn_job(task.id, plan_if_needed=False, manual_instruction=None)
        if self.settings.app.auto_resume_git_auth_blocked:
            await self._auto_resume_git_auth_blocked()
        await self.requests.recovery_reset()
        await self.progress.restore_active(self.settings.owner_id)

    async def _auto_resume_git_auth_blocked(self) -> None:
        if not self.settings.github_token:
            return
        tasks = await self.tasks.list(statuses=[TaskStatus.BLOCKED.value], limit=100)
        for task in tasks:
            spec = task.spec
            if spec is None or spec.task_type is not TaskType.COMPOSITE:
                continue
            reason = (task.blocked_reason or task.error or "").lower()
            if not any(
                marker in reason
                for marker in (
                    "askpass",
                    "could not read username",
                    "authentication failed",
                    "github_token",
                    "git credentials",
                    "terminal prompts disabled",
                )
            ):
                continue
            stages = await self.stages.list(task.id)
            if not any(
                stage.operation is StageOperation.PUBLISH
                and stage.status in {StageStatus.FAILED, StageStatus.BLOCKED}
                for stage in stages
            ):
                continue
            try:
                await self.tasks.transition(task.id, TaskStatus.QUEUED)
            except (TransitionError, TaskError):
                continue
            await self.tasks.add_event(
                task.id,
                "git-auth-resume",
                "Задача возвращена в очередь после исправления Git-аутентификации",
                agent="orchestrator",
            )
            await self.notifier.send_text(
                self._chat_for_task(task),
                f"Задача #{task.id:03d}: Git-аутентификация настроена, продолжаю с этапа publish. "
                "Потребуется подтверждение публикации.",
            )
            self._spawn_job(task.id, plan_if_needed=False, manual_instruction=None)

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
        self.git_auth.close()
        await self.progress.close()

    # ------------------------------------------------------------------
    # incoming conversation
    # ------------------------------------------------------------------

    async def handle_incoming(
        self, message: IncomingMessage, *, request_mode: str = "auto"
    ) -> None:
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
        try:
            envelope = build_envelope(
                message,
                project_id=project.id,
                conversation_id=conversation.id,
                redactor=self.redactor,
                mode=request_mode,
            )
        except RequestTooLarge as exc:
            await self.notifier.send_text(message.chat_id, str(exc))
            return
        captured, created = await self.requests.capture(envelope)
        if not created:
            task_id = captured.get("task_id")
            if task_id:
                await self.notifier.send_text(
                    message.chat_id, f"Это сообщение уже создало задачу #{int(task_id):03d}."
                )
            else:
                await self.notifier.send_text(
                    message.chat_id, "Это сообщение уже обрабатывается или уже обработано."
                )
            return
        stored_project = await self.projects.get(int(captured["project_id"]))
        if stored_project is None:
            await self.requests.mark_status(str(captured["request_id"]), "failed")
            await self.notifier.send_text(message.chat_id, "Проект исходного запроса удалён.")
            return
        project = stored_project
        try:
            conversation = await self.conversations.get(int(captured["conversation_id"]))
        except Exception:
            await self.requests.mark_status(str(captured["request_id"]), "failed")
            await self.notifier.send_text(message.chat_id, "Исходная беседа запроса удалена.")
            return
        envelope = self.requests.envelope_from_row(captured)
        request_id = envelope.request_id
        current_attachments = tuple(attachment.filename for attachment in message.attachments)
        if envelope.attachment_refs and current_attachments != envelope.attachment_refs:
            await self.requests.mark_status(request_id, "retryable")
            await self.notifier.send_text(
                message.chat_id,
                "Повторная доставка не содержит тех же вложений исходного запроса; "
                "безопасно сопоставить их нельзя. Запрос не обработан.",
            )
            return
        try:
            attachment_text, images = await self._prepare_attachments(message)
        except Exception as exc:
            await self.requests.mark_status(request_id, "retryable")
            await self.notifier.send_text(
                message.chat_id, f"Вложения не обработаны: {type(exc).__name__}."
            )
            return
        text = envelope.original_text.strip()
        if text:
            text = self.redactor.redact(text)
        if attachment_text:
            text = (text + "\n\n" + attachment_text).strip()
        if not text and not images:
            await self.requests.mark_status(request_id, "rejected")
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
            classifier = self.routing.classifier_profile
            intent_result, classify_usage = await self.supervisor.classify(
                project_block=project_block,
                conversation_block=conversation_block,
                latest_user_message=self.redactor.redact(text) or "(изображение)",
                conversation_id=conversation.id,
                reasoning_effort=classifier.effort,
                max_completion_tokens=classifier.max_tokens,
            )
            await self._record_kimi_usage(
                classify_usage,
                task_id=None,
                project_id=project.id,
                role="classification",
                context_chars=len(project_block) + len(conversation_block),
            )
        except (KimiQuotaError, ProviderQuotaError) as exc:
            if self.providers is not None:
                self.providers.mark_quota_exhausted("classifier")
            await self.requests.mark_status(request_id, "retryable")
            await self._notify_quota(message.chat_id, exc)
            return
        except asyncio.CancelledError:
            await self.requests.mark_status(request_id, "cancelled")
            raise
        except (KimiError, ProviderError, LimitExceeded) as exc:
            await self.requests.mark_status(request_id, "retryable")
            await self.notifier.send_text(message.chat_id, self._friendly_error(exc))
            return
        except Exception:
            await self.requests.mark_status(request_id, "failed")
            raise

        ambiguous = intent_result.clarification or intent_result.confidence < 0.6
        if intent_result.clarification:
            question = self.redactor.redact(intent_result.clarification)[:1500]
            await self.conversations.add_message(
                conversation.id,
                "assistant",
                question,
                meta={"kind": "clarification"},
            )
            await self.requests.mark_status(envelope.request_id, "clarification")
            await self.notifier.send_text(message.chat_id, question)
            return
        if intent_result.kind is IntentKind.TASK and not ambiguous:
            try:
                task = await self._begin_task(
                    project=project,
                    conversation_id=conversation.id,
                    chat_id=message.chat_id,
                    title_hint=intent_result.task_title_hint or "",
                    request_text=envelope.original_text,
                    request_id=request_id,
                )
            except Exception:
                await self.requests.mark_status(request_id, "failed")
                raise
            if task is None:
                await self.requests.mark_status(request_id, "rejected")
            return

        await self.requests.mark_status(request_id, "chat")
        try:
            await self._chat(
                project=project,
                conversation=conversation,
                summary=summary,
                context_messages=context_messages,
                images=images,
                chat_id=message.chat_id,
                request_id=request_id,
            )
        except Exception:
            await self.requests.mark_status(request_id, "failed")
            raise

    async def _chat(
        self,
        *,
        project: ProjectRecord,
        conversation: Any,
        summary: str | None,
        context_messages: list[Any],
        images: list[tuple[bytes, str]],
        chat_id: int,
        request_id: str | None = None,
    ) -> None:
        api_messages = self.conversations.messages_for_api(context_messages)
        latest_text = next(
            (
                str(message.get("content") or "")
                for message in reversed(api_messages)
                if message.get("role") == "user"
            ),
            "",
        )
        memory_block = await self.conversations.memory_context(
            project_id=project.id,
            conversation_id=conversation.id,
            query=latest_text,
        )
        try:
            await self.usage.ensure_kimi_budget(0)
            chat_profile = self.routing.reasoning(self.routing.for_chat(latest_text))
            response = await self.supervisor.chat(
                project_block=self._project_block(project),
                memory_block=memory_block,
                summary=summary,
                messages=api_messages,
                images=images,
                conversation_id=conversation.id,
                reasoning_effort=chat_profile.effort,
                max_completion_tokens=chat_profile.max_tokens,
                project_id=project.id,
            )
            await self._record_kimi_usage(
                response.usage,
                task_id=None,
                project_id=project.id,
                role="chat",
                context_chars=sum(
                    len(str(message.get("content") or "")) for message in api_messages
                ),
                routing_level=int(self.routing.for_chat(latest_text)),
            )
        except (KimiQuotaError, ProviderQuotaError) as exc:
            if self.providers is not None:
                self.providers.mark_quota_exhausted("chat")
            if request_id:
                await self.requests.mark_status(request_id, "retryable")
            await self._notify_quota(chat_id, exc)
            return
        except asyncio.CancelledError:
            if request_id:
                await self.requests.mark_status(request_id, "cancelled")
            raise
        except (KimiError, ProviderError, LimitExceeded) as exc:
            if request_id:
                await self.requests.mark_status(request_id, "retryable")
            await self.notifier.send_text(chat_id, self._friendly_error(exc))
            return
        content = self.redactor.redact(response.content.strip())
        if not content:
            content = "Провайдер не вернул текстовый ответ."
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
        await self._send_markdown(chat_id, content)
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
            if attachment.size > self.settings.context.max_file_read_bytes:
                parts.append(
                    f"[Вложение {attachment.filename} пропущено: больше "
                    f"{self.settings.context.max_file_read_bytes} байт]"
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
                f"{content[: self.settings.context.max_file_read_bytes]}\n"
                f"[КОНЕЦ ВЛОЖЕНИЯ: {attachment.filename}]"
            )
        return "\n\n".join(parts), images

    def _project_block(self, project: ProjectRecord) -> str:
        instructions = project.instructions[:4000] if project.instructions else "(нет)"
        return self.redactor.redact(
            f"name: {project.name}\n"
            f"path: {project.path}\n"
            f"description: {project.description or '(нет)'}\n"
            f"stack: {project.stack or '(не указан)'}\n"
            f"base_branch: {project.base_branch}\n"
            f"instructions:\n{instructions}\n"
            f"extra_read_paths: {project.extra_read_paths_json}"
        )

    def _conversation_block(self, messages: list[Any]) -> str:
        lines: list[str] = []
        for message in messages[-12:]:
            content = self.redactor.redact(message.content.strip())
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
        request_text: str = "",
        request_id: str | None = None,
    ) -> TaskRecord | None:
        async with self._creation_lock:
            return await self._begin_task_locked(
                project=project,
                conversation_id=conversation_id,
                chat_id=chat_id,
                title_hint=title_hint,
                request_text=request_text,
                request_id=request_id,
            )

    async def _begin_task_locked(
        self,
        *,
        project: ProjectRecord,
        conversation_id: int,
        chat_id: int,
        title_hint: str,
        request_text: str = "",
        request_id: str | None = None,
    ) -> TaskRecord | None:
        active = await self.tasks.active(project.id)
        if active:
            ids = ", ".join(f"#{task.id:03d}" for task in active)
            await self.notifier.send_text(
                chat_id,
                f"В проекте {project.name} уже есть активная задача {ids}. "
                "Дождитесь её завершения, примите/отклоните изменения или остановите её через /stop.",
            )
            return None
        title = self.redactor.redact((title_hint or "Новая задача").strip())[:120]
        if request_id:
            task, created = await self.requests.create_task(
                request_id=request_id,
                project_id=project.id,
                conversation_id=conversation_id,
                title=title,
                max_rework=self.settings.limits.max_rework,
                source_request=request_text or title,
            )
            if not created:
                return task
        else:
            task = await self.tasks.create(
                project.id,
                conversation_id,
                title,
                max_rework=self.settings.limits.max_rework,
                source_request=request_text or title,
                request_id=None,
                intent_source="telegram",
            )
            await self.tasks.add_event(task.id, "accepted", "Задача принята от владельца")
        await self.progress.update(task.id, force=True, chat_id=chat_id)
        self._spawn_job(task.id, plan_if_needed=True, manual_instruction=None)
        return task

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
        except (KimiQuotaError, ProviderQuotaError) as exc:
            if self.providers is not None:
                provider_name = getattr(exc, "provider", None)
                if provider_name:
                    self.providers.mark_provider_quota(provider_name)
                    await self.providers.persist_state(
                        provider_name, "QUOTA_EXHAUSTED", category="quota"
                    )
                else:
                    await self.tasks.add_event(
                        task_id,
                        "provider-quota-unknown",
                        "Quota error without provider identity; no provider was blocked",
                        {"error_type": type(exc).__name__},
                        agent="orchestrator",
                    )
            await self._pause_task(task_id, self._quota_message_for(exc))
        except (KimiRateLimitError, ProviderRateLimitError):
            await self._retry_wait(
                task_id,
                "Провайдер временно ограничил запросы. "
                "Повтор будет выполнен после безопасной паузы; прогресс сохранён.",
            )
        except (
            KimiError,
            ProviderError,
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

    async def _plan_task(
        self,
        task: TaskRecord,
        project: ProjectRecord,
        *,
        request_override: str | None = None,
    ) -> TaskRecord:
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
        original_intent = task.source_request.strip()
        latest_user = (
            request_override
            or original_intent
            or (user_messages[-1].content if user_messages else task.title)
        )
        memory_block = await self.conversations.memory_context(
            project_id=project.id,
            conversation_id=conversation.id,
            query=latest_user,
        )
        project_context = await self.context_builder.build(project, goal=latest_user)
        requested_level = self.routing.for_request(latest_user)
        plan_profile = self.routing.reasoning(requested_level)
        await self.usage.ensure_kimi_budget(task.id)
        spec, plan_usage = await self.supervisor.plan(
            project_block=self._project_block(project),
            conversation_block=self._conversation_block(context_messages),
            memory_block=memory_block,
            project_context=project_context,
            latest_user_message=latest_user,
            conversation_id=conversation.id,
            reasoning_effort=plan_profile.effort,
            max_completion_tokens=plan_profile.max_tokens,
            capabilities_block=capability_block(),
            strategy_block=(
                self.strategies.select(latest_user, limit=self.settings.strategies.max_selected)
                if self.settings.strategies.enabled
                else ""
            ),
        )
        final_level = max(int(requested_level), int(self.routing.for_spec(spec)))
        updates: dict[str, object] = {
            "routing_level": final_level,
            "schema_version": 2,
        }
        if not spec.scope.strip():
            updates["scope"] = spec.goal[:8000]
        if not spec.source_request.strip() or original_intent:
            updates["source_request"] = latest_user[:8000]
        if spec.max_attempts == 0:
            updates["max_attempts"] = self.settings.limits.max_iterations
        if spec.max_minutes == 0:
            updates["max_minutes"] = self.settings.limits.max_task_minutes
        spec = spec.model_copy(update=updates)
        await self._record_kimi_usage(
            plan_usage,
            task_id=task.id,
            project_id=project.id,
            role="planning",
            prompt_version=self.supervisor.prompt_metadata().get("version"),
            routing_level=final_level,
            context_chars=len(project_context) + len(memory_block) + len(latest_user),
        )
        task = await self.tasks.save_spec(task.id, spec)
        await self.tasks.add_event(
            task.id,
            "plan",
            f"ТЗ сформировано: {spec.title}",
            {
                "acceptance_criteria": spec.acceptance_criteria,
                "steps": spec.steps,
                "prompt_versions": self.supervisor.prompt_metadata(),
                "routing_level": spec.routing_level,
            },
        )
        if task.status is not TaskStatus.QUEUED:
            task = await self.tasks.transition(task.id, TaskStatus.QUEUED)
        preflight = await self._preflight_contract(task, project, spec)
        if preflight.status is not TaskStatus.QUEUED:
            return preflight
        task = preflight
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))
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
        }:
            reason = (
                f"Тип {spec.task_type.value} требует отдельного привилегированного исполнителя; "
                "используйте COMPOSITE с поддерживаемыми этапами или отдельный этап."
            )
            await self._block_task(task.id, reason)
            return await self.tasks.get(task.id)
        if spec.task_type is TaskType.COMPOSITE:
            if not spec.stages:
                await self._block_task(task.id, "COMPOSITE не содержит этапов.")
                return await self.tasks.get(task.id)
            await self.stages.ensure(task.id, spec.stages)
            await self.tasks.add_event(
                task.id,
                "composite-plan",
                f"Сохранён план из {len(spec.stages)} этапов",
                {"stages": [stage.model_dump() for stage in spec.stages]},
            )
            return task
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
        from ai_supervisor.presentation.renderer import approval_card

        await self.notifier.send_text(
            self._chat_for_task(task),
            approval_card(
                title="Требуется подтверждение",
                why=reason[:1000],
                target=f"задача #{task.id:03d}",
                consequences="Операция будет выполнена один раз; отказ безопасно остановит задачу.",
                details="Подтверждение привязано к текущей задаче и параметрам операции.",
            ),
            [
                [
                    Button("Разрешить", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
                [Button("Подробности", f"det:{task.id}"), Button("Отменить", f"stop:{task.id}")],
            ],
            parse_mode="HTML",
        )
        return approval_id

    async def _resume_review_checkpoint(
        self, task: TaskRecord, project: ProjectRecord, project_context: str
    ) -> bool:
        """Continue a non-composite task from persisted evidence, without rerunning worker."""
        evidence = self._load_evidence(task)
        if evidence is None or task.review is not None or not task.worktree_path:
            return False
        worktree = Path(task.worktree_path)
        if not worktree.is_dir():
            return False
        spec = self._spec_for(task)
        saved_manifest = parse_manifest(task.evidence_manifest_json)
        if saved_manifest is None:
            return False
        current_manifest = await build_manifest(
            project=project, task=task, worktree=worktree, evidence=evidence, spec=spec
        )
        if current_manifest.manifest_hash != saved_manifest.manifest_hash:
            await self._block_task(
                task.id,
                "Evidence устарел: состояние worktree/HEAD/index/untracked изменилось "
                "после сохранения checkpoint. Требуется решение владельца.",
            )
            return True
        project_context = await self.context_builder.build(project, goal=spec.goal, root=worktree)
        precheck = ReviewGate.precheck(evidence, spec, worktree=worktree)
        if precheck is None:
            await self._try_transition(task.id, TaskStatus.REVIEWING)
            await self.usage.ensure_kimi_budget(task.id)
            review_level = RoutingLevel(spec.routing_level or int(self.routing.for_spec(spec)))
            profile = self.routing.reasoning(review_level)
            review, review_usage = await self.supervisor.review(
                project_block=self._project_block(project),
                spec=spec,
                evidence=evidence,
                project_context=project_context,
                reasoning_effort=profile.effort,
                max_completion_tokens=profile.max_tokens,
                task_id=task.id,
                project_id=project.id,
            )
            await self._record_kimi_usage(
                review_usage,
                task_id=task.id,
                project_id=project.id,
                role="review",
                prompt_version=self.supervisor.prompt_metadata().get("version"),
                routing_level=int(review_level),
            )
        else:
            review = precheck
        review = ReviewGate.enforce(review, evidence, spec, worktree=worktree)
        task = await self.tasks.update_fields(task.id, review_json=review.model_dump_json())
        await self.tasks.add_event(
            task.id,
            "review-checkpoint",
            f"Review checkpoint: {review.verdict.value}",
            {"summary": review.summary[:2000]},
        )
        if review.verdict is ReviewVerdict.APPROVED:
            await self._finish_for_acceptance(
                task.id, project, spec_evidence=evidence, review=review
            )
            return True
        # The checkpoint cannot safely re-enter the worker loop from REVIEWING.
        # Preserve the evidence and require an explicit rework/resume decision.
        await self._block_task(
            task.id,
            "Review checkpoint требует решения владельца; повторный запуск worker не выполнялся.\n"
            + review.summary,
            review=review,
        )
        return True

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
        if self._spec_for(task).task_type is not TaskType.COMPOSITE:
            if await self._resume_review_checkpoint(task, project, project_context):
                return
        if self._spec_for(task).task_type is TaskType.COMPOSITE:
            await self._execute_composite(
                task, project, project_context, manual_instruction=manual_instruction
            )
            return
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
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))
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
            manifest = await build_manifest(
                project=project,
                task=task,
                worktree=Path(task.worktree_path or ""),
                evidence=evidence,
                spec=spec,
            )
            task = await self.tasks.update_fields(
                task.id,
                head_commit=head_commit,
                diff_path=str(state_dir / "diff.patch"),
                evidence_json=evidence.model_dump_json(),
                evidence_manifest_json=manifest.model_dump_json(),
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
            await self.progress.update(task.id, chat_id=self._chat_for_task(task))
            spec = self._spec_for(task)
            # Reviewer must see the current project snapshot, not the one taken
            # before the worker changed the worktree.
            project_context = await self.context_builder.build(
                project, goal=spec.goal, root=Path(task.worktree_path or project.path)
            )
            precheck = ReviewGate.precheck(evidence, spec, worktree=Path(task.worktree_path or ""))
            if precheck is None:
                await self._try_transition(task.id, TaskStatus.REVIEWING)
                await self.usage.ensure_kimi_budget(task.id)
                review_level = RoutingLevel(spec.routing_level or int(self.routing.for_spec(spec)))
                review_profile = self.routing.reasoning(review_level)
                review, review_usage = await self.supervisor.review(
                    project_block=self._project_block(project),
                    spec=spec,
                    evidence=evidence,
                    project_context=project_context,
                    reasoning_effort=review_profile.effort,
                    max_completion_tokens=review_profile.max_tokens,
                    task_id=task.id,
                    project_id=project.id,
                )
                await self._record_kimi_usage(
                    review_usage,
                    task_id=task.id,
                    project_id=project.id,
                    role="review",
                    prompt_version=self.supervisor.prompt_metadata().get("version"),
                    routing_level=int(review_level),
                )
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
                    "root_cause": review.root_cause.value,
                    "issues": [item.model_dump() for item in review.issues],
                },
            )
            if review.verdict is ReviewVerdict.APPROVED:
                await self._finish_for_acceptance(
                    task.id, project, spec_evidence=evidence, review=review
                )
                return
            if review.root_cause is RootCause.PLANNER_ERROR:
                events = await self.tasks.events(task.id, limit=100)
                replanned = any(event["event_type"] == "planner_replan" for event in events)
                if replanned:
                    await self._block_task(
                        task.id,
                        "Повторная ошибка планировщика: автоматический пересмотр уже выполнялся; "
                        "исполнитель не отправлен на доработку.\n" + review.summary,
                        review=review,
                    )
                    return
                original = (task.spec.source_request if task.spec else "") or task.title
                await self.tasks.add_event(
                    task.id,
                    "planner_replan",
                    "Ограниченный автоматический пересмотр ТЗ после planner_error",
                    {"reason": review.summary[:2000], "original_request": original[:8000]},
                    agent="orchestrator",
                )
                await self._try_transition(task.id, TaskStatus.WAITING_INPUT)
                task = await self.tasks.get(task.id)
                await self._try_transition(task.id, TaskStatus.PLANNING)
                task = await self.tasks.get(task.id)
                await self._plan_task(task, project, request_override=original)
                task = await self.tasks.get(task.id)
                await self._try_transition(task.id, TaskStatus.QUEUED)
                task = await self.tasks.get(task.id)
                await self._execute_task(
                    task.id,
                    project=project,
                    project_context=project_context,
                    manual_instruction=None,
                )
                return
            if (
                review.root_cause
                in {
                    RootCause.ENVIRONMENT_ERROR,
                    RootCause.EXTERNAL_SERVICE_ERROR,
                }
                and review.verdict is not ReviewVerdict.APPROVED
            ):
                await self._retry_wait(task.id, review.summary)
                return
            if (
                review.root_cause is RootCause.PERMISSION_ERROR
                and review.verdict is not ReviewVerdict.APPROVED
            ):
                await self._await_required_approval(task.id, review)
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

    # ------------------------------------------------------------------
    # COMPOSITE execution
    # ------------------------------------------------------------------

    async def _execute_composite(
        self,
        task: TaskRecord,
        project: ProjectRecord,
        project_context: str,
        *,
        manual_instruction: str | None,
    ) -> None:
        spec = self._spec_for(task)
        if spec.task_type is not TaskType.COMPOSITE:
            raise CompositeError("task is not composite")
        if not spec.stages:
            raise CompositeError("COMPOSITE task has no executable stages")
        await self.stages.ensure(task.id, spec.stages)
        task = await self.tasks.update_fields(
            task.id, worker_name="Python Orchestrator (COMPOSITE)"
        )
        if not task.worktree_path:
            prepared = await self.workspace.prepare(task, project)
            dsh_home = self.settings.dsh_homes_dir / f"task-{task.id:04d}"
            dsh_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            task = await self.tasks.update_fields(
                task.id,
                worker_name="Composite",
                worktree_path=prepared["worktree_path"],
                branch=prepared["branch"],
                base_ref=prepared["base_ref"],
                base_commit=prepared["base_commit"],
                dsh_home=str(dsh_home),
            )
        worktree = Path(task.worktree_path or "")
        if not worktree.is_dir():
            raise CompositeError("worktree не существует")
        await self.stages.reset_running(task.id)
        await self.stages.reset_waiting(task.id)
        await self.stages.reset_failed(task.id)

        if not task.head_commit:
            task = await self.tasks.update_fields(
                task.id, head_commit=(await git_output(worktree, "rev-parse", "HEAD")).strip()
            )
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))

        while True:
            task = await self.tasks.get(task.id)
            project = await self.projects.get(task.project_id) or project
            if task.cancel_requested:
                await self.stages.cancel_open(task.id)
                await self._try_transition(task.id, TaskStatus.CANCELLED)
                return
            budget_error = await self._composite_budget_error(task, project, spec)
            if budget_error:
                await self._block_task(task.id, budget_error)
                return
            stages = await self.stages.list(task.id)
            if any(stage.status is StageStatus.WAITING_APPROVAL for stage in stages):
                # If the task is running, an approval was lost/expired; request it again.
                await self.stages.reset_waiting(task.id)
                stages = await self.stages.list(task.id)
            if all(
                stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED} for stage in stages
            ):
                await self._finalize_composite(task, project, spec, stages)
                return
            failed = [
                stage
                for stage in stages
                if stage.status in {StageStatus.FAILED, StageStatus.BLOCKED}
            ]
            if failed:
                await self._block_task(
                    task.id,
                    f"Этап {failed[0].stage_key} не выполнен: {failed[0].error or 'неизвестная причина'}",
                )
                return
            completed = {
                stage.stage_key
                for stage in stages
                if stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED}
            }
            ready = [
                stage
                for stage in stages
                if stage.status in {StageStatus.PENDING, StageStatus.READY}
                and all(dep in completed for dep in stage.depends_on)
            ]
            if not ready:
                await self._block_task(
                    task.id, "Нет готовых этапов: граф зависимостей не даёт прогресса."
                )
                return
            stage = ready[0]
            if stage.status is StageStatus.PENDING:
                stage = await self.stages.set_status(stage.id, StageStatus.READY)
            if self._stage_needs_approval(stage) and not stage.approval_granted:
                try:
                    await self._request_stage_approval(task, project, stage, worktree)
                except GitAuthMissing as exc:
                    await self._wait_for_git_auth(task, stage, str(exc))
                return
            await self.stages.set_status(stage.id, StageStatus.RUNNING, increment_attempts=True)
            await self.tasks.add_event(
                task.id,
                f"stage-{stage.stage_key}",
                f"Этап {stage.stage_key} начат: {stage.goal}",
                {"operation": stage.operation.value, "executor": stage.executor.value},
                agent=stage.executor.value,
                stage_id=stage.id,
            )
            await self.progress.update(task.id, chat_id=self._chat_for_task(task))
            try:
                task = await self.tasks.get(task.id)
                result = await self._dispatch_stage(
                    task, project, stage, worktree, project_context, manual_instruction
                )
            except (GitAuthMissing, GitAuthRejected) as exc:
                stage = await self.stages.set_status(
                    stage.id, StageStatus.FAILED, error=self.redactor.redact(str(exc))[:4000]
                )
                await self._wait_for_git_auth(task, stage, str(exc))
                return
            except (
                CompositeError,
                StageError,
                WorkspaceError,
                GitOpsError,
                GitAuthError,
                GitHubError,
                ConfigImportError,
                WorkerError,
            ) as exc:
                stage = await self.stages.set_status(
                    stage.id, StageStatus.FAILED, error=self.redactor.redact(str(exc))[:4000]
                )
                await self._block_task(
                    task.id,
                    f"Этап {stage.stage_key} не выполнен: {str(exc)[:2000]}",
                )
                return
            stage = await self.stages.set_status(
                stage.id,
                StageStatus.COMPLETED,
                result=result,
                error=None,
                approval_granted=False,
            )
            await self.tasks.add_event(
                task.id,
                f"stage-{stage.stage_key}",
                f"Этап {stage.stage_key} завершён",
                {"result": result},
                agent=stage.executor.value,
                stage_id=stage.id,
            )
            await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))

    async def _composite_budget_error(
        self, task: TaskRecord, project: ProjectRecord, spec: TaskSpec
    ) -> str | None:
        now = datetime.now(UTC)
        limit_minutes = spec.max_minutes or self.settings.limits.max_task_minutes
        deadline = (task.started_at or now).astimezone(UTC) + timedelta(minutes=limit_minutes)
        if now >= deadline:
            return "Превышен максимальный лимит времени составной задачи."
        budget = spec.token_budget or project.token_budget
        if budget > 0:
            totals = await self.usage.task_totals(task.id)
            if totals.total_tokens >= budget:
                return f"Исчерпан token_budget задачи ({totals.total_tokens}/{budget})."
        return None

    @staticmethod
    def _stage_needs_approval(stage: StageRecord) -> bool:
        if RiskLevel.HIGH in stage.required_permissions:
            return True
        return stage.operation in {
            StageOperation.IMPORT_CONFIG,
            StageOperation.GITHUB_ENSURE_REPO,
            StageOperation.PUBLISH,
        }

    @staticmethod
    def _stage_approval_action(stage: StageRecord) -> Action:
        return {
            StageOperation.IMPORT_CONFIG: Action.CONFIG_IMPORT,
            StageOperation.GITHUB_ENSURE_REPO: Action.GITHUB_REPOSITORY_CREATE,
            StageOperation.PUBLISH: Action.GIT_PUSH,
        }.get(stage.operation, Action.STAGE_APPROVAL)

    async def _request_stage_approval(
        self, task: TaskRecord, project: ProjectRecord, stage: StageRecord, worktree: Path
    ) -> None:
        action = self._stage_approval_action(stage)
        payload: dict[str, Any] = {
            "task_id": task.id,
            "stage_id": stage.id,
            "stage_key": stage.stage_key,
            "project_id": project.id,
            "operation": stage.operation.value,
            "action": action.value,
        }
        details = [f"Задача #{task.id:03d}, этап {stage.stage_key}: {stage.goal}"]
        try:
            if stage.operation is StageOperation.IMPORT_CONFIG:
                plan = await self.config_import.plan(
                    project,
                    await self._stage_import_source(project, stage),
                    stage.destination or "",
                    overwrite=stage.overwrite,
                    target_root=worktree,
                    allow_existing_identical=True,
                )
                payload.update(
                    {
                        "source": str(plan.source),
                        "destination": str(plan.destination.relative_to(worktree)),
                        "overwrite": plan.overwrite,
                        "source_hash": plan.source_hash,
                        "already_applied": plan.already_applied,
                    }
                )
                details.append(
                    f"Импорт: {plan.source} → {plan.destination.relative_to(worktree)} "
                    f"({plan.file_count} файлов, {plan.total_bytes} байт)"
                )
            elif stage.operation is StageOperation.GITHUB_ENSURE_REPO:
                if self.github is None:
                    raise GitAuthMissing("GITHUB_TOKEN не настроен для GitHub API")
                owner = stage.repo_owner
                if not owner:
                    user = await self.github.authenticated_user()
                    owner = str(user.get("login") or "")
                    if not owner:
                        raise CompositeError("GitHub не вернул имя пользователя")
                    await self.stages.update(stage.id, repo_owner=owner)
                    stage = await self.stages.get(stage.id)
                payload.update(
                    {
                        "repo_owner": owner,
                        "repo_name": stage.repo_name,
                        "private": stage.private,
                    }
                )
                details.append(f"GitHub: {owner}/{stage.repo_name}, private={stage.private}")
            elif stage.operation is StageOperation.PUBLISH:
                if not task.head_commit:
                    raise CompositeError("нет коммита для публикации")
                branch = stage.branch or project.base_branch
                if not stage.merge and not stage.branch and task.branch:
                    branch = task.branch
                if not project.remote_url:
                    raise CompositeError(
                        "remote проекта не задан; сначала добавьте github_ensure_repo"
                    )
                actual_remote = await remote_url(Path(project.path), stage.remote)
                validate_remote_url(actual_remote)
                parsed_remote = urlsplit(actual_remote)
                if (
                    parsed_remote.scheme == "https"
                    and (parsed_remote.hostname or "").lower() in self.settings.github.push_hosts
                    and not self.settings.github_token
                ):
                    raise GitAuthMissing("GITHUB_TOKEN не настроен для HTTPS-push в GitHub")
                payload.update(
                    {
                        "remote": stage.remote,
                        "branch": branch,
                        "commit": task.head_commit,
                        "remote_url": actual_remote,
                        "merge": stage.merge,
                    }
                )
                details.append(
                    f"Публикация: {stage.remote}/{branch}, commit {task.head_commit[:12]}, "
                    f"remote={actual_remote}, merge={stage.merge}"
                )
            else:
                details.append(
                    f"Операция: {stage.operation.value}, executor={stage.executor.value}"
                )
        except (CompositeError, ConfigImportError, GitOpsError, GitHubError, ProjectError) as exc:
            await self._block_task(task.id, f"Подготовка этапа {stage.stage_key} невозможна: {exc}")
            return
        approval_id = await self.approvals.request(
            action=action,
            payload=payload,
            created_by=self.settings.owner_id,
            task_id=task.id,
            ttl_seconds=900,
        )
        stage = await self.stages.set_status(
            stage.id, StageStatus.WAITING_APPROVAL, approval_id=approval_id, approval_granted=False
        )
        await self._try_transition(task.id, TaskStatus.WAITING_APPROVAL)
        await self.tasks.add_event(
            task.id,
            "stage-approval-requested",
            f"Этап {stage.stage_key}: требуется подтверждение",
            payload,
            agent="orchestrator",
            stage_id=stage.id,
        )
        await self.notifier.send_text(
            self._chat_for_task(task),
            f"Задача #{task.id:03d}: требуется подтверждение этапа {stage.stage_key}.\n"
            + "\n".join(details)
            + "\nПодтверждение привязано к указанному действию, репозиторию/ветке и коммиту.",
            [
                [
                    Button("Разрешить один раз", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
                [
                    Button("Показать diff", f"dif:{task.id}"),
                    Button("Остановить", f"stop:{task.id}"),
                ],
            ],
        )

    async def _stage_binding_error(
        self, task: TaskRecord, project: ProjectRecord, stage: StageRecord, payload: dict[str, Any]
    ) -> str | None:
        if stage.operation is StageOperation.IMPORT_CONFIG:
            try:
                plan = await self.config_import.plan(
                    project,
                    await self._stage_import_source(project, stage),
                    stage.destination or "",
                    overwrite=stage.overwrite,
                    target_root=Path(task.worktree_path or ""),
                    allow_existing_identical=True,
                )
            except ConfigImportError as exc:
                return f"источник/назначение импорта изменились: {exc}"
            if plan.source_hash != payload.get("source_hash"):
                return "источник импорта изменился после запроса"
            if str(plan.destination.relative_to(Path(task.worktree_path or ""))) != payload.get(
                "destination"
            ):
                return "назначение импорта изменилось после запроса"
        elif stage.operation is StageOperation.PUBLISH:
            current_commit = task.head_commit or ""
            if current_commit != payload.get("commit"):
                return "коммит изменился после запроса на публикацию"
            expected_branch = stage.branch or project.base_branch
            if not stage.merge and not stage.branch and task.branch:
                expected_branch = task.branch
            if expected_branch != payload.get("branch"):
                return "ветка публикации изменилась"
            try:
                actual = await remote_url(
                    Path(project.path), str(payload.get("remote") or stage.remote)
                )
            except GitOpsError as exc:
                return f"remote больше не настроен: {exc}"
            if actual != payload.get("remote_url"):
                return "remote URL изменился после запроса на публикацию"
        elif stage.operation is StageOperation.GITHUB_ENSURE_REPO:
            if stage.repo_name != payload.get("repo_name"):
                return "имя GitHub-репозитория изменилось"
            if stage.private != payload.get("private"):
                return "видимость GitHub-репозитория изменилась"
            if stage.repo_owner and stage.repo_owner != payload.get("repo_owner"):
                return "владелец GitHub-репозитория изменился"
        return None

    async def _handle_stage_approval(
        self,
        task_id: int,
        stage_id: int,
        approval_id: int,
        payload: dict[str, Any],
        user_id: int,
        chat_id: int,
    ) -> None:
        task = await self.tasks.get(task_id)
        project = await self.projects.get(task.project_id)
        stage = await self.stages.get(stage_id)
        if project is None or stage.task_id != task.id:
            await self.notifier.send_text(chat_id, "Этап/проект не найден.")
            return
        if stage.status is not StageStatus.WAITING_APPROVAL or stage.approval_id != approval_id:
            await self.notifier.send_text(chat_id, "Это подтверждение больше не актуально.")
            return
        reason = await self._stage_binding_error(task, project, stage, payload)
        if reason:
            await self.stages.set_status(stage.id, StageStatus.BLOCKED, error=reason)
            await self._block_task(
                task.id, f"Подтверждение этапа {stage.stage_key} устарело: {reason}"
            )
            return
        stage = await self.stages.set_status(
            stage.id, StageStatus.READY, approval_granted=True, error=None
        )
        if not await self._try_transition(task.id, TaskStatus.QUEUED):
            await self.notifier.send_text(chat_id, "Не удалось вернуть задачу в очередь.")
            return
        await self.tasks.reset_started_at(task.id)
        await self.tasks.update_fields(task.id, cancel_requested=False)
        await self.tasks.add_event(
            task.id,
            "stage-approved",
            f"Этап {stage.stage_key} подтверждён",
            payload,
            agent="orchestrator",
            stage_id=stage.id,
        )
        await self.notifier.send_text(
            chat_id, f"Задача #{task.id:03d}: этап {stage.stage_key} подтверждён, продолжаю."
        )
        self._spawn_job(task.id, plan_if_needed=False, manual_instruction=None)

    async def _handle_stage_rejection(
        self, task_id: int, stage_id: int, reason: str, chat_id: int
    ) -> None:
        stage = await self.stages.get_optional(stage_id)
        task = await self.tasks.get_optional(task_id)
        if stage is None or task is None or stage.task_id != task.id:
            return
        if stage.status is StageStatus.WAITING_APPROVAL:
            await self.stages.set_status(stage.id, StageStatus.BLOCKED, error=reason)
        await self._block_task(task.id, f"Этап {stage.stage_key} отклонён владельцем: {reason}")
        await self.notifier.send_text(
            chat_id,
            f"Задача #{task.id:03d}: этап {stage.stage_key} отклонён; завершённые этапы сохранены.",
        )

    async def _dispatch_stage(
        self,
        task: TaskRecord,
        project: ProjectRecord,
        stage: StageRecord,
        worktree: Path,
        project_context: str,
        manual_instruction: str | None,
    ) -> dict[str, Any]:
        if stage.operation is StageOperation.IMPORT_CONFIG:
            return await self._stage_import(task, project, stage, worktree)
        if stage.operation is StageOperation.GIT_COMMIT:
            return await self._stage_commit(task, stage, worktree)
        if stage.operation is StageOperation.VALIDATE:
            return await self._stage_validate(task, project, stage, worktree)
        if stage.operation is StageOperation.GITHUB_ENSURE_REPO:
            return await self._stage_github(task, project, stage)
        if stage.operation is StageOperation.PUBLISH:
            return await self._stage_publish(task, project, stage)
        if stage.operation is StageOperation.KIMI_ANALYZE:
            return await self._stage_kimi(task, project, stage, project_context, manual_instruction)
        if stage.operation is StageOperation.DEEPSEEK_CHANGE:
            return await self._stage_deepseek(
                task, project, stage, project_context, manual_instruction
            )
        raise CompositeError(f"неизвестная операция этапа: {stage.operation.value}")

    async def _stage_import_source(self, project: ProjectRecord, stage: StageRecord) -> str:
        if stage.source:
            return stage.source
        return str(await self.config_import.discover(project, stage.goal))

    async def _stage_import(
        self, task: TaskRecord, project: ProjectRecord, stage: StageRecord, worktree: Path
    ) -> dict[str, Any]:
        plan = await self.config_import.plan(
            project,
            await self._stage_import_source(project, stage),
            stage.destination or "",
            overwrite=stage.overwrite,
            target_root=worktree,
            allow_existing_identical=True,
        )
        result = await self.config_import.execute(plan)
        return {
            "destination": str(plan.destination.relative_to(worktree)),
            "files": result.files,
            "bytes": result.total_bytes,
            "already_applied": plan.already_applied,
            "backup": result.backup,
            "secret_scan": "passed",
        }

    async def _stage_commit(
        self, task: TaskRecord, stage: StageRecord, worktree: Path
    ) -> dict[str, Any]:
        staged = await git(worktree, "add", "-A", timeout_seconds=60, check=False)
        if staged.returncode != 0:
            raise CompositeError(f"git add failed: {staged.stderr.strip()[:1000]}")
        quiet = await git(worktree, "diff", "--cached", "--quiet", "HEAD", check=False)
        changed = quiet.returncode != 0
        if changed:
            commit = await self.workspace.commit_changes(
                task=task,
                worktree=worktree,
                changed=True,
                message=stage.commit_message or None,
            )
        else:
            commit = (await git_output(worktree, "rev-parse", "HEAD")).strip()
        await self.tasks.update_fields(task.id, head_commit=commit)
        return {"commit": commit, "changed": changed}

    async def _stage_validate(
        self, task: TaskRecord, project: ProjectRecord, stage: StageRecord, worktree: Path
    ) -> dict[str, Any]:
        spec = self._spec_for(task)
        dsh_home = Path(task.dsh_home or self.settings.dsh_homes_dir / f"task-{task.id:04d}")
        state_dir = self.settings.task_logs_dir / f"task-{task.id:04d}" / f"stage-{stage.stage_key}"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        check_timeout = max(30.0, (spec.max_minutes or self.settings.limits.max_task_minutes) * 60)
        evidence = await self.workspace.collect_evidence(
            task=task,
            project=project,
            worktree=worktree,
            dsh_home=dsh_home,
            state_dir=state_dir,
            worker=WorkerResult(
                session_id=f"composite-{stage.stage_key}",
                final_response="deterministic composite validation",
                finish_reason="completed",
                return_code=0,
            ),
            check_timeout_seconds=check_timeout,
            diff_path=state_dir / "diff.patch",
        )
        failed = [check.name for check in evidence.checks if not check.passed]
        if failed:
            raise CompositeError("провалены проверки: " + ", ".join(failed))
        if stage.require_change and not evidence.has_changes:
            raise CompositeError("этап validate требует фактического изменения, diff пуст")
        for name in evidence.changed_files:
            allowed = any(
                name == path or name.startswith(path.rstrip("/") + "/")
                for path in stage.allowed_paths
            )
            if not allowed:
                raise CompositeError(f"файл вне allowed_paths этапа: {name}")
        await self.tasks.update_fields(
            task.id,
            evidence_json=evidence.model_dump_json(),
            diff_path=str(state_dir / "diff.patch"),
            head_commit=evidence.head_commit,
        )
        return {
            "checks": [check.model_dump() for check in evidence.checks],
            "changed_files": evidence.changed_files,
            "head_commit": evidence.head_commit,
            "secret_scan": "passed",
        }

    async def _stage_github(
        self, task: TaskRecord, project: ProjectRecord, stage: StageRecord
    ) -> dict[str, Any]:
        if self.github is None:
            raise CompositeError("GITHUB_TOKEN не настроен")
        user = await self.github.authenticated_user()
        login = str(user.get("login") or "")
        if not login:
            raise CompositeError("GitHub не вернул имя пользователя")
        owner = stage.repo_owner or login
        if not stage.repo_name:
            raise CompositeError("repo_name не задан")
        info = await self.github.repository_info(owner, stage.repo_name)
        created = info is None
        if info is None:
            # The authenticated user's own repos use /user/repos; only a
            # different owner is an organization endpoint.
            org = None if owner == login else owner
            info = await self.github.create_repository(
                stage.repo_name, private=stage.private, owner=org
            )
        clone_url = str(info.get("clone_url") or "")
        if not clone_url:
            html = str(info.get("html_url") or "")
            if html:
                clone_url = html.rstrip("/") + ".git"
        if not clone_url:
            raise CompositeError("GitHub не вернул clone_url")
        validate_remote_url(clone_url)
        changed = await ensure_remote(Path(project.path), stage.remote, clone_url)
        project = await self.projects.update_instructions(project.id, remote_url=clone_url)
        return {
            "repository": str(info.get("full_name") or f"{owner}/{stage.repo_name}"),
            "clone_url": clone_url,
            "created": created,
            "remote": stage.remote,
            "remote_changed": changed,
        }

    async def _stage_publish(
        self, task: TaskRecord, project: ProjectRecord, stage: StageRecord
    ) -> dict[str, Any]:
        branch = stage.branch or project.base_branch
        if not stage.merge and not stage.branch and task.branch:
            branch = task.branch
        repo = Path(project.path).resolve()
        actual_remote = await remote_url(repo, stage.remote)
        validate_remote_url(actual_remote)
        if stage.merge:
            # Validate credentials/remote before changing the local base branch.
            await remote_branch_sha(repo, stage.remote, branch, auth=self.git_auth)
            if not await self.workspace.is_applied(task, project):
                new_head = await self.workspace.apply(task, project, cleanup=False)
                task = await self.tasks.update_fields(task.id, head_commit=new_head)
            branch = project.base_branch
        local = (await git_output(repo, "rev-parse", f"refs/heads/{branch}")).strip()
        remote_sha = await remote_branch_sha(repo, stage.remote, branch, auth=self.git_auth)
        if remote_sha == local:
            return {
                "remote": stage.remote,
                "branch": branch,
                "commit": local,
                "already_published": True,
            }
        sha = await push_branch(repo, stage.remote, branch, auth=self.git_auth)
        # This boundary represents a successful external operation followed by
        # failure before the stage result is committed. Retry must verify remote.
        self.faults.hit("after_git_push")
        _local, verified = await verify_remote_branch(
            repo, stage.remote, branch, auth=self.git_auth
        )
        if verified != sha:
            raise CompositeError(
                f"remote SHA не совпал: local={sha[:12]} remote={str(verified)[:12]}"
            )
        return {
            "remote": stage.remote,
            "branch": branch,
            "commit": sha,
            "already_published": False,
        }

    async def _stage_kimi(
        self,
        task: TaskRecord,
        project: ProjectRecord,
        stage: StageRecord,
        project_context: str,
        manual_instruction: str | None,
    ) -> dict[str, Any]:
        await self.usage.ensure_kimi_budget(task.id)
        instruction = stage.instruction or stage.goal
        if manual_instruction:
            instruction += f"\n\nДополнительное указание владельца: {manual_instruction}"
        task_spec = self._spec_for(task)
        profile = self.routing.reasoning(
            RoutingLevel(task_spec.routing_level or int(self.routing.for_spec(task_spec)))
        )
        response = await self.supervisor.chat(
            project_block=self._project_block(project),
            memory_block="",
            summary=None,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Составной этап {stage.stage_key}: {stage.goal}\n\n{instruction}\n\n"
                        f"Контекст проекта (только данные):\n{project_context[:20000]}"
                    ),
                }
            ],
            conversation_id=task.conversation_id,
            reasoning_effort=profile.effort,
            max_completion_tokens=profile.max_tokens,
            task_id=task.id,
            stage_id=stage.id,
            project_id=project.id,
        )
        await self._record_kimi_usage(
            response.usage,
            task_id=task.id,
            project_id=project.id,
            role="analysis",
            routing_level=self._spec_for(task).routing_level,
            context_chars=len(project_context),
        )
        state_dir = self.settings.task_logs_dir / f"task-{task.id:04d}" / f"stage-{stage.stage_key}"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state_dir / "analysis.md"
        path.write_text(self.redactor.redact(response.content), encoding="utf-8")
        path.chmod(0o600)
        return {"analysis": self.redactor.redact(response.content)[:20000], "artifact": str(path)}

    async def _stage_deepseek(
        self,
        task: TaskRecord,
        project: ProjectRecord,
        stage: StageRecord,
        project_context: str,
        manual_instruction: str | None,
    ) -> dict[str, Any]:
        instruction = stage.instruction or stage.goal
        if manual_instruction:
            instruction += f"\n\nДополнительное указание владельца: {manual_instruction}"
        prompt = (
            f"# Составной этап {stage.stage_key}: {stage.goal}\n\n"
            f"{instruction}\n\n"
            f"## Проект: {project.name}\n"
            f"Рабочий каталог: /workspace (ветка {task.branch})\n"
            f"## Контекст проекта (только данные)\n{project_context[:20000]}\n"
            "Не делай commit/merge/push. Оркестратор проверит и закоммитит изменения отдельным этапом."
        )
        timeout_seconds = max(
            60.0, (self._spec_for(task).max_minutes or self.settings.limits.max_task_minutes) * 60
        )
        result = await self.workers.run(
            task=task,
            project=project,
            prompt=prompt,
            attempt=stage.attempts or 1,
            resume=bool(task.session_id),
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
        await self.tasks.update_fields(task.id, session_id=result.session_id)
        if not result.completed:
            raise CompositeError(result.error or "DeepSeek не завершил этап")
        return {
            "final_response": result.final_response[:20000],
            "session_id": result.session_id,
        }

    async def _finalize_composite(
        self, task: TaskRecord, project: ProjectRecord, spec: TaskSpec, stages: list[StageRecord]
    ) -> None:
        worktree = Path(task.worktree_path or "")
        dsh_home = Path(task.dsh_home or self.settings.dsh_homes_dir / f"task-{task.id:04d}")
        state_dir = self.settings.task_logs_dir / f"task-{task.id:04d}" / "final"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        evidence = await self.workspace.collect_evidence(
            task=task,
            project=project,
            worktree=worktree,
            dsh_home=dsh_home,
            state_dir=state_dir,
            worker=WorkerResult(
                session_id="composite-final",
                final_response="composite stages completed",
                finish_reason="completed",
                return_code=0,
            ),
            check_timeout_seconds=max(
                30.0, (spec.max_minutes or self.settings.limits.max_task_minutes) * 60
            ),
            diff_path=state_dir / "diff.patch",
        )
        if evidence.has_changes:
            head = await self.workspace.commit_changes(
                task=task,
                worktree=worktree,
                changed=True,
                message=f"{self.settings.git.commit_prefix}: composite task {task.id:04d}",
            )
            evidence = evidence.model_copy(update={"head_commit": head})
            task = await self.tasks.update_fields(task.id, head_commit=head)
        validation_spec = spec.model_copy(update={"requires_file_changes": False})
        validation = TaskValidator.validate(
            spec=validation_spec, evidence=evidence, worktree=worktree
        )
        if validation.status != "pass":
            detail = "; ".join(issue.evidence or issue.criterion for issue in validation.issues)
            await self._block_task(task.id, f"Финальная проверка не пройдена: {detail}")
            return
        review = ReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="Все этапы COMPOSITE завершены; детерминированная проверка пройдена.",
        )
        task = await self.tasks.update_fields(
            task.id,
            evidence_json=evidence.model_dump_json(),
            review_json=review.model_dump_json(),
            head_commit=evidence.head_commit,
            error=None,
            blocked_reason=None,
        )
        if not await self._try_transition(task.id, TaskStatus.COMPLETED):
            await self._block_task(task.id, "Не удалось завершить составную задачу.")
            return
        task = await self.tasks.get(task.id)
        usage_totals = await self.usage.by_provider(task_id=task.id)
        report = build_report(
            task=task,
            project_name=project.name,
            spec=spec,
            evidence=evidence,
            review=review,
            usage_service=self.usage,
            usage_totals=usage_totals,
            final_status=TaskStatus.COMPLETED,
        )
        report += "\n\n" + await self._composite_stage_report(task.id)
        await self.tasks.update_fields(task.id, report=report)
        await self.tasks.add_event(
            task.id,
            "composite-completed",
            "Составная задача завершена",
            {"stages": [stage.stage_key for stage in stages]},
            agent="orchestrator",
        )
        await self.experience.record_success(task, project, stages)
        await self._maybe_usage_warning(task.id)
        mode = await self.conversations.get_ui_mode(self._chat_for_task(task))
        usage_totals = await self.usage.by_provider(task_id=task.id)
        model_requests = sum(
            int(getattr(totals, "requests", 0)) for totals in usage_totals.values()
        )
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))
        await self.notifier.send_text(
            self._chat_for_task(task),
            completion_card(
                task,
                project,
                evidence=evidence,
                review=review,
                stages=stages,
                model_requests=model_requests,
                mode=mode,
                repo_url=project.remote_url,
            ),
            [
                [
                    Button("Подробности", f"det:{task.id}"),
                    Button("Расход токенов", f"use:{task.id}"),
                ],
                [Button("Журнал", f"log:{task.id}")],
            ],
            parse_mode="HTML",
        )
        await self.conversations.add_message(
            task.conversation_id,
            "assistant",
            report,
            meta={"task_id": task.id, "kind": "composite-report", "status": "completed"},
        )

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

    async def _wait_for_git_auth(self, task: TaskRecord, stage: StageRecord, reason: str) -> None:
        safe = self.redactor.redact(reason)[:4000]
        try:
            await self.tasks.transition(task.id, TaskStatus.WAITING_INPUT)
        except (TransitionError, TaskError):
            await self._block_task(
                task.id, f"Не удалось приостановить задачу для настройки Git-доступа: {safe}"
            )
            return
        task = await self.tasks.update_fields(task.id, error=safe)
        await self.tasks.add_event(
            task.id,
            "waiting_git_auth",
            f"Этап {stage.stage_key} ожидает GitHub-аутентификацию",
            {"error": safe},
            agent="orchestrator",
            stage_id=stage.id,
        )
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))
        await self.notifier.send_text(
            self._chat_for_task(task),
            error_card(
                task,
                await self.projects.get(task.project_id),
                reason="GITHUB_TOKEN не настроен или отклонён: " + safe,
            ),
            [
                [Button("Повторить", f"res:{task.id}"), Button("Диагностика", f"det:{task.id}")],
                [Button("Подробности", f"det:{task.id}"), Button("Отменить", f"stop:{task.id}")],
            ],
            parse_mode="HTML",
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
        project = await self.projects.get(task.project_id)
        if project is not None:
            await self.experience.record_failure(
                task, project, category="blocked", reason=safe_reason
            )
        report = await self._final_report(task, project, TaskStatus.BLOCKED)
        await self.tasks.update_fields(task.id, report=report)
        stages = await self.stages.list(task.id)
        mode = await self.conversations.get_ui_mode(self._chat_for_task(task))
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))
        await self.notifier.send_text(
            self._chat_for_task(task),
            error_card(task, project, reason=safe_reason, mode=mode, stages=stages),
            [
                [Button("Повторить", f"res:{task.id}"), Button("Диагностика", f"det:{task.id}")],
                [Button("Подробности", f"det:{task.id}"), Button("Отменить", f"stop:{task.id}")],
            ],
            parse_mode="HTML",
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
        mode = await self.conversations.get_ui_mode(self._chat_for_task(task))
        stages = await self.stages.list(task.id)
        await self.progress.update(task.id, force=True, chat_id=self._chat_for_task(task))
        await self.notifier.send_text(
            self._chat_for_task(task),
            completion_card(
                task,
                project,
                evidence=spec_evidence,
                review=review,
                stages=stages,
                mode=mode,
            ),
            [
                [Button("Принять", f"acc:{task.id}"), Button("На доработку", f"rew:{task.id}")],
                [Button("Подробности", f"det:{task.id}"), Button("Diff", f"dif:{task.id}")],
                [Button("Журнал", f"log:{task.id}"), Button("Откатить", f"rb:{task.id}")],
            ],
            parse_mode="HTML",
        )
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
        await self.experience.record_success(task, project, [])
        report = await self._final_report(task, project, TaskStatus.COMPLETED)
        await self.tasks.update_fields(task.id, report=report)
        mode = await self.conversations.get_ui_mode(chat_id)
        stages = await self.stages.list(task.id)
        await self.progress.update(task.id, force=True, chat_id=chat_id)
        buttons = [
            [Button("Подробности", f"det:{task.id}"), Button("Расход токенов", f"use:{task.id}")],
            [Button("Журнал", f"log:{task.id}")],
        ]
        if project.remote_url and project.remote_url.startswith(("https://", "http://")):
            buttons.insert(0, [Button("Открыть репозиторий", url=project.remote_url)])
        if project.remote_url:
            buttons.insert(0, [Button("Опубликовать в Git", f"push:{task.id}")])
        await self.notifier.send_text(
            chat_id,
            completion_card(
                task,
                project,
                evidence=self._load_evidence(task),
                review=task.review,
                stages=stages,
                mode=mode,
                repo_url=project.remote_url,
            ),
            buttons,
            parse_mode="HTML",
        )
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
            TaskStatus.WAITING_APPROVAL,
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
            await self.stages.cancel_open(task.id)
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
        from ai_supervisor.presentation.renderer import approval_card

        await self.notifier.send_text(
            chat_id,
            approval_card(
                title="Создание GitHub-репозитория",
                why=f"Создать репозиторий {name}",
                target="GitHub API",
                consequences="Репозиторий будет создан от имени владельца токена.",
                details=f"Видимость: {'private' if private else 'public'}",
            ),
            [
                [
                    Button("Разрешить", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
            ],
            parse_mode="HTML",
        )

    async def request_config_import(
        self, chat_id: int, user_id: int, source: str, destination: str, overwrite: bool = False
    ) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        project = await self._current_project(chat_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Проект не выбран.")
            return
        try:
            plan = await self.config_import.plan(project, source, destination, overwrite=overwrite)
        except ConfigImportError as exc:
            await self.notifier.send_text(chat_id, f"Импорт отклонён: {exc}")
            return
        relative_destination = str(plan.destination.relative_to(Path(project.path).resolve()))
        approval_id = await self.approvals.request(
            action=Action.CONFIG_IMPORT,
            payload={
                "project_id": project.id,
                "source": str(plan.source),
                "destination": relative_destination,
                "overwrite": plan.overwrite,
                "source_hash": plan.source_hash,
            },
            created_by=user_id,
            ttl_seconds=900,
        )
        from ai_supervisor.presentation.renderer import approval_card

        await self.notifier.send_text(
            chat_id,
            approval_card(
                title="Импорт конфигурации",
                why=f"Импортировать {plan.source}",
                target=f"проект {project.name} → {relative_destination}",
                consequences="Файлы будут записаны в рабочее дерево; коммит не создаётся автоматически.",
                volume=f"{plan.file_count} файлов, {plan.total_bytes} байт; overwrite={plan.overwrite}",
                details="Текстовые файлы проверены на секреты; они не найдены.",
            ),
            [
                [
                    Button("Разрешить", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
            ],
            parse_mode="HTML",
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
        parsed_remote = urlsplit(actual_remote)
        if (
            parsed_remote.scheme == "https"
            and (parsed_remote.hostname or "").lower() in self.settings.github.push_hosts
            and not self.settings.github_token
        ):
            await self.notifier.send_text(
                chat_id,
                "GITHUB_TOKEN не настроен: HTTPS-push в GitHub невозможен. "
                "Настройте токен и повторите публикацию; задача сохранена.",
            )
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
        from ai_supervisor.presentation.renderer import approval_card

        await self.notifier.send_text(
            chat_id,
            approval_card(
                title="Публикация в Git",
                why=f"Опубликовать {project.base_branch} задачи #{task.id:03d}",
                target=f"{project.remote_url} · origin/{project.base_branch}",
                consequences="Ветка будет отправлена без force; удалённый коммит будет проверен.",
                details="Force-push не используется.",
            ),
            [
                [
                    Button("Разрешить", f"apv:{approval_id}"),
                    Button("Отклонить", f"rej:{approval_id}"),
                ],
                [Button("Подробности", f"det:{task.id}"), Button("Отмена", f"rej:{approval_id}")],
            ],
            parse_mode="HTML",
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

    async def request_provider_recovery(self, target: str, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        if self.providers is None:
            await self.notifier.send_text(chat_id, "Провайдеры не инициализированы.")
            return
        try:
            provider_name, provider = self.providers.resolve_target(target)
        except ProviderError as exc:
            await self.notifier.send_text(chat_id, f"Провайдер не найден: {exc}")
            return
        states = await self.providers.state_snapshot()
        state = states.get(provider_name, {})
        if state.get("state") != "QUOTA_EXHAUSTED":
            await self.notifier.send_text(
                chat_id,
                f"{provider_name}: состояние {state.get('state', 'UNKNOWN')}. "
                "Восстановление требуется только для QUOTA_EXHAUSTED.",
            )
            return
        approval_id = await self.approvals.request(
            action=Action.PROVIDER_RECOVERY,
            payload={
                "kind": "provider_recovery",
                "provider_id": provider_name,
                "model_id": provider.model,
                "account_scope_id": "default",
                "state_generation": int(state.get("generation") or 0),
            },
            created_by=user_id,
            ttl_seconds=600,
        )
        await self.notifier.send_text(
            chat_id,
            f"Восстановление {provider_name}\n\n"
            f"Состояние: QUOTA_EXHAUSTED\n"
            f"Причина: {state.get('reason') or 'квота исчерпана'}\n"
            f"Последняя проверка: {state.get('last_checked_at') or '—'}\n\n"
            "Будет выполнен минимальный платный generation probe без инструментов, истории, "
            "памяти и project context. Подтвердите.",
            [
                [
                    Button("Подтвердить probe", f"apv:{approval_id}"),
                    Button("Отмена", f"rej:{approval_id}"),
                ]
            ],
        )

    async def _execute_provider_recovery(
        self, payload: dict[str, Any], user_id: int, chat_id: int
    ) -> None:
        if self.providers is None:
            return
        provider_name = str(payload.get("provider_id") or "")
        expected_model = str(payload.get("model_id") or "")
        expected_generation = int(payload.get("state_generation") or 0)
        provider = self.providers.available().get(provider_name)
        if provider is None or (expected_model and provider.model != expected_model):
            await self.notifier.send_text(
                chat_id,
                "Конфигурация провайдера изменилась после подтверждения; probe отменён.",
            )
            return
        identity = ProviderCallIdentity(
            request_id=f"probe-{uuid.uuid4().hex}",
            logical_call_id=f"probe-{uuid.uuid4().hex}",
            attempt_id="probe-1",
            provider_id=provider_name,
            account_scope_id=str(payload.get("account_scope_id") or "default"),
            model_id=provider.model,
            role="provider_recovery",
            reasoning_profile="minimal",
        )
        try:
            await self.usage.ensure_kimi_budget(0)
            response = await self.providers.recover_generation(
                provider_name, identity, expected_generation=expected_generation
            )
            await self.usage.record(
                provider=provider_name,
                model=provider.model,
                usage=response.usage,
                role="provider_recovery",
                request_id=identity.request_id,
                logical_call_id=identity.logical_call_id,
                attempt_id=identity.attempt_id,
                provider_id=provider_name,
                account_scope_id=identity.account_scope_id,
                reasoning_profile=identity.reasoning_profile,
            )
        except ProviderRecoveryBusy:
            await self.notifier.send_text(
                chat_id, "Probe для этой конфигурации уже выполняется; повторный не запущен."
            )
            return
        except ProviderRecoveryStateError as exc:
            await self.notifier.send_text(
                chat_id,
                f"Восстановление {provider_name} не требуется или approval устарел: {exc}. "
                "Платный probe не выполнялся.",
            )
            return
        except ProviderRecoveryPersistenceError:
            await self.notifier.send_text(
                chat_id,
                "Генерация успешна, но состояние не удалось сохранить в SQLite. "
                "Блокировка сохранена; восстановление не подтверждено.",
            )
            return
        except (ProviderError, KimiError) as exc:
            category = type(exc).__name__
            await self.notifier.send_text(
                chat_id,
                f"Восстановление {provider_name} не подтверждено.\n"
                f"Причина: {category}. Блокировка сохранена.\n"
                "Точное время восстановления квоты провайдером не сообщено.",
            )
            return
        await self.notifier.send_text(
            chat_id,
            f"Провайдер {provider_name} восстановлен: минимальная генерация успешна.\n"
            "Блокировка снята. Ожидающие задачи не запускаются автоматически; "
            "продолжите нужные через /resume.",
        )
        active = await self.tasks.active(None)
        if active:
            ids = ", ".join(f"#{task.id:03d}" for task in active[:20])
            await self.notifier.send_text(chat_id, f"Ожидают продолжения: {ids}.")

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
        if action is Action.PROVIDER_RECOVERY:
            await self._execute_provider_recovery(payload, user_id, chat_id)
            return
        if payload.get("stage_id"):
            await self._handle_stage_approval(
                int(payload["task_id"]),
                int(payload["stage_id"]),
                approval_id,
                payload,
                user_id,
                chat_id,
            )
            return
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
        elif action is Action.CONFIG_IMPORT:
            project = await self.projects.get(int(payload["project_id"]))
            if project is None:
                await self.notifier.send_text(chat_id, "Проект импорта не найден.")
                return
            try:
                plan = await self.config_import.plan(
                    project,
                    str(payload["source"]),
                    str(payload["destination"]),
                    overwrite=bool(payload.get("overwrite", False)),
                )
                if plan.source_hash != payload.get("source_hash"):
                    raise ConfigImportError(
                        "источник изменился после запроса; запросите импорт заново"
                    )
                result = await self.config_import.execute(plan)
            except ConfigImportError as exc:
                await self.notifier.send_text(chat_id, f"Импорт не выполнен: {exc}")
                return
            logger.info(
                "config import project=%s files=%s bytes=%s destination=%s",
                project.id,
                result.files,
                result.total_bytes,
                result.destination,
            )
            await self.notifier.send_text(
                chat_id,
                f"Конфигурация импортирована: {result.destination}\n"
                f"Файлов: {result.files}, байт: {result.total_bytes}\n"
                + (f"Резервная копия: {result.backup}\n" if result.backup else "")
                + "Изменения записаны в рабочее дерево и не закоммичены; проверьте git status.",
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
                sha = await push_branch(Path(project.path), remote, branch, auth=self.git_auth)
                _local, remote_sha = await verify_remote_branch(
                    Path(project.path), remote, branch, auth=self.git_auth
                )
            except (GitOpsError, ProjectError, GitAuthError) as exc:
                await self.notifier.send_text(
                    chat_id, f"Публикация не выполнена: {self.redactor.redact(str(exc))}"
                )
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
        pending = await self.approvals.get(approval_id)
        changed = await self.approvals.reject_pending(approval_id, user_id=user_id)
        if changed and pending and pending.get("payload_json"):
            try:
                payload = json.loads(pending["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict) and payload.get("stage_id"):
                await self._handle_stage_rejection(
                    int(payload["task_id"]),
                    int(payload["stage_id"]),
                    "rejected by owner",
                    chat_id,
                )
                return
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
        if task.task_type is TaskType.COMPOSITE:
            if target is TaskStatus.PAUSED:
                await self.stages.reset_running(task.id)
            else:
                await self.stages.cancel_open(task.id)
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

    async def _composite_stage_report(self, task_id: int) -> str:
        stages = await self.stages.list(task_id)
        if not stages:
            return ""
        lines = ["## COMPOSITE этапы", ""]
        for stage in stages:
            duration = "—"
            if stage.started_at:
                end = stage.finished_at or datetime.now(UTC)
                seconds = max(0, int((end - stage.started_at).total_seconds()))
                duration = f"{seconds // 60} мин {seconds % 60} сек"
            result = stage.result or {}
            details: list[str] = []
            if "commit" in result:
                details.append(f"commit={str(result['commit'])[:12]}")
            if "files" in result:
                details.append(f"files={result['files']}")
            if "already_applied" in result:
                details.append(f"already_applied={result['already_applied']}")
            if "changed_files" in result:
                details.append("changed=" + ",".join(str(x) for x in result["changed_files"][:8]))
            if "secret_scan" in result:
                details.append(f"secret_scan={result['secret_scan']}")
            if "repository" in result:
                details.append(f"repository={result['repository']}")
            if "branch" in result and "remote" in result:
                details.append(
                    f"{result['remote']}/{result['branch']}={str(result.get('commit', ''))[:12]}"
                )
            if stage.error:
                details.append("error=" + self.redactor.redact(stage.error)[:500])
            detail_text = "; ".join(details) if details else stage.goal[:160]
            lines.append(
                f"- {stage.stage_key}: {stage.status.value} "
                f"[{stage.executor.value}] {duration} — {detail_text}"
            )
        return "\n".join(lines)

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
            report = (
                f"Задача #{task.id:03d} — {status.value}\n"
                f"Проект: {project.name}\n"
                f"Ошибка: {task.error or 'нет данных'}\n"
                f"Стоимость: данные об использовании — /usage"
            )
        else:
            usage_totals = await self.usage.by_provider(task_id=task.id)
            report = build_report(
                task=task,
                project_name=project.name,
                spec=spec,
                evidence=evidence,
                review=review,
                usage_service=self.usage,
                usage_totals=usage_totals,
                final_status=status,
            )
        if task.task_type is TaskType.COMPOSITE:
            suffix = await self._composite_stage_report(task.id)
            if suffix:
                report += "\n\n" + suffix
        return report

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
        return self.redactor.redact(
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
        self,
        usage: Any,
        *,
        task_id: int | None,
        project_id: int | None,
        role: str | None = None,
        prompt_version: str | None = None,
        routing_level: int | None = None,
        context_chars: int | None = None,
    ) -> None:
        canonical = canonical_role(role or "planner")
        provider_name = getattr(usage, "provider_id", "") or "unknown"
        model_name = getattr(usage, "model_id", "") or "unknown"
        await self.usage.record(
            provider=provider_name,
            model=model_name,
            usage=usage,
            task_id=task_id,
            project_id=project_id,
            role=canonical,
            prompt_version=prompt_version
            or (
                self.supervisor.prompt_metadata().get("version")
                if hasattr(self.supervisor, "prompt_metadata")
                else None
            ),
            routing_level=routing_level,
            context_chars=context_chars,
            raw_usage=getattr(self.supervisor, "last_raw_usage", None),
            request_id=getattr(usage, "request_id", "") or None,
            logical_call_id=getattr(usage, "logical_call_id", "") or None,
            attempt_id=getattr(usage, "attempt_id", "") or None,
            provider_id=getattr(usage, "provider_id", "") or None,
            account_scope_id=getattr(usage, "account_scope_id", "") or None,
            stage_id=getattr(usage, "stage_id", None),
            reasoning_profile=getattr(usage, "reasoning_profile", "") or None,
        )

    @staticmethod
    def _quota_message() -> str:
        return (
            "Квота провайдера исчерпана\n\n"
            "Задача приостановлена. Прогресс сохранён.\n\n"
            "Причина: достигнут лимит текущего окна провайдера.\n"
            "Точное время восстановления неизвестно.\n\n"
            "Локальные команды остаются доступны."
        )

    def _quota_message_for(self, exc: Exception) -> str:
        # Keep the user-facing text stable; never include raw provider JSON.
        return self._quota_message()

    async def _notify_quota(self, chat_id: int, exc: Exception) -> None:
        await self.notifier.send_text(chat_id, self._quota_message_for(exc))

    def _friendly_error(self, exc: Exception) -> str:
        text = self.redactor.redact(str(exc))[:1500]
        if isinstance(exc, LimitExceeded):
            return "Лимит: " + text
        if isinstance(exc, ProviderError):
            return "Ошибка провайдера: " + text
        if isinstance(exc, KimiError):
            return "Ошибка Kimi: " + text
        return "Ошибка: " + text

    async def _send_markdown(self, chat_id: int, text: str) -> None:
        formatted = markdown_to_telegram_html(text)
        limit = self.settings.telegram.max_message_chars
        if len(formatted) <= min(limit, 3800):
            await self.notifier.send_text(chat_id, formatted, parse_mode="HTML")
            return
        # Long provider answers: preserve Markdown as plain text rather than risk unbalanced HTML.
        await self._send_long(chat_id, text)

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
            await self._record_kimi_usage(
                usage, task_id=None, project_id=project_id, role="summarizer"
            )
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

    async def chat_direct(self, chat_id: int, user_id: int, text: str) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        project = await self._current_project(chat_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Сначала выберите или создайте проект.")
            return
        conversation = await self.conversations.get_or_create_conversation(chat_id, project.id)
        clean = self.redactor.redact(text.strip())[:8000]
        if not clean:
            await self.notifier.send_text(chat_id, "Пустой запрос не обработан.")
            return
        await self.conversations.add_message(conversation.id, "user", clean)
        summary, context_messages = await self.conversations.build_context(conversation.id)
        await self._chat(
            project=project,
            conversation=conversation,
            summary=summary,
            context_messages=context_messages,
            images=[],
            chat_id=chat_id,
        )

    async def retry_request(self, request_id: str, user_id: int, chat_id: int) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        row = await self.requests.get(request_id)
        if row is None:
            await self.notifier.send_text(chat_id, "Запрос не найден.")
            return
        if row.get("task_id"):
            await self.notifier.send_text(
                chat_id, f"Запрос уже создал задачу #{int(row['task_id']):03d}."
            )
            return
        if row.get("status") not in {"retryable", "failed", "cancelled"}:
            await self.notifier.send_text(
                chat_id, f"Запрос в состоянии {row.get('status')}; повтор не требуется."
            )
            return
        if row.get("attachment_refs_json") not in (None, "", "[]"):
            await self.requests.mark_status(request_id, "retryable")
            await self.notifier.send_text(
                chat_id,
                "Вложения запроса нельзя безопасно восстановить автоматически; "
                "отправьте запрос с вложениями повторно.",
            )
            return
        updated, changed = await self.requests.retry(request_id)
        if not changed:
            await self.notifier.send_text(chat_id, "Повторная обработка уже запущена.")
            return
        from ai_supervisor.core.interfaces import IncomingMessage

        envelope = self.requests.envelope_from_row(updated)
        await self.handle_incoming(
            IncomingMessage(
                chat_id=int(envelope.chat_id),
                user_id=int(envelope.owner_id),
                text=envelope.original_text,
                telegram_message_id=envelope.source_message_id,
            ),
            request_mode=str(envelope.mode),
        )

    async def create_task_from_text(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        source_message_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self._assert_owner(user_id)
        self._assert_owner_chat(chat_id)
        project = await self._current_project(chat_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Сначала выберите или создайте проект.")
            return
        clean_text = self.redactor.redact(text.strip())
        if len(clean_text) > 8000:
            await self.notifier.send_text(chat_id, "Задача превышает 8000 символов; сократите её.")
            return
        conversation = await self.conversations.get_or_create_conversation(chat_id, project.id)
        from ai_supervisor.core.interfaces import IncomingMessage

        if idempotency_key is None and source_message_id is None:
            # Internal callers must provide stable idempotency; a random fallback
            # prevents accidental cross-request dedup but is not retry-safe.
            idempotency_key = f"internal:{uuid.uuid4().hex}"
        try:
            envelope = build_envelope(
                IncomingMessage(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=clean_text,
                    telegram_message_id=source_message_id,
                ),
                project_id=project.id,
                conversation_id=conversation.id,
                redactor=self.redactor,
                mode="task",
                idempotency_key=idempotency_key,
            )
        except RequestTooLarge as exc:
            await self.notifier.send_text(chat_id, str(exc))
            return
        captured, created = await self.requests.capture(envelope)
        if not created:
            task_id = captured.get("task_id")
            if task_id:
                await self.notifier.send_text(
                    chat_id, f"Это сообщение уже создало задачу #{int(task_id):03d}."
                )
            else:
                await self.notifier.send_text(chat_id, "Этот запрос уже обрабатывается.")
            return
        stored_project = await self.projects.get(int(captured["project_id"]))
        if stored_project is None:
            await self.requests.mark_status(str(captured["request_id"]), "failed")
            await self.notifier.send_text(chat_id, "Проект исходного запроса удалён.")
            return
        try:
            stored_conversation = await self.conversations.get(int(captured["conversation_id"]))
        except Exception:
            await self.requests.mark_status(str(captured["request_id"]), "failed")
            await self.notifier.send_text(chat_id, "Исходная беседа запроса удалена.")
            return
        stored_envelope = self.requests.envelope_from_row(captured)
        if stored_envelope.attachment_refs:
            await self.requests.mark_status(stored_envelope.request_id, "retryable")
            await self.notifier.send_text(
                chat_id,
                "Вложения исходного запроса не могут быть безопасно восстановлены; "
                "задача не создана.",
            )
            return
        try:
            task = await self._begin_task(
                project=stored_project,
                conversation_id=stored_conversation.id,
                chat_id=chat_id,
                title_hint=text.strip()[:120] or "Задача",
                request_text=stored_envelope.original_text,
                request_id=stored_envelope.request_id,
            )
        except Exception:
            await self.requests.mark_status(stored_envelope.request_id, "failed")
            raise
        if task is None:
            await self.requests.mark_status(stored_envelope.request_id, "rejected")

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
        if self.providers is not None:
            roles = self.providers.snapshot()["roles"]
            lines.append(
                "Управляющие модели: "
                + ", ".join(
                    f"{role}={data['provider']}/{data['model'] or '—'}"
                    for role, data in roles.items()
                )
            )
        else:
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
        lines = ["<b>Расход API</b>", ""]
        if task_id is not None:
            task = await self.tasks.get(task_id)
            totals = await self.usage.task_totals(task.id)
            lines += [f"<b>Задача #{task.id:03d}</b>", "", self.usage.format_totals(totals), ""]
            models = await self.usage.by_model(task_id=task.id)
            if models:
                lines.append("<b>Модели</b>")
                lines.append("")
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
        lines += [f"<b>{label.capitalize()}</b>", "", self.usage.format_totals(totals)]
        models = await self.usage.by_model(start=start, end=end)
        if models:
            lines += ["", "<b>Модели</b>", ""]
            for (provider, model), value in models.items():
                lines.append(f"• {provider}/{model}: {self.usage.format_totals(value)}")
        if project:
            provider_totals = await self.usage.by_provider(project_id=project.id)
            lines += ["", f"<b>Проект {project.name}</b>", ""]
            for provider, value in provider_totals.items():
                lines.append(f"• {provider}: {self.usage.format_totals(value)}")
        if not self.settings.usage.configured:
            lines += ["", "Цены не заданы в config.toml, стоимость не рассчитывается."]
        lines += [
            "",
            f"Лимит/сутки: токены {self.settings.limits.daily_token_limit or 'без лимита'}, "
            f"запросы {self.settings.limits.daily_request_limit or 'без лимита'}.",
        ]
        return "\n".join(lines)

    async def memory_text(self, chat_id: int, query: str) -> str:
        project = await self._current_project(chat_id)
        if query.strip():
            memories = await self.conversations.search_memories(query.strip())
            if not memories:
                return "Совпадений в памяти нет."
            import html

            return "\n".join(
                f"#{item.id} [{html.escape(item.scope)}/{html.escape(item.category)}"
                f"{' · устарело' if item.stale else ''}] "
                f"{html.escape(self.redactor.redact(item.content[:500]), quote=True)}"
                for item in memories
            )
        items = await self.conversations.get_memories(project_id=project.id if project else None)
        if not items:
            return "Память пуста. Добавить: /memory add <текст>"
        lines = ["<b>Память</b>", ""]
        for item in items[:20]:
            confirmed = "" if item.verified else " · неподтверждено"
            stale = " · устарело" if item.stale else ""
            lines += [
                f"<b>#{item.id} · {item.scope}/{item.category}</b>{confirmed}{stale}",
                "",
                __import__("html").escape(self.redactor.redact(item.content[:500]), quote=True),
                "",
            ]
        return "\n".join(lines).rstrip()

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
            f"Egress: {'enabled' if self.settings.egress.enabled else 'disabled'}; "
            f"hosts={','.join(self.settings.egress.allowed_hosts) or '(derived)'}"
        )
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
        if self.providers is not None:
            lines.append(
                "LLM roles: "
                + ", ".join(
                    f"{role}={data['provider']}/{data['model'] or '—'}"
                    for role, data in self.providers.snapshot()["roles"].items()
                )
            )
        else:
            lines.append(
                f"Kimi: {self.settings.kimi.model} (key configured: {bool(self.settings.kimi_key)})"
            )
        lines.append(
            f"DeepSeek: {self.settings.deepseek_model} (key configured: {bool(self.settings.deepseek_key)})"
        )
        lines.append(f"Telegram owner: {self.settings.owner_id}")
        if self.github is not None:
            try:
                user = await self.github.authenticated_user()
                login = str(user.get("login") or "")
                lines.append(f"GitHub auth: @{login}" if login else "GitHub auth: token accepted")
            except GitHubError as exc:
                lines.append(f"GitHub auth: rejected/insufficient ({type(exc).__name__})")
        else:
            lines.append("GitHub auth: not configured")
        lines.append(f"Project: {project.name if project else 'none'}")
        lines.append(
            "CLI `ai-supervisor doctor` additionally checks configured providers, Telegram getMe and bwrap."
        )
        return "\n".join(lines)

    async def settings_text(self, chat_id: int) -> str:
        summary = self.settings.public_summary()
        lines = ["<b>Настройки</b>", "", "Секреты скрыты.", ""]
        lines += [
            "<b>Модели</b>",
            "",
            *[
                f"{role}: {data['provider']} / {data['model'] or 'недоступна'}"
                for role, data in (
                    self.providers.snapshot()["roles"].items() if self.providers is not None else []
                )
            ],
            "",
            "<b>Параметры</b>",
            "",
            json.dumps(summary, ensure_ascii=False, indent=2)[:2500],
        ]
        return "\n".join(lines)

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

    async def send_compact(self, task_id: int, chat_id: int) -> None:
        await self.progress.update(task_id, force=True, chat_id=chat_id)

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
        if not task.report:
            await self.notifier.send_text(chat_id, "Отчёт ещё не сформирован.")
            return
        mode = await self.conversations.get_ui_mode(chat_id)
        if mode == "compact":
            project = await self.projects.get(task.project_id)
            stages = await self.stages.list(task.id)
            evidence = self._load_evidence(task)
            usage_totals = await self.usage.by_provider(task_id=task.id)
            model_requests = sum(
                int(getattr(totals, "requests", 0)) for totals in usage_totals.values()
            )
            await self.notifier.send_text(
                chat_id,
                completion_card(
                    task,
                    project,
                    evidence=evidence,
                    review=task.review,
                    stages=stages,
                    model_requests=model_requests,
                    mode=mode,
                    repo_url=project.remote_url if project else None,
                ),
                [[Button("Подробности", f"det:{task.id}")]],
                parse_mode="HTML",
            )
            return
        await self._send_long(chat_id, task.report)

    async def send_details(self, task_id: int, chat_id: int) -> None:
        task = await self.tasks.get(task_id)
        project = await self.projects.get(task.project_id)
        stages = await self.stages.list(task.id)
        mode = await self.conversations.get_ui_mode(chat_id)
        text = details_view(
            task,
            project,
            self._spec_for(task),
            stages,
            self._load_evidence(task),
            mode=mode,
        )
        await self.notifier.send_text(
            chat_id,
            text,
            [
                [Button("Коротко", f"compact:{task.id}"), Button("Отчёт", f"rep:{task.id}")],
                [Button("Журнал", f"log:{task.id}")],
            ],
            parse_mode="HTML",
        )

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
