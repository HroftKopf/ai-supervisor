from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from ai_supervisor.config import Settings
from ai_supervisor.core.interfaces import Button, Notifier
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.projects import ProjectService
from ai_supervisor.memory.stages import TaskStageService
from ai_supervisor.memory.tasks import TaskService
from ai_supervisor.models import TaskRecord, TaskStatus
from ai_supervisor.presentation.renderer import progress_card, task_card

logger = logging.getLogger(__name__)


class ProgressManager:
    """Edits one stable Telegram message per task and throttles frequent updates."""

    def __init__(
        self,
        *,
        settings: Settings,
        tasks: TaskService,
        projects: ProjectService,
        conversations: ConversationService,
        stages: TaskStageService,
        notifier: Notifier,
        redactor: SecretRedactor,
        throttle_seconds: float = 1.2,
    ) -> None:
        self.settings = settings
        self.tasks = tasks
        self.projects = projects
        self.conversations = conversations
        self.stages = stages
        self.notifier = notifier
        self.redactor = redactor
        self.throttle_seconds = throttle_seconds
        self._locks: dict[int, asyncio.Lock] = {}
        self._last: dict[int, float] = {}
        self._pending: dict[int, asyncio.Task[Any]] = {}

    async def close(self) -> None:
        for job in list(self._pending.values()):
            job.cancel()
        if self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()

    async def restore_active(self, chat_id: int) -> None:
        tasks = await self.tasks.active()
        for task in tasks:
            try:
                await self.update(task.id, force=True, chat_id=chat_id)
            except Exception as exc:
                logger.warning("cannot restore progress for task #%s: %s", task.id, exc)

    async def update(
        self,
        task_id: int,
        *,
        force: bool = False,
        chat_id: int | None = None,
    ) -> bool:
        if not force and self._is_throttled(task_id):
            self._schedule(task_id, chat_id)
            return False
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            task = await self.tasks.get_optional(task_id)
            if task is None:
                return False
            project = await self.projects.get(task.project_id)
            stages = await self.stages.list(task.id)
            mode = await self.conversations.get_ui_mode(chat_id or self._owner_chat(task))
            text = (
                progress_card(task, project, stages, mode=mode)
                if stages
                else task_card(task, project, mode=mode)
            )
            if task.status is TaskStatus.COMPLETED:
                buttons = [
                    [
                        Button("Подробности", f"det:{task.id}"),
                        Button("Расход токенов", f"use:{task.id}"),
                    ],
                    [Button("Журнал", f"log:{task.id}")],
                ]
            elif task.status in {TaskStatus.BLOCKED, TaskStatus.WAITING_INPUT, TaskStatus.FAILED}:
                buttons = [
                    [
                        Button("Подробности", f"det:{task.id}"),
                        Button("Повторить", f"res:{task.id}"),
                    ],
                    [Button("Отменить", f"stop:{task.id}")],
                ]
            else:
                buttons = [
                    [Button("Подробности", f"det:{task.id}"), Button("Отменить", f"stop:{task.id}")]
                ]
            digest = hashlib.sha256(
                (text + repr([(row.text, row.callback_data) for row in buttons[0]])).encode()
            ).hexdigest()
            if task.ui_hash == digest and task.ui_message_id is not None and not force:
                self._last[task_id] = time.monotonic()
                return True
            target_chat = task.ui_chat_id or chat_id or self._owner_chat(task)
            sent = False
            if task.ui_message_id is not None:
                sent = await self.notifier.edit_text(
                    target_chat,
                    task.ui_message_id,
                    text,
                    buttons,
                    parse_mode="HTML",
                )
            if not sent:
                message_id = await self.notifier.send_text(
                    target_chat, text, buttons, parse_mode="HTML"
                )
                if message_id is None:
                    return False
                task = await self.tasks.update_fields(
                    task.id, ui_chat_id=target_chat, ui_message_id=message_id
                )
            task = await self.tasks.update_fields(
                task.id,
                ui_chat_id=target_chat,
                ui_message_id=task.ui_message_id,
                ui_hash=digest,
                ui_updated_at=datetime.now(UTC),
            )
            self._last[task_id] = time.monotonic()
            return True

    def _owner_chat(self, task: TaskRecord) -> int:
        # All tasks are owner-only; settings.owner_id is the canonical chat.
        return self.settings.owner_id

    def _is_throttled(self, task_id: int) -> bool:
        last = self._last.get(task_id, 0.0)
        return (time.monotonic() - last) < self.throttle_seconds

    def _schedule(self, task_id: int, chat_id: int | None) -> None:
        existing = self._pending.get(task_id)
        if existing is not None and not existing.done():
            return

        async def delayed() -> None:
            try:
                await asyncio.sleep(self.throttle_seconds)
                await self.update(task_id, force=True, chat_id=chat_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("throttled progress update failed: %s", exc)
            finally:
                self._pending.pop(task_id, None)

        self._pending[task_id] = asyncio.create_task(delayed(), name=f"progress-{task_id}")
