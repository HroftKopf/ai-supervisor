from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from ai_supervisor.config import ConfigError, Settings, load_settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.logging_setup import configure_logging

logger = logging.getLogger(__name__)

SUPPORTED_DSH_SDK = "0.2.2rc2"


async def run_bot(settings: Settings, *, skip_checks: bool = False) -> None:
    from aiogram import Bot

    from ai_supervisor.agents.supervisor import SupervisorAgent
    from ai_supervisor.core.context import ProjectContextBuilder
    from ai_supervisor.core.orchestrator import Orchestrator
    from ai_supervisor.core.permissions import ApprovalService
    from ai_supervisor.core.sandbox import create_sandbox
    from ai_supervisor.core.usage import UsageService
    from ai_supervisor.core.worker_manager import WorkerManager, WorkerScheduler
    from ai_supervisor.core.workspace import WorkspaceManager
    from ai_supervisor.memory.conversations import ConversationService
    from ai_supervisor.memory.database import Database
    from ai_supervisor.memory.projects import ProjectService
    from ai_supervisor.memory.tasks import TaskService
    from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig
    from ai_supervisor.telegram.bot import TelegramBot, build_bot
    from ai_supervisor.telegram.notifications import BotNotifier

    settings.require_credentials()
    settings.ensure_runtime_dirs()
    redactor = SecretRedactor.from_settings(settings)
    configure_logging(settings, redactor)

    sandbox = create_sandbox(settings)
    await sandbox.check_available()
    logger.info("sandbox check passed: %s", settings.sandbox.backend)

    db = Database(settings.db_path)
    await db.connect()
    kimi = KimiClient(
        KimiClientConfig(
            api_key=settings.kimi_key,
            base_url=settings.kimi.base_url,
            model=settings.kimi.model,
            reasoning_effort=settings.kimi.reasoning_effort,
            max_completion_tokens=settings.kimi.max_completion_tokens,
            request_timeout_seconds=settings.kimi.request_timeout_seconds,
            max_retries=settings.kimi.max_retries,
            max_concurrent_requests=settings.kimi.max_concurrent_requests,
        ),
        redactor,
    )
    bot: Bot | None = None
    orchestrator: Orchestrator | None = None
    telegram: TelegramBot | None = None
    try:
        if not skip_checks and settings.kimi.verify_model_on_startup:
            models = await kimi.validate_model()
            logger.info("Kimi model verified (%s models available)", len(models))
        projects = ProjectService(db, settings)
        conversations = ConversationService(db, settings)
        tasks = TaskService(db)
        usage = UsageService(db, settings)
        approvals = ApprovalService(db)
        workspace = WorkspaceManager(settings, sandbox, redactor)
        supervisor = SupervisorAgent(kimi, settings, redactor)
        context_builder = ProjectContextBuilder(settings, redactor)
        worker_manager = WorkerManager(
            settings=settings,
            sandbox=sandbox,
            workspace=workspace,
            tasks=tasks,
            redactor=redactor,
        )
        scheduler = WorkerScheduler(settings.limits.max_workers)
        bot = build_bot(settings)
        notifier = BotNotifier(bot, settings)
        orchestrator = Orchestrator(
            settings=settings,
            projects=projects,
            conversations=conversations,
            tasks=tasks,
            usage=usage,
            approvals=approvals,
            supervisor=supervisor,
            workspace=workspace,
            workers=worker_manager,
            scheduler=scheduler,
            context_builder=context_builder,
            redactor=redactor,
            notifier=notifier,
        )
        telegram = TelegramBot(
            settings=settings,
            db=db,
            orchestrator=orchestrator,
            projects=projects,
            notifier=notifier,
        )
        logger.info("AI Supervisor starting (root=%s)", settings.root)
        await telegram.start()
    finally:
        if orchestrator is not None:
            await orchestrator.shutdown()
        await kimi.close()
        if telegram is not None:
            await telegram.close()
        elif bot is not None:
            await bot.session.close()
        await db.close()


