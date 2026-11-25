from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ai_supervisor.config import Settings
from ai_supervisor.core.interfaces import Attachment, IncomingMessage
from ai_supervisor.core.orchestrator import Orchestrator
from ai_supervisor.memory.projects import ProjectError, ProjectService
from ai_supervisor.models import TaskRecord
from ai_supervisor.telegram.auth import OwnerMiddleware
from ai_supervisor.telegram.notifications import BotNotifier

logger = logging.getLogger(__name__)

HELP_TEXT = """AI Supervisor — команды

/start — начать и показать состояние
/help — эта справка
/projects — список проектов
/project — текущий проект; /project use|add|create|remove|test|build|instructions
/new — начать новый диалог
/tasks — последние задачи
/status [id] — состояние системы или задачи
/agents — активные исполнители
/logs [id] — журнал задачи
/diff [id] — git diff задачи
/report [id] — отчёт по задаче
/approve [id] — подтвердить ожидающее действие
/reject [id] — отклонить подтверждение
/stop [id] — остановить задачу
/resume [id] — продолжить приостановленную задачу
/rollback [id] — откатить изменения задачи (с подтверждением)
/rework [id] — отправить ожидающую задачу на доработку
/usage — расход токенов
/memory [query] — память; /memory add <текст>, /memory delete <id>
/settings — настройки без секретов

Можно писать обычным языком: Kimi сам решит, обсуждать идею или запускать работу.
"""


