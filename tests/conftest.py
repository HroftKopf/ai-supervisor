from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from ai_supervisor.config import Settings, load_settings
from ai_supervisor.core.context import ProjectContextBuilder
from ai_supervisor.core.interfaces import Button
from ai_supervisor.core.orchestrator import Orchestrator
from ai_supervisor.core.permissions import ApprovalService
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.sandbox import BwrapSandbox
from ai_supervisor.core.usage import UsageService
from ai_supervisor.core.worker_manager import WorkerScheduler
from ai_supervisor.core.workspace import WorkspaceManager
from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.database import Database
from ai_supervisor.memory.projects import ProjectService
from ai_supervisor.memory.tasks import TaskService
from ai_supervisor.models import (
    IntentKind,
    IntentResult,
    ProjectRecord,
    ReviewResult,
    ReviewVerdict,
    RootCause,
    TaskRecord,
    TaskSpec,
    TokenUsage,
    WorkerResult,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = 424242


class FakeSupervisor:
    def prompt_metadata(self) -> dict[str, str]:
        return {"version": "test", "supervisor": "test", "planner": "test"}

    def __init__(self) -> None:
        self.intent = IntentKind.TASK
        self.review_verdict = ReviewVerdict.ACCEPT
        self.review_root_cause = RootCause.UNKNOWN
        self.review_sequence: list[ReviewVerdict] = []
        self.chat_calls = 0
        self.review_calls = 0
        self.chat_response = "Привет! Это ответ Kimi."
        self.plan_spec = TaskSpec(
            title="Append line",
            goal="Append a verified line to README.md",
            acceptance_criteria=["README.md contains the added line"],
            steps=["Edit README.md", "Run configured checks"],
            files_expected=["README.md"],
        )

    async def classify(self, **_: object) -> tuple[IntentResult, TokenUsage]:
        return (
            IntentResult(
                kind=self.intent,
                confidence=0.95,
                reason="test fixture",
                task_title_hint="Append line" if self.intent is IntentKind.TASK else None,
            ),
            TokenUsage(input_tokens=5, output_tokens=2, requests=1),
        )

    async def plan(self, **_: object) -> tuple[TaskSpec, TokenUsage]:
        return self.plan_spec, TokenUsage(input_tokens=10, output_tokens=5, requests=1)

    async def review(self, **_: object) -> tuple[ReviewResult, TokenUsage]:
        self.review_calls += 1
        verdict = self.review_sequence.pop(0) if self.review_sequence else self.review_verdict
        if verdict is ReviewVerdict.ACCEPT:
            result = ReviewResult(
                verdict=ReviewVerdict.ACCEPT,
                summary="diff соответствует ТЗ",
                issues=[],
                required_changes=[],
            )
        elif verdict is ReviewVerdict.REWORK:
            result = ReviewResult(
                verdict=ReviewVerdict.REWORK,
                summary="нужна доработка",
                root_cause=self.review_root_cause,
                issues=["неполное изменение"],
                required_changes=["дополнить README"],
            )
        else:
            result = ReviewResult(
                verdict=ReviewVerdict.FAIL,
                summary="работа отклонена",
                issues=["принципиальная ошибка"],
                required_changes=[],
            )
        return result, TokenUsage(input_tokens=7, output_tokens=3, requests=1)

    async def chat(self, **_: object):
        self.chat_calls += 1
        from ai_supervisor.providers.kimi import KimiResponse

        return KimiResponse(
            content=self.chat_response,
            reasoning_content="",
            usage=TokenUsage(input_tokens=3, output_tokens=2, requests=1),
            model="kimi-test",
        )

    async def summarize(self, prompt: str) -> tuple[str, TokenUsage]:
        return "Краткое резюме", TokenUsage(input_tokens=2, output_tokens=1, requests=1)


class FakeWorkers:
    def __init__(self) -> None:
        self.write_content: str | None = "verified line\n"
        self.write_sequence: list[str | None] | None = None
        self.calls = 0
        self.started = asyncio.Event()
        self.block = False
        self.cancelled = False
        self.usage = TokenUsage(input_tokens=11, output_tokens=4, requests=1)

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
        self.calls += 1
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        content = self.write_sequence.pop(0) if self.write_sequence else self.write_content
        if content is not None:
            worktree = Path(task.worktree_path or "")
            readme = worktree / "README.md"
            with readme.open("a", encoding="utf-8") as handle:
                handle.write(content)
        return WorkerResult(
            session_id=task.session_id or "session-fake",
            final_response="готово",
            finish_reason="completed",
            error=None,
            usage=self.usage,
            events_count=1,
            duration_seconds=0.01,
            return_code=0,
        )

    async def cancel(self, task_id: int) -> bool:
        self.cancelled = True
        return True

    def clear_cancel(self, task_id: int) -> None:
        self.cancelled = False


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, list[list[Button]] | None]] = []
        self.edits: list[tuple[int, int, str, list[list[Button]] | None]] = []
        self.fail_edits = False
        self.documents: list[tuple[int, Path]] = []

    async def send_text(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[Button]] | None = None,
        *,
        parse_mode: str | None = None,
    ) -> int:
        self.messages.append((chat_id, text, buttons))
        return len(self.messages)

    async def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: list[list[Button]] | None = None,
        *,
        parse_mode: str | None = None,
    ) -> bool:
        if self.fail_edits:
            return False
        self.edits.append((chat_id, message_id, text, buttons))
        return True

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        self.documents.append((chat_id, path))

    async def edit_buttons(
        self, chat_id: int, message_id: int, buttons: list[list[Button]] | None
    ) -> None:
        return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = load_settings(check_env_permissions=False)
    projects = tmp_path / "projects"
    projects.mkdir()
    app = base.app.model_copy(update={"data_dir": str(tmp_path / "var")})
    security = base.security.model_copy(update={"allowed_project_roots": [str(projects)]})
    secrets = base.secrets.model_copy(update={"telegram_owner_id": OWNER_ID})
    settings = base.model_copy(update={"app": app, "security": security, "secrets": secrets})
    settings.ensure_runtime_dirs()
    return settings


