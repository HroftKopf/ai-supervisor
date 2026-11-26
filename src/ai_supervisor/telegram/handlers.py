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
from ai_supervisor.core.interfaces import Attachment, Button, IncomingMessage
from ai_supervisor.core.orchestrator import Orchestrator
from ai_supervisor.memory.projects import ProjectError, ProjectService
from ai_supervisor.models import ProjectPolicy, TaskRecord
from ai_supervisor.presentation.renderer import main_menu, settings_card
from ai_supervisor.telegram.auth import OwnerMiddleware
from ai_supervisor.telegram.notifications import BotNotifier

logger = logging.getLogger(__name__)

HELP_TEXT = """AI Supervisor — команды

/start — начать и показать состояние
/help — эта справка
/projects — список проектов
/project — текущий проект; /project list|select|add|create|remove|info|permissions|test|build|instructions|import-config
/new — начать новый диалог
/tasks — последние задачи
/chat <текст> — обычный ответ без классификатора
/task <текст> — создать задачу без классификатора
/auto [текст] — автоматический выбор chat/task
/task_info [id] — подробности задачи
/queue — очередь и активные задачи
/status [id] — состояние системы или задачи
/agents — активные исполнители
/logs [id] — журнал задачи
/diff [id] — git diff задачи
/report [id] — отчёт по задаче
/approve [id] — подтвердить ожидающее действие
/reject [id] — отклонить подтверждение
/stop [id] — остановить задачу
/cancel [id] — отменить задачу (алиас /stop)
/pause [id] — безопасно приостановить
/resume [id] — продолжить приостановленную задачу
/retry [id] — повторить неудачную/заблокированную задачу
/rollback [id] — откатить изменения задачи (с подтверждением)
/push [id] — опубликовать завершённый Git-репозиторий (с подтверждением)
/rework [id] — отправить ожидающую задачу на доработку
/usage [today|week|month|task <id>] — расход токенов
/memory [query] — память; /memory add <текст>, /memory edit <id> <текст>, /memory stale <id> on|off, /memory delete <id>
/models — управляющие модели по ролям
/provider [info|test [role]|recover <provider-or-role>|select <role> <provider>] — провайдеры без секретов
/settings — настройки без секретов
/doctor — быстрая диагностика
/github info <owner/repo> — информация о репозитории
/github create <name> [private|public] — создать репозиторий (с подтверждением)

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
        r.message.register(self.cmd_chat, Command("chat"))
        r.message.register(self.cmd_auto, Command("auto"))
        r.message.register(self.cmd_task, Command("task"))
        r.message.register(self.cmd_request, Command("request"))
        r.message.register(self.cmd_task_info, Command("task_info"))
        r.message.register(self.cmd_queue, Command("queue"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_agents, Command("agents"))
        r.message.register(self.cmd_logs, Command("logs"))
        r.message.register(self.cmd_diff, Command("diff"))
        r.message.register(self.cmd_report, Command("report"))
        r.message.register(self.cmd_approve, Command("approve"))
        r.message.register(self.cmd_reject, Command("reject"))
        r.message.register(self.cmd_stop, Command("stop"))
        r.message.register(self.cmd_cancel, Command("cancel"))
        r.message.register(self.cmd_pause, Command("pause"))
        r.message.register(self.cmd_resume, Command("resume"))
        r.message.register(self.cmd_retry, Command("retry"))
        r.message.register(self.cmd_rollback, Command("rollback"))
        r.message.register(self.cmd_push, Command("push"))
        r.message.register(self.cmd_rework, Command("rework"))
        r.message.register(self.cmd_usage, Command("usage"))
        r.message.register(self.cmd_memory, Command("memory"))
        r.message.register(self.cmd_models, Command("models"))
        r.message.register(self.cmd_provider, Command("provider"))
        r.message.register(self.cmd_settings, Command("settings"))
        r.message.register(self.cmd_doctor, Command("doctor"))
        r.message.register(self.cmd_github, Command("github"))
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
        project = await self.orchestrator.current_project(message.chat.id)
        active = await self.orchestrator.tasks.active(project.id if project else None)
        mode = await self.orchestrator.conversations.get_ui_mode(message.chat.id)
        await self.notifier.send_text(
            message.chat.id,
            main_menu(
                project_name=project.name if project else None,
                active=len(active),
                mode=mode,
            ),
            [
                [Button("Проекты", "menu:projects"), Button("Задачи", "menu:tasks")],
                [Button("История", "menu:history"), Button("API", "menu:usage")],
                [Button("Состояние", "menu:status"), Button("Настройки", "menu:settings")],
            ],
            parse_mode="HTML",
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
        if sub in {"use", "select"}:
            await self._project_use(message.chat.id, rest)
        elif sub == "list":
            await self.cmd_projects(message)
        elif sub == "add":
            await self._project_add(message.chat.id, rest)
        elif sub == "create":
            await self._project_create(message.chat.id, rest)
        elif sub == "remove":
            await self._project_remove(message.chat.id, rest)
        elif sub == "import-config":
            await self._project_import_config(message.chat.id, message.from_user.id, rest)
        elif sub in {
            "test",
            "build",
            "instructions",
            "desc",
            "stack",
            "check",
            "permissions",
            "remote",
            "budget",
            "max-rework",
            "extra-read",
            "permission",
        }:
            await self._project_update(message.chat.id, sub, rest)
        else:
            await self.notifier.send_text(
                message.chat.id,
                "Использование: /project [use|add|create|remove|test|build|instructions|check]",
            )

    async def _project_import_config(self, chat_id: int, user_id: int, rest: str) -> None:
        import shlex

        try:
            parts = shlex.split(rest)
        except ValueError as exc:
            await self.notifier.send_text(chat_id, f"Некорректные аргументы: {exc}")
            return
        overwrite = "--overwrite" in parts
        parts = [part for part in parts if part != "--overwrite"]
        if len(parts) != 2:
            await self.notifier.send_text(
                chat_id,
                "Использование: /project import-config <source> <relative-destination> [--overwrite]",
            )
            return
        await self.orchestrator.request_config_import(
            chat_id, user_id, parts[0], parts[1], overwrite
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
            f"Remote: {project.remote_url or '(не задан)'}\n"
            f"Бюджет токенов: {project.token_budget or 'не задан'}\n"
            f"Max rework: {project.max_rework}\n"
            f"Доп. пути чтения: {project.extra_read_paths_json}\n"
            f"Policy: {project.policy_json}\n"
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
            if sub == "permissions":
                await self.notifier.send_text(
                    chat_id,
                    f"Политика проекта {project.name}:\n{project.policy_json}\n"
                    f"Remote: {project.remote_url or '(не задан)'}\n"
                    f"Бюджет: {project.token_budget or 'не задан'}\n"
                    f"Max rework: {project.max_rework}",
                )
                return
            if sub == "remote":
                updated = await self.projects.update_instructions(
                    project.id, remote_url=rest.strip()
                )
            elif sub == "budget":
                if not rest.strip().isdigit():
                    raise ProjectError("бюджет должен быть числом токенов")
                updated = await self.projects.update_instructions(
                    project.id, token_budget=int(rest.strip())
                )
            elif sub == "max-rework":
                if not rest.strip().isdigit():
                    raise ProjectError("max-rework должен быть числом")
                updated = await self.projects.update_instructions(
                    project.id, max_rework=int(rest.strip())
                )
            elif sub == "extra-read":
                paths = [item.strip() for item in rest.split(",") if item.strip()]
                updated = await self.projects.update_instructions(
                    project.id, extra_read_paths=paths
                )
            elif sub == "permission":
                parts = rest.split()
                if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                    raise ProjectError(
                        "использование: /project permission <allow_git_push|allow_remote_change|"
                        "allow_system_changes|allow_config_import> on|off"
                    )
                policy = ProjectPolicy.model_validate_json(project.policy_json)
                key = parts[0]
                if key not in {
                    "allow_git_push",
                    "allow_remote_change",
                    "allow_system_changes",
                    "allow_config_import",
                }:
                    raise ProjectError(f"неизвестная политика: {key}")
                values = policy.model_dump()
                values[key] = parts[1].lower() == "on"
                updated = await self.projects.update_instructions(
                    project.id, policy=ProjectPolicy.model_validate(values)
                )
            elif sub == "test":
                updated = await self.projects.update_instructions(project.id, test_command=rest)
            elif sub == "build":
                updated = await self.projects.update_instructions(project.id, build_command=rest)
            elif sub == "instructions":
                updated = await self.projects.update_instructions(project.id, instructions=rest)
            elif sub == "desc":
                updated = await self.projects.update_instructions(project.id, description=rest)
            else:
                updated = await self.projects.update_instructions(project.id, stack=rest)
        except (ProjectError, ValueError) as exc:
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

    async def cmd_chat(self, message: Message) -> None:
        text = _command_tail(message.text or "").strip()
        if not text:
            await self.notifier.send_text(message.chat.id, "Использование: /chat <запрос>")
            return
        await self.orchestrator.chat_direct(message.chat.id, message.from_user.id, text)  # type: ignore[union-attr]

    async def cmd_auto(self, message: Message) -> None:
        text = _command_tail(message.text or "").strip()
        if text:
            await self.orchestrator.handle_incoming(
                IncomingMessage(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,  # type: ignore[union-attr]
                    text=text,
                    telegram_message_id=getattr(message, "message_id", None),
                )
            )
            return
        await self.notifier.send_text(
            message.chat.id,
            "Режим auto включён для следующих сообщений: классификатор выберет chat или task.",
        )

    async def cmd_task(self, message: Message) -> None:
        text = _command_tail(message.text or "")
        if not text:
            await self.notifier.send_text(message.chat.id, "Использование: /task <текст задачи>")
            return
        await self.orchestrator.create_task_from_text(
            message.chat.id,
            message.from_user.id,
            text,
            source_message_id=getattr(message, "message_id", None),
            idempotency_key=(
                f"telegram:{message.chat.id}:{getattr(message, 'message_id', None)}:task"
                if getattr(message, "message_id", None) is not None
                else None
            ),
        )

    async def cmd_request(self, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 4 or parts[1] != "retry":
            await self.notifier.send_text(
                message.chat.id, "Использование: /request retry <request_id>"
            )
            return
        await self.orchestrator.retry_request(
            parts[3],
            message.from_user.id,
            message.chat.id,  # type: ignore[union-attr]
        )

    async def cmd_task_info(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.send_details(task.id, message.chat.id)

    async def cmd_queue(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.queue_text(message.chat.id)
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
        arg = _command_tail(message.text or "")
        detailed = arg.lower() in {"full", "detailed", "подробно"}
        if detailed:
            arg = ""
        task = (
            await self.orchestrator.tasks.get_optional(int(arg))
            if arg.isdigit()
            else await self.orchestrator.latest_task(message.chat.id)
        )
        if task is None:
            await self.notifier.send_text(message.chat.id, "Задача не найдена.")
            return
        if detailed:
            await self.orchestrator.send_detailed_report(task.id, message.chat.id)
        else:
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

    async def cmd_cancel(self, message: Message) -> None:
        await self.cmd_stop(message)

    async def cmd_pause(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        if arg and not arg.isdigit():
            await self.notifier.send_text(message.chat.id, "ID задачи должен быть числом.")
            return
        task = (
            await self.orchestrator.tasks.get_optional(int(arg))
            if arg.isdigit()
            else await self.orchestrator.latest_task(message.chat.id)
        )
        if task is None:
            await self.notifier.send_text(message.chat.id, "Задача не найдена.")
            return
        await self.orchestrator.pause_task(task.id, message.from_user.id, message.chat.id)

    async def cmd_retry(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        await self.orchestrator.resume_by_id(
            message.chat.id, int(arg) if arg.isdigit() else None, message.from_user.id
        )

    async def cmd_resume(self, message: Message) -> None:
        arg = _command_tail(message.text or "")
        await self.orchestrator.resume_by_id(
            message.chat.id, int(arg) if arg.isdigit() else None, message.from_user.id
        )

    async def cmd_rollback(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.request_rollback(task.id, message.from_user.id, message.chat.id)

    async def cmd_push(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.request_push(task.id, message.from_user.id, message.chat.id)

    async def cmd_rework(self, message: Message) -> None:
        task = await self._task_from_message(message)
        if task:
            await self.orchestrator.rework_task(task.id, message.from_user.id, message.chat.id)

    async def cmd_usage(self, message: Message) -> None:
        arg = _command_tail(message.text or "").strip().lower()
        task_id = None
        period = "today"
        if arg.startswith("task "):
            value = arg.split(maxsplit=1)[1]
            if not value.isdigit():
                await self.notifier.send_text(message.chat.id, "Использование: /usage task <id>")
                return
            task_id = int(value)
        elif arg in {"week", "month", "today"}:
            period = arg
        elif arg:
            await self.notifier.send_text(
                message.chat.id, "Использование: /usage [today|week|month|task <id>]"
            )
            return
        await self.notifier.send_text(
            message.chat.id,
            await self.orchestrator.usage_text(message.chat.id, period, task_id),
            parse_mode="HTML",
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
        if arg.startswith("edit "):
            parts = arg.split(maxsplit=2)
            if len(parts) < 3 or not parts[1].isdigit():
                await self.notifier.send_text(
                    message.chat.id, "Использование: /memory edit <id> <текст>"
                )
                return
            updated = await self.orchestrator.conversations.update_memory(
                int(parts[1]), content=parts[2]
            )
            await self.notifier.send_text(
                message.chat.id, "Обновлено." if updated else "Запись не найдена."
            )
            return
        if arg.startswith("stale "):
            parts = arg.split()
            if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"on", "off"}:
                await self.notifier.send_text(
                    message.chat.id, "Использование: /memory stale <id> on|off"
                )
                return
            updated = await self.orchestrator.conversations.update_memory(
                int(parts[1]), stale=parts[2] == "on"
            )
            await self.notifier.send_text(
                message.chat.id, "Обновлено." if updated else "Запись не найдена."
            )
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
            message.chat.id,
            await self.orchestrator.memory_text(message.chat.id, arg),
            parse_mode="HTML",
        )

    async def cmd_doctor(self, message: Message) -> None:
        await self.notifier.send_text(
            message.chat.id, await self.orchestrator.doctor_text(message.chat.id)
        )

    async def cmd_github(self, message: Message) -> None:
        arg = _command_tail(message.text or "").strip()
        parts = arg.split()
        if len(parts) >= 3 and parts[0].lower() == "info":
            await self.orchestrator.github_info(message.chat.id, parts[1], parts[2])
            return
        if len(parts) >= 2 and parts[0].lower() == "create":
            visibility = parts[2].lower() if len(parts) > 2 else "private"
            if visibility not in {"private", "public"}:
                await self.notifier.send_text(
                    message.chat.id, "Видимость должна быть private или public."
                )
                return
            await self.orchestrator.request_github_create(
                parts[1], visibility == "private", message.from_user.id, message.chat.id
            )
            return
        await self.notifier.send_text(
            message.chat.id,
            "Использование: /github info <owner/repo> | /github create <name> [private|public]",
        )

    async def cmd_models(self, message: Message) -> None:
        if self.orchestrator.providers is None:
            await self.notifier.send_text(message.chat.id, "Провайдеры не инициализированы.")
            return
        snapshot = self.orchestrator.providers.snapshot()
        lines = ["Управляющие модели", ""]
        for role, data in snapshot["roles"].items():
            model = data.get("model") or "недоступна"
            available = "✓" if data.get("available") else "✕"
            lines.append(f"{available} {role}: {data['provider']} / {model}")
        lines += [
            "",
            "Смена: /provider select <role> <provider>; активные задачи должны быть завершены.",
        ]
        await self.notifier.send_text(message.chat.id, "\n".join(lines))

    async def cmd_provider(self, message: Message) -> None:
        if self.orchestrator.providers is None:
            await self.notifier.send_text(message.chat.id, "Провайдеры не инициализированы.")
            return
        parts = (message.text or "").split()
        arg = parts[1] if len(parts) > 1 else "info"
        registry = self.orchestrator.providers
        if arg == "info":
            snapshot = registry.snapshot()
            lines = ["Провайдеры", ""]
            for name, data in snapshot["providers"].items():
                cap = data["capabilities"]
                lines.append(
                    f"{name}: {data['model']}; tools={cap['tools']}, structured={cap['structured_output']}, "
                    f"streaming={cap['streaming']}, reasoning={cap['reasoning']}, cache={cap['prompt_cache']}"
                )
            states = await registry.state_snapshot()
            if states:
                lines += ["", "Сохранённое состояние:"]
                for name, state in states.items():
                    lines.append(
                        f"{name}: {state.get('state')} "
                        f"(category={state.get('failure_category') or '—'}, "
                        f"checked={state.get('last_checked_at') or '—'})"
                    )
                    if state.get("state") == "QUOTA_EXHAUSTED":
                        lines.append(f"  восстановление: /provider recover {name}")
            lines += ["", "Ключи не отображаются."]
            await self.notifier.send_text(message.chat.id, "\n".join(lines))
            return
        if arg == "recover":
            if len(parts) != 3:
                await self.notifier.send_text(
                    message.chat.id, "Использование: /provider recover <provider-or-role>"
                )
                return
            await self.orchestrator.request_provider_recovery(
                parts[2],
                message.from_user.id,
                message.chat.id,  # type: ignore[union-attr]
            )
            return
        if arg == "test":
            role = parts[2] if len(parts) > 2 else "chat"
            results = await registry.health(role)
            lines = ["Проверка провайдера", ""]
            for name, data in results.items():
                lines.append(f"{name}: {'API доступен' if data['available'] else 'недоступен'}")
                checks = data.get("checks") or {}
                if checks:
                    lines.append(
                        "  " + ", ".join(f"{key}={value}" for key, value in checks.items())
                    )
                if data.get("generation_ready") is not True:
                    lines.append(
                        "  generation readiness: не подтверждена (платный probe не выполнялся)"
                    )
                if data.get("detail"):
                    lines.append(str(data["detail"])[:1000])
            await self.notifier.send_text(message.chat.id, "\n".join(lines))
            return
        if arg == "select":
            if len(parts) != 4:
                await self.notifier.send_text(
                    message.chat.id, "Использование: /provider select <role> <provider>"
                )
                return
            role, provider = parts[2], parts[3]
            active = await self.orchestrator.tasks.active(None)
            if active:
                await self.notifier.send_text(
                    message.chat.id,
                    "Есть активные задачи. Дождитесь их завершения или отмените их; "
                    "смена провайдера не применяется к уже запущенным задачам.",
                )
                return
            try:
                registry.select(role, provider)
            except Exception as exc:
                await self.notifier.send_text(message.chat.id, f"Не удалось: {exc}")
                return
            await self.notifier.send_text(
                message.chat.id,
                f"Роль {role} теперь использует {provider}. Смена действует на новые запросы "
                "до перезапуска; для постоянной настройки измените [llm.roles].",
            )
            return
        await self.notifier.send_text(
            message.chat.id,
            "Использование: /provider info | /provider test [role] | "
            "/provider select <role> <provider>",
        )

    async def cmd_settings(self, message: Message) -> None:
        mode = await self.orchestrator.conversations.get_ui_mode(message.chat.id)
        await self.notifier.send_text(
            message.chat.id,
            settings_card(
                mode=mode,
                lines=(await self.orchestrator.settings_text(message.chat.id)).splitlines()[:30],
            ),
            [
                [Button("compact", "ui:mode:compact"), Button("detailed", "ui:mode:detailed")],
                [Button("debug", "ui:mode:debug")],
            ],
            parse_mode="HTML",
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
            if action == "ui" and data.startswith("ui:mode:"):
                mode = data.split(":", 2)[2]
                try:
                    await self.orchestrator.conversations.set_ui_mode(chat_id, mode)
                except ValueError:
                    await callback.answer("Неизвестный режим", show_alert=True)
                    return
                await self.notifier.send_text(chat_id, f"Режим интерфейса: {mode}.")
                await self.orchestrator.progress.update(
                    task_id or 0, force=True, chat_id=chat_id
                ) if task_id else None
                return
            if action == "menu":
                if arg == "projects":
                    await self.cmd_projects(await self._synthetic_message(callback))
                elif arg == "tasks":
                    await self.notifier.send_text(
                        chat_id, await self.orchestrator.tasks_text(chat_id)
                    )
                elif arg == "history":
                    await self.notifier.send_text(
                        chat_id, await self.orchestrator.tasks_text(chat_id)
                    )
                elif arg == "usage":
                    await self.notifier.send_text(
                        chat_id,
                        await self.orchestrator.usage_text(chat_id),
                        parse_mode="HTML",
                    )
                elif arg == "status":
                    await self.notifier.send_text(
                        chat_id, await self.orchestrator.status_text(chat_id)
                    )
                elif arg == "settings":
                    await self.notifier.send_text(
                        chat_id, "Откройте /settings для режима интерфейса."
                    )
                return
            if action == "det" and task_id is not None:
                await self.orchestrator.send_details(task_id, chat_id)
            elif action == "compact" and task_id is not None:
                await self.orchestrator.send_compact(task_id, chat_id)
            elif action == "use" and task_id is not None:
                await self.notifier.send_text(
                    chat_id,
                    await self.orchestrator.usage_text(chat_id, task_id=task_id),
                    parse_mode="HTML",
                )
            elif action == "rep" and task_id is not None:
                await self.orchestrator.send_report(task_id, chat_id)
            elif action == "acc" and task_id is not None:
                await self.orchestrator.accept_task(task_id, user_id, chat_id)
            elif action == "rew" and task_id is not None:
                await self.orchestrator.rework_task(task_id, user_id, chat_id)
            elif action == "dif" and task_id is not None:
                await self.orchestrator.send_diff(task_id, chat_id)
            elif action == "log" and task_id is not None:
                await self.orchestrator.send_logs(task_id, chat_id)
            elif action == "rb" and task_id is not None:
                await self.orchestrator.request_rollback(task_id, user_id, chat_id)
            elif action == "push" and task_id is not None:
                await self.orchestrator.request_push(task_id, user_id, chat_id)
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

    async def _synthetic_message(self, callback: CallbackQuery) -> Message:
        # Only used for menu callbacks that share command handlers.
        return callback.message  # type: ignore[return-value]


def _command_tail(text: str) -> str:
    return text.split(maxsplit=1)[1].strip() if " " in text else ""