class Doctor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("OK", name, detail))

    def warn(self, name: str, detail: str) -> None:
        self.warnings.append(f"{name}: {detail}")
        self.rows.append(("WARN", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.errors.append(f"{name}: {detail}")
        self.rows.append(("FAIL", name, detail))

    async def run(self) -> int:
        s = self.settings
        version = sys.version_info
        if version >= (3, 12):
            self.ok("Python", platform.python_version())
        else:
            self.fail("Python", f"{platform.python_version()} < 3.12")

        try:
            sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            self.ok("SQLite FTS5", sqlite3.sqlite_version)
        except Exception as exc:
            self.warn("SQLite FTS5", f"unavailable: {exc}")

        if shutil.which("git"):
            result = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=10
            )
            self.ok("Git", result.stdout.strip())
        else:
            self.fail("Git", "not found")

        if shutil.which("bwrap"):
            self.ok("bubblewrap", shutil.which("bwrap") or "")
            from ai_supervisor.core.sandbox import BwrapSandbox

            try:
                await BwrapSandbox(s).check_available()
                self.ok("bwrap probe", "user namespaces/filesystem isolation works")
            except Exception as exc:
                self.fail("bwrap probe", str(exc))
        else:
            self.fail("bubblewrap", "install the bubblewrap package")

        for root in s.allowed_roots():
            if root.is_dir():
                self.ok("allowed root", str(root))
            else:
                self.fail("allowed root", f"missing: {root}")

        try:
            sdk_version = importlib.metadata.version("deepseek-harness-sdk")
            self.ok("deepseek-harness-sdk", sdk_version)
            if sdk_version != SUPPORTED_DSH_SDK:
                self.warn(
                    "deepseek-harness-sdk version",
                    f"tested with {SUPPORTED_DSH_SDK}, found {sdk_version}",
                )
            from deepseek_harness import DeepSeekHarness

            with tempfile.TemporaryDirectory(prefix="ai-supervisor-doctor-") as temp:
                workspace = Path(temp) / "workspace"
                home = Path(temp) / "home"
                workspace.mkdir()
                home.mkdir()
                harness = DeepSeekHarness(
                    dsh_home=str(home),
                    cwd=str(workspace),
                    profile=s.deepseek.profile,
                    provider=s.deepseek.provider,
                    model=s.deepseek_model,
                    api_key="doctor-placeholder",
                    initialize_timeout_seconds=60,
                )
                try:
                    harness.start()
                    self.ok("DSH runtime", "sdk-minimal initialized")
                finally:
                    harness.close()
        except Exception as exc:
            self.fail("DeepSeek Harness SDK", f"{type(exc).__name__}: {exc}")

        if s.secrets.telegram_owner_id is not None:
            self.ok("Telegram owner ID", str(s.secrets.telegram_owner_id))
        else:
            self.fail("Telegram owner ID", "TELEGRAM_OWNER_ID is missing")
        if s.telegram_token:
            try:
                from aiogram import Bot

                bot = Bot(token=s.telegram_token)
                try:
                    me = await bot.get_me()
                    self.ok("Telegram bot", f"@{me.username} (id={me.id})")
                finally:
                    await bot.session.close()
            except Exception as exc:
                self.fail("Telegram bot", f"{type(exc).__name__}: {exc}")
        else:
            self.fail("Telegram bot", "TELEGRAM_BOT_TOKEN is missing")

        missing = s.secrets.missing()
        if missing:
            self.fail("credentials", "missing: " + ", ".join(missing))
        else:
            self.ok("credentials", "all required variables are present")
        try:
            s.assert_secret_file_permissions()
            self.ok(
                ".env permissions",
                oct(s.env_path.stat().st_mode & 0o777) if s.env_path.exists() else "no .env",
            )
        except ConfigError as exc:
            self.fail(".env permissions", str(exc))

        if s.github_token:
            self.ok("GitHub API", "GITHUB_TOKEN configured")
        else:
            self.warn("GitHub API", "GITHUB_TOKEN optional and not configured; /github is disabled")

        if s.kimi_key:
            from ai_supervisor.core.redaction import SecretRedactor
            from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig

            kimi = KimiClient(
                KimiClientConfig(
                    api_key=s.kimi_key,
                    base_url=s.kimi.base_url,
                    model=s.kimi.model,
                    reasoning_effort=s.kimi.reasoning_effort,
                    max_completion_tokens=s.kimi.max_completion_tokens,
                    request_timeout_seconds=s.kimi.request_timeout_seconds,
                    max_retries=0,
                    max_concurrent_requests=1,
                ),
                SecretRedactor.from_settings(s),
            )
            try:
                models = await kimi.validate_model()
                self.ok("Kimi model", f"{s.kimi.model} (available; {len(models)} models)")
            except Exception as exc:
                self.fail("Kimi model", f"{type(exc).__name__}: {exc}")
            finally:
                await kimi.close()
        else:
            self.fail("Kimi", "KIMI_API_KEY is missing")

        self._print()
        return 1 if self.errors else 0

    def _print(self) -> None:
        for status, name, detail in self.rows:
            marker = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}[status]
            print(f"{marker} {name}: {detail}")
        if self.errors:
            print(f"\n{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        elif self.warnings:
            print(f"\nOK with {len(self.warnings)} warning(s)")
        else:
            print("\nAll checks passed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-supervisor", description="AI Supervisor")
    parser.add_argument("--version", action="version", version="ai-supervisor 0.1.0")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run the Telegram bot")
    run.add_argument("--skip-checks", action="store_true", help="skip startup network/model checks")
    sub.add_parser("doctor", help="check the local installation and credentials")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(check_env_permissions=args.command != "doctor")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        return asyncio.run(Doctor(settings).run())
    try:
        asyncio.run(run_bot(settings, skip_checks=getattr(args, "skip_checks", False)))
        return 0
    except KeyboardInterrupt:
        return 130
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger(__name__).exception("fatal startup error")
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