class Handlers:
    def __init__(
        self,
        *,
        settings: Settings,
        orchestrator: Orchestrator,
        projects: ProjectService,
        notifier: BotNotifier,
    ) -> None:
        self.settings = settings
        self.orchestrator = orchestrator
        self.projects = projects
        self.notifier = notifier
        self.router = Router(name="ai-supervisor-owner")
        self.router.message.outer_middleware(OwnerMiddleware(settings))
        self.router.callback_query.outer_middleware(OwnerMiddleware(settings))
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._register()

    # ------------------------------------------------------------------
    # routing
    # ------------------------------------------------------------------

    def _register(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_help, Command("help"))
        r.message.register(self.cmd_projects, Command("projects"))
        r.message.register(self.cmd_project, Command("project"))
        r.message.register(self.cmd_new, Command("new"))
        r.message.register(self.cmd_tasks, Command("tasks"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_agents, Command("agents"))
        r.message.register(self.cmd_logs, Command("logs"))
        r.message.register(self.cmd_diff, Command("diff"))
        r.message.register(self.cmd_report, Command("report"))
        r.message.register(self.cmd_approve, Command("approve"))
        r.message.register(self.cmd_reject, Command("reject"))
        r.message.register(self.cmd_stop, Command("stop"))
        r.message.register(self.cmd_resume, Command("resume"))
        r.message.register(self.cmd_rollback, Command("rollback"))
        r.message.register(self.cmd_rework, Command("rework"))
        r.message.register(self.cmd_usage, Command("usage"))
        r.message.register(self.cmd_memory, Command("memory"))
        r.message.register(self.cmd_settings, Command("settings"))
        r.message.register(self.cmd_unknown, F.text.startswith("/"))
        r.message.register(self.on_document, F.document)
        r.message.register(self.on_photo, F.photo)
        r.message.register(self.on_text, F.text)
        r.message.register(self.on_unsupported)
        r.callback_query.register(self.on_callback)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    async def cmd_start(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id,
            "AI Supervisor запущен.\n\n"
            + await self.orchestrator.status_text(message.chat.id)
            + "\n\n"
            + HELP_TEXT,
        )

    async def cmd_help(self, message: Message) -> None:
        await self.notifier.send_text(message.chat.id, HELP_TEXT)

    async def cmd_projects(self, message: Message) -> None:
        projects = await self.projects.list()
        if not projects:
            await self.notifier.send_text(
                message.chat.id,
                "Проектов нет. Добавьте: /project add <имя> <абсолютный путь>\n"
                "или создайте: /project create <имя>",
            )
            return
        current = await self.orchestrator.current_project(message.chat.id)
        lines = ["Проекты:"]
        for project in projects:
            marker = " ← текущий" if current and current.id == project.id else ""
            lines.append(f"• {project.name} — {project.path}{marker}")
        await self.notifier.send_text(message.chat.id, "\n".join(lines))

    async def cmd_project(self, message: Message) -> None:
        text = _command_tail(message.text or "")
        if not text:
            await self._project_info(message.chat.id)
            return
        parts = text.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if sub == "use":
            await self._project_use(message.chat.id, rest)
        elif sub == "add":
            await self._project_add(message.chat.id, rest)
        elif sub == "create":
            await self._project_create(message.chat.id, rest)
        elif sub == "remove":
            await self._project_remove(message.chat.id, rest)
        elif sub in {"test", "build", "instructions", "desc", "stack", "check"}:
            await self._project_update(message.chat.id, sub, rest)
        else:
            await self.notifier.send_text(
                message.chat.id,
                "Использование: /project [use|add|create|remove|test|build|instructions|check]",
            )

    async def _project_info(self, chat_id: int) -> None:
        project = await self.orchestrator.current_project(chat_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Проект не выбран. /project add <имя> <путь>")
            return
        await self.notifier.send_text(
            chat_id,
            f"Проект: {project.name}\n"
            f"Путь: {project.path}\n"
            f"Стек: {project.stack or '(не указан)'}\n"
            f"Описание: {project.description or '(нет)'}\n"
            f"Базовая ветка: {project.base_branch}\n"
            f"Тестовая команда: {project.test_command or '(не настроена)'}\n"
            f"Сборочная команда: {project.build_command or '(не настроена)'}\n"
            f"Инструкции: {project.instructions[:1000] or '(нет)'}",
        )

    async def _project_use(self, chat_id: int, name: str) -> None:
        if not name.strip():
            await self.notifier.send_text(chat_id, "Использование: /project use <имя>")
            return
        project = await self.projects.get_by_name(name.strip())
        if project is None:
            await self.notifier.send_text(chat_id, "Проект не найден.")
            return
        await self.orchestrator.conversations.set_active_project(chat_id, project.id)
        await self.notifier.send_text(chat_id, f"Текущий проект: {project.name}")

    async def _project_add(self, chat_id: int, rest: str) -> None:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            await self.notifier.send_text(
                chat_id, "Использование: /project add <имя> <абсолютный путь>"
            )
            return
        name, path = parts
        try:
            project = await self.projects.add(name, path)
        except ProjectError as exc:
            await self.notifier.send_text(chat_id, f"Не удалось добавить проект: {exc}")
            return
        await self.orchestrator.conversations.set_active_project(chat_id, project.id)
        await self.notifier.send_text(
            chat_id,
            f"Проект {project.name} зарегистрирован.\nПуть: {project.path}\n"
            "Проверки не настроены; добавьте: /project test <команда>, /project build <команда>.",
        )

    async def _project_create(self, chat_id: int, rest: str) -> None:
        parts = rest.split(maxsplit=2)
        if not parts or not parts[0].strip():
            await self.notifier.send_text(chat_id, "Использование: /project create <имя> [стек]")
            return
        name = parts[0]
        stack = " ".join(parts[1:]).strip()
        try:
            project = await self.projects.create(name, stack=stack)
        except ProjectError as exc:
            await self.notifier.send_text(chat_id, f"Не удалось создать проект: {exc}")
            return
        await self.orchestrator.conversations.set_active_project(chat_id, project.id)
        await self.notifier.send_text(
            chat_id, f"Проект {project.name} создан: {project.path}\nНачальный коммит сделан."
        )

    async def _project_remove(self, chat_id: int, rest: str) -> None:
        name = rest.strip()
        if not name:
            await self.notifier.send_text(chat_id, "Использование: /project remove <имя>")
            return
        project = await self.projects.get_by_name(name)
        if project is None:
            await self.notifier.send_text(chat_id, "Проект не найден.")
            return
        try:
            await self.projects.remove(project.id)
        except ProjectError as exc:
            await self.notifier.send_text(chat_id, f"Не удалось удалить проект: {exc}")
            return
        await self.orchestrator.conversations.set_active_project(chat_id, None)
        await self.notifier.send_text(
            chat_id,
            f"Проект {project.name} удалён из реестра. Файлы на диске не изменялись.",
        )

    async def _project_update(self, chat_id: int, sub: str, rest: str) -> None:
        project = await self.orchestrator.current_project(chat_id)
        if project is None:
            await self.notifier.send_text(chat_id, "Проект не выбран.")
            return
        try:
            if sub == "check":
                await self.projects.validate_repository(Path(project.path))
                await self.notifier.send_text(chat_id, "Проект доступен, Git-репозиторий валиден.")
                return
            if sub == "test":
                updated = await self.projects.update_instructions(project.id, test_command=rest)
            elif sub == "build":
                updated = await self.projects.update_instructions(project.id, build_command=rest)
            elif sub == "instructions":
                updated = await self.projects.update_instructions(project.id, instructions=rest)
            elif sub == "desc":
                updated = await self.projects.update_instructions(project.id, description=rest)
            else:
                updated = await self.projects.update_instructions(project.id, stack=rest)
        except ProjectError as exc:
            await self.notifier.send_text(chat_id, f"Не удалось обновить проект: {exc}")
            return
        await self.notifier.send_text(chat_id, f"Проект {updated.name} обновлён.")

    async def cmd_new(self, message: Message) -> None:
        project = await self.orchestrator.current_project(message.chat.id)
        conversation = await self.orchestrator.conversations.new_conversation(
            message.chat.id, project.id if project else None
        )
        await self.notifier.send_text(
            message.chat.id, f"Начат новый диалог (id {conversation.id}). История сохранена."
        )

    async def cmd_tasks(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.tasks_text(message.chat.id)
        )

    async def cmd_status(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        if arg:
            task = await self._task_or_reply(message.chat.id, arg)
            if task is None:
                return
            await self.notifier.send_text(message.chat.id, self._task_details(task))
        else:
            await self.notifier.send_text(
                message.chat.id, await self.orchestrator.status_text(message.chat.id)
            )

    async def cmd_agents(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.agents_text(message.chat.id)
        )

    async def cmd_logs(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.send_logs(task.id, message.chat.id)

    async def cmd_diff(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.send_diff(task.id, message.chat.id)

    async def cmd_report(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.send_report(task.id, message.chat.id)

    async def cmd_approve(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        if arg.isdigit():
            await self.orchestrator.approve_action(int(arg), message.from_user.id, message.chat.id)
        else:
            await self.orchestrator.approve_latest(message.from_user.id, message.chat.id)

    async def cmd_reject(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        if arg.isdigit():
            await self.orchestrator.reject_action(int(arg), message.from_user.id, message.chat.id)
        else:
            await self.orchestrator.reject_latest(message.from_user.id, message.chat.id)

    async def cmd_stop(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        await self.orchestrator.stop_latest_or(message.chat.id, int(arg) if arg.isdigit() else None)

    async def cmd_resume(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        await self.orchestrator.resume_by_id(
            message.chat.id, int(arg) if arg.isdigit() else None, message.from_user.id
        )

    async def cmd_rollback(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.request_rollback(task.id, message.from_user.id, message.chat.id)

    async def cmd_rework(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.rework_task(task.id, message.from_user.id, message.chat.id)

    async def cmd_usage(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.usage_text(message.chat.id)
        )

    async def cmd_memory(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        if arg.startswith("add "):
            content = arg[4:].strip()
            if not content:
                await self.notifier.send_text(message.chat.id, "Использование: /memory add <текст>")
                return
            project = await self.orchestrator.current_project(message.chat.id)
            memory = await self.orchestrator.conversations.add_memory(
                "project" if project else "user",
                self.orchestrator.redactor.redact(content),
                project_id=project.id if project else None,
                source="telegram",
                user_confirmed=True,
            )
            await self.notifier.send_text(message.chat.id, f"Сохранено в памяти #{memory.id}.")
            return
        if arg.startswith("delete "):
            ident = arg.split(maxsplit=1)[1]
            if not ident.isdigit():
                await self.notifier.send_text(message.chat.id, "Использование: /memory delete <id>")
                return
            deleted = await self.orchestrator.conversations.delete_memory(int(ident))
            await self.notifier.send_text(
                message.chat.id, "Удалено." if deleted else "Запись не найдена."
            )
            return
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.memory_text(message.chat.id, arg)
        )

    async def cmd_settings(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.settings_text(message.chat.id)
        )

    async def cmd_unknown(self, message: Message) -> None:
        await self.notifier.send_text(message.chat.id, "Неизвестная команда.\n\n" + HELP_TEXT)

    # ------------------------------------------------------------------
    # natural language / attachments
    # ------------------------------------------------------------------

    async def on_text(self, message: Message) -> None:
        text = message.text or ""
        if message.reply_to_message and message.reply_to_message.text:
            quoted = message.reply_to_message.text[:1200]
            text = f"[В ответ на сообщение]\n{quoted}\n\n{text}"
        await self._process(message, text=text, attachments=[])

    async def on_photo(self, message: Message) -> None:
        photo = message.photo[-1] if message.photo else None
        if photo is None:
            return
        buffer = io.BytesIO()
        await message.bot.download(photo, destination=buffer)
        await self._process(
            message,
            text=message.caption or "",
            attachments=[
                Attachment(
                    filename=f"photo-{photo.file_unique_id}.jpg",
                    mime_type="image/jpeg",
                    data=buffer.getvalue(),
                    is_image=True,
                )
            ],
        )

    async def on_document(self, message: Message) -> None:
        document = message.document
        if document is None:
            return
        if document.file_size and document.file_size > 20 * 1024 * 1024:
            await self.notifier.send_text(message.chat.id, "Файл больше 20 МБ не принимается.")
            return
        buffer = io.BytesIO()
        await message.bot.download(document, destination=buffer)
        mime = (
            document.mime_type
            or mimetypes.guess_type(document.file_name or "")[0]
            or "application/octet-stream"
        )
        await self._process(
            message,
            text=message.caption or "",
            attachments=[
                Attachment(
                    filename=document.file_name or f"document-{document.file_unique_id}",
                    mime_type=mime,
                    data=buffer.getvalue(),
                    is_image=mime in {"image/jpeg", "image/png", "image/webp"},
                )
            ],
        )

    async def on_unsupported(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id,
            "Поддерживаются текст, изображения JPEG/PNG/WebP и текстовые файлы.",
        )

    async def _process(self, message: Message, *, text: str, attachments: list[Attachment]) -> None:
        if message.from_user is None:
            return
        lock = self._chat_locks.setdefault(message.chat.id, asyncio.Lock())
        async with lock:
            await message.bot.send_chat_action(message.chat.id, "typing")
            typing = asyncio.create_task(self._typing_loop(message.chat.id))
            try:
                await self.orchestrator.handle_incoming(
                    IncomingMessage(
                        chat_id=message.chat.id,
                        user_id=message.from_user.id,
                        text=text,
                        telegram_message_id=message.message_id,
                        reply_to_message_id=message.reply_to_message.message_id
                        if message.reply_to_message
                        else None,
                        attachments=attachments,
                    )
                )
            except Exception as exc:
                logger.exception("incoming message failed")
                await self.notifier.send_text(
                    message.chat.id,
                    "Ошибка обработки сообщения: "
                    + self.orchestrator.redactor.redact(f"{type(exc).__name__}: {exc}")[:500],
                )
            finally:
                typing.cancel()
                try:
                    await typing
                except asyncio.CancelledError:
                    pass

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(4)
                await self.notifier.bot.send_chat_action(chat_id, "typing")
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------

    async def on_callback(self, callback: CallbackQuery) -> None:
        data = callback.data or ""
        action, _, arg = data.partition(":")
        if callback.from_user is None or callback.message is None:
            await callback.answer()
            return
        chat_id = callback.message.chat.id
        user_id = callback.from_user.id
        await callback.answer("Принято")
        task_id = int(arg) if arg.isdigit() else None
        try:
            if action == "acc" and task_id is not None:
                await self.orchestrator.accept_task(task_id, user_id, chat_id)
            elif action == "rew" and task_id is not None:
                await self.orchestrator.rework_task(task_id, user_id, chat_id)
            elif action == "dif" and task_id is not None:
                await self.orchestrator.send_diff(task_id, chat_id)
            elif action == "log" and task_id is not None:
                await self.orchestrator.send_logs(task_id, chat_id)
            elif action == "rb" and task_id is not None:
                await self.orchestrator.request_rollback(task_id, user_id, chat_id)
            elif action == "stop" and task_id is not None:
                await self.orchestrator.stop_task(task_id, chat_id)
            elif action == "res" and task_id is not None:
                await self.orchestrator.resume_task(task_id, user_id, chat_id)
            elif action == "apv" and task_id is not None:
                await self.orchestrator.approve_action(task_id, user_id, chat_id)
            elif action == "rej" and task_id is not None:
                await self.orchestrator.reject_action(task_id, user_id, chat_id)
            else:
                await self.notifier.send_text(chat_id, "Неизвестное действие кнопки.")
        except Exception as exc:
            logger.exception("callback failed")
            await self.notifier.send_text(
                chat_id,
                "Ошибка действия: "
                + self.orchestrator.redactor.redact(f"{type(exc).__name__}: {exc}")[:400],
            )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _task_from_message(self, message: Message) -> TaskRecord | None:
        arg = _command_tail(message.text or "")
        if arg and not arg.isdigit():
            await self.notifier.send_text(message.chat.id, "ID задачи должен быть числом.")
            return None
        if arg.isdigit():
            task = await self.orchestrator.tasks.get_optional(int(arg))
            if task is None:
                await self.notifier.send_text(message.chat.id, "Задача не найдена.")
            return task
        task = await self.orchestrator.latest_task(message.chat.id)
        if task is None:
            await self.notifier.send_text(message.chat.id, "В текущем проекте задач нет.")
        return task

    async def _task_or_reply(self, chat_id: int, arg: str) -> TaskRecord | None:
        if not arg.isdigit():
            await self.notifier.send_text(chat_id, "ID задачи должен быть числом.")
            return None
        task = await self.orchestrator.tasks.get_optional(int(arg))
        if task is None:
            await self.notifier.send_text(chat_id, "Задача не найдена.")
        return task

    def _time(self, value) -> str:  # type: ignore[no-untyped-def]
        if value is None:
            return "—"
        return value.astimezone(ZoneInfo(self.settings.app.timezone)).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )

    def _task_details(self, task: TaskRecord) -> str:
        spec = task.spec
        review = task.review
        lines = [
            f"Задача #{task.id:03d} [{task.status.value}]",
            task.title,
            f"Создана: {self._time(task.created_at)}",
            f"Начата: {self._time(task.started_at)}",
            f"Завершена: {self._time(task.finished_at)}",
            f"Ветка: {task.branch or '—'}",
            f"Worktree: {task.worktree_path or '—'}",
            f"Доработок: {task.rework_count}/{task.max_rework}",
        ]
        if spec:
            lines.append(f"Цель: {spec.goal[:500]}")
        if review:
            lines.append(f"Ревью: {review.verdict.value} — {review.summary[:800]}")
        if task.error:
            lines.append(f"Ошибка: {task.error[:800]}")
        return self.orchestrator.redactor.redact("\n".join(lines))


def _command_tail(text: str) -> str:
    return text.split(maxsplit=1)[1].strip() if " " in text else ""
