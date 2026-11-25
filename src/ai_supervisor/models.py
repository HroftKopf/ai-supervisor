from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    QUEUED = "queued"
    RUNNING = "running"
    REVIEWING = "reviewing"
    REWORK = "rework"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
ACTIVE_STATUSES = {
    TaskStatus.CREATED,
    TaskStatus.PLANNING,
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.REVIEWING,
    TaskStatus.REWORK,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.PAUSED,
    TaskStatus.INTERRUPTED,
}

# Explicit state machine. The orchestrator, not a model, performs transitions.
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.REVIEWING,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.REVIEWING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.REWORK,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.REWORK: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.INTERRUPTED}
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.REWORK, TaskStatus.CANCELLED}
    ),
    TaskStatus.INTERRUPTED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class TransitionError(RuntimeError):
    pass


def ensure_transition(current: TaskStatus, new: TaskStatus) -> None:
    if new not in TRANSITIONS[current]:
        raise TransitionError(f"invalid task transition: {current.value} -> {new.value}")


class IntentKind(StrEnum):
    CHAT = "chat"
    TASK = "task"


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntentKind
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=1000)
    clarification: str | None = Field(default=None, max_length=1000)
    task_title_hint: str | None = Field(default=None, max_length=160)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=120)
    goal: str = Field(min_length=10, max_length=8000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[str] = Field(default_factory=list, max_length=30)
    files_expected: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("acceptance_criteria", "steps", "constraints", "risks")
    @classmethod
    def _no_blank_items(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) != len(value):
            raise ValueError("list items must be non-blank")
        return cleaned


class ReviewVerdict(StrEnum):
    ACCEPT = "accept"
    REWORK = "rework"
    FAIL = "fail"


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    summary: str = Field(min_length=1, max_length=4000)
    issues: list[str] = Field(default_factory=list, max_length=30)
    required_changes: list[str] = Field(default_factory=list, max_length=30)


class TokenUsage(BaseModel):
    """Provider-reported usage. Never synthesise values the provider omitted."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)

    def merge(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            requests=self.requests + other.requests,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    final_response: str = ""
    finish_reason: str | None = None
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    events_count: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    timed_out: bool = False
    return_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def completed(self) -> bool:
        return (
            self.error is None
            and not self.timed_out
            and self.finish_reason == "completed"
            and self.return_code == 0
        )


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    exit_code: int | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int
    base_commit: str = ""
    head_commit: str = ""
    status_porcelain: str = ""
    diff_stat: str = ""
    diff: str = ""
    diff_truncated: bool = False
    changed_files: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    worker: WorkerResult
    verification_note: str = ""
    captured_at: datetime = Field(default_factory=utcnow)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_files) or bool(self.diff.strip())

    @property
    def checks_passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def has_unrun_checks(self) -> bool:
        return not self.checks


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    project_id: int
    conversation_id: int
    title: str
    status: TaskStatus
    spec_json: str = "{}"
    worker_name: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    rework_count: int = 0
    max_rework: int = 3
    review_json: str | None = None
    evidence_json: str | None = None
    report: str | None = None
    error: str | None = None
    session_id: str | None = None
    dsh_home: str | None = None
    diff_path: str | None = None
    logs_path: str | None = None
    cancel_requested: bool = False
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def spec(self) -> TaskSpec | None:
        import json

        try:
            return TaskSpec.model_validate(json.loads(self.spec_json))
        except Exception:
            return None

    @property
    def review(self) -> ReviewResult | None:
        import json

        if not self.review_json:
            return None
        try:
            return ReviewResult.model_validate(json.loads(self.review_json))
        except Exception:
            return None


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    path: str
    description: str = ""
    stack: str = ""
    instructions: str = ""
    base_branch: str = "main"
    test_command: str | None = None
    build_command: str | None = None
    created_at: datetime
    updated_at: datetime


class ConversationRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    chat_id: int
    project_id: int | None = None
    title: str | None = None
    summary: str | None = None
    summary_upto_id: int | None = None
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    scope: Literal["user", "project", "conversation"]
    project_id: int | None = None
    conversation_id: int | None = None
    key: str = ""
    content: str
    source: str = "user"
    user_confirmed: bool = False
    created_at: datetime
    updated_at: datetime


class MessageRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    conversation_id: int
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    telegram_message_id: int | None = None
    reply_to_message_id: int | None = None
    meta_json: str = "{}"
    created_at: datetime


class JsonDict(dict[str, Any]):
    pass