@pytest.fixture
def repo(settings: Settings) -> Path:
    root = Path(settings.security.allowed_project_roots[0]) / "demo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
    return root


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings.db_path)
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def project(database: Database, settings: Settings, repo: Path) -> ProjectRecord:
    service = ProjectService(database, settings)
    return await service.add("demo", str(repo))


@pytest_asyncio.fixture
async def orchestrator(
    database: Database,
    settings: Settings,
    project: ProjectRecord,
) -> AsyncIterator[
    tuple[Orchestrator, FakeSupervisor, FakeWorkers, FakeNotifier, ProjectService, TaskService]
]:
    projects = ProjectService(database, settings)
    conversations = ConversationService(database, settings)
    tasks = TaskService(database)
    usage = UsageService(database, settings)
    approvals = ApprovalService(database)
    redactor = SecretRedactor(["topsecret"])
    sandbox = BwrapSandbox(settings)
    workspace = WorkspaceManager(settings, sandbox, redactor)
    supervisor = FakeSupervisor()
    workers = FakeWorkers()
    notifier = FakeNotifier()
    orch = Orchestrator(
        settings=settings,
        projects=projects,
        conversations=conversations,
        tasks=tasks,
        usage=usage,
        approvals=approvals,
        supervisor=supervisor,  # type: ignore[arg-type]
        workspace=workspace,
        workers=workers,  # type: ignore[arg-type]
        scheduler=WorkerScheduler(2),
        context_builder=ProjectContextBuilder(settings, redactor),
        redactor=redactor,
        notifier=notifier,
    )
    await orch.startup()
    yield orch, supervisor, workers, notifier, projects, tasks
    await orch.shutdown()
