from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from ai_supervisor.config import Settings
from ai_supervisor.core.orchestrator import Orchestrator
from ai_supervisor.memory.database import Database
from ai_supervisor.memory.projects import ProjectService
from ai_supervisor.telegram.auth import DedupeMiddleware
from ai_supervisor.telegram.handlers import Handlers
from ai_supervisor.telegram.notifications import BotNotifier

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        orchestrator: Orchestrator,
        projects: ProjectService,
        notifier: BotNotifier,
    ) -> None:
        self.settings = settings
        self.db = db
        self.orchestrator = orchestrator
        self.projects = projects
        self.notifier = notifier
        self.bot = notifier.bot
        self.dispatcher = Dispatcher()
        self.dispatcher.update.outer_middleware(DedupeMiddleware(db))
        self.handlers = Handlers(
            settings=settings,
            orchestrator=orchestrator,
            projects=projects,
            notifier=notifier,
        )
        self.dispatcher.include_router(self.handlers.router)

    async def start(self) -> None:
        me = await self.bot.get_me()
        logger.info("Telegram bot authorized as @%s (id=%s)", me.username, me.id)
        await self.orchestrator.startup()
        await self.dispatcher.start_polling(
            self.bot,
            polling_timeout=self.settings.telegram.polling_timeout_seconds,
            handle_as_tasks=True,
            handle_signals=True,
        )

    async def close(self) -> None:
        await self.bot.session.close()


def build_bot(settings: Settings) -> Bot:
    session = AiohttpSession(timeout=90)
    return Bot(
        token=settings.telegram_token,
        session=session,
        default=DefaultBotProperties(parse_mode=None, link_preview_is_disabled=True),
    )
