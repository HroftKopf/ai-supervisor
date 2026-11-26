from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath as PathLike
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskType(StrEnum):
    READ_ONLY = "read_only"
    MODIFY = "modify"
    GIT_OPERATION = "git_operation"
    EXECUTE = "execute"
    SYSTEM_OPERATION = "system_operation"
    EXTERNAL_OPERATION = "external_operation"
    COMPOSITE = "composite"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class StageOperation(StrEnum):
    IMPORT_CONFIG = "import_config"
    GIT_COMMIT = "git_commit"
    VALIDATE = "validate"
    GITHUB_ENSURE_REPO = "github_ensure_repo"
    PUBLISH = "publish"
    KIMI_ANALYZE = "kimi_analyze"
    DEEPSEEK_CHANGE = "deepseek_change"


class StageExecutor(StrEnum):
    PYTHON = "python"
    GIT = "git"
    GITHUB = "github"
    VALIDATOR = "validator"
    KIMI = "kimi"
    DEEPSEEK = "deepseek"


class StageStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REWORK = "rework"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    RETRY_WAIT = "retry_wait"
    PARTIALLY_COMPLETED = "partially_completed"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.PARTIALLY_COMPLETED,
    TaskStatus.CANCELLED,
    TaskStatus.FAILED,
}
ACTIVE_STATUSES = {
    TaskStatus.CREATED,
    TaskStatus.PLANNING,
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.VALIDATING,
    TaskStatus.REVIEWING,
    TaskStatus.REWORK,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.WAITING_INPUT,
    TaskStatus.RETRY_WAIT,
    TaskStatus.BLOCKED,
    TaskStatus.PAUSED,
    TaskStatus.INTERRUPTED,
}

# Explicit state machine. The orchestrator, not a model, performs transitions.
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.BLOCKED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.BLOCKED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VALIDATING,
            TaskStatus.REVIEWING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.COMPLETED,
        }
    ),
    TaskStatus.VALIDATING: frozenset(
        {
            TaskStatus.REVIEWING,
            TaskStatus.REWORK,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.RETRY_WAIT,
            TaskStatus.BLOCKED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.REVIEWING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.REWORK,
            TaskStatus.WAITING_INPUT,
            TaskStatus.RETRY_WAIT,
            TaskStatus.BLOCKED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.REWORK: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INPUT,
            TaskStatus.RETRY_WAIT,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.REWORK,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_INPUT,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.WAITING_INPUT: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.QUEUED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.RETRY_WAIT: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.INTERRUPTED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.BLOCKED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.PARTIALLY_COMPLETED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
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


STAGE_ID_RE = r"^[a-z][a-z0-9_-]{0,63}$"
STAGE_EXECUTORS: dict[StageOperation, StageExecutor] = {
    StageOperation.IMPORT_CONFIG: StageExecutor.PYTHON,
    StageOperation.GIT_COMMIT: StageExecutor.GIT,
    StageOperation.VALIDATE: StageExecutor.VALIDATOR,
    StageOperation.GITHUB_ENSURE_REPO: StageExecutor.GITHUB,
    StageOperation.PUBLISH: StageExecutor.GIT,
    StageOperation.KIMI_ANALYZE: StageExecutor.KIMI,
    StageOperation.DEEPSEEK_CHANGE: StageExecutor.DEEPSEEK,
}


class StageSpec(BaseModel):
    """A single executable node in a COMPOSITE task graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=STAGE_ID_RE)
    operation: StageOperation
    goal: str = Field(min_length=3, max_length=2000)
    depends_on: list[str] = Field(default_factory=list, max_length=30)
    executor: StageExecutor
    required_permissions: list[RiskLevel] = Field(default_factory=list, max_length=10)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=15)
    inputs: list[str] = Field(default_factory=list, max_length=30)
    outputs: list[str] = Field(default_factory=list, max_length=30)
    source: str | None = Field(default=None, max_length=2000)
    destination: str | None = Field(default=None, max_length=2000)
    overwrite: bool = False
    commit_message: str | None = Field(default=None, max_length=300)
    repo_name: str | None = Field(default=None, max_length=100)
    repo_owner: str | None = Field(default=None, max_length=39)
    private: bool = True
    remote: str = Field(default="origin", max_length=64)
    branch: str | None = Field(default=None, max_length=200)
    merge: bool = True
    instruction: str | None = Field(default=None, max_length=8000)
    allowed_paths: list[str] = Field(default_factory=lambda: ["."], max_length=50)
    require_change: bool = False

    @field_validator("depends_on")
    @classmethod
    def _known_dependencies(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("stage dependencies must be unique")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def _stage_paths(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            item = item.strip()
            if not item:
                continue
            path = PathLike(item)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"stage paths must stay inside the project: {item}")
            cleaned.append(item)
        return cleaned or ["."]

    @model_validator(mode="after")
    def _operation_consistency(self) -> StageSpec:
        expected = STAGE_EXECUTORS[self.operation]
        if self.executor is not expected:
            raise ValueError(
                f"stage {self.id}: operation {self.operation.value} must use executor "
                f"{expected.value}, not {self.executor.value}"
            )
        if RiskLevel.CRITICAL in self.required_permissions:
            raise ValueError(f"stage {self.id}: CRITICAL permissions are forbidden")
        if self.operation is StageOperation.IMPORT_CONFIG and not self.destination:
            raise ValueError(f"stage {self.id}: import_config requires destination")
        if self.operation is StageOperation.GITHUB_ENSURE_REPO and not self.repo_name:
            raise ValueError(f"stage {self.id}: github_ensure_repo requires repo_name")
        if self.operation in {StageOperation.KIMI_ANALYZE, StageOperation.DEEPSEEK_CHANGE}:
            if not (self.instruction or self.goal):
                raise ValueError(f"stage {self.id}: model operation requires an instruction")
        if self.operation is StageOperation.PUBLISH and self.merge and self.branch is not None:
            raise ValueError(
                f"stage {self.id}: branch is derived from the project base branch when merge=true"
            )
        return self


class TaskSpec(BaseModel):
    """Validated contract passed from Kimi to the executor.

    The optional v2 fields keep existing stored specs readable; missing values are
    derived from the task type and project policy by the orchestrator.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=1, le=2)
    title: str = Field(min_length=3, max_length=120)
    goal: str = Field(min_length=10, max_length=8000)
    task_type: TaskType = TaskType.MODIFY
    source_request: str = Field(default="", max_length=8000)
    scope: str = Field(default="", max_length=2000)
    parent_task_id: int | None = Field(default=None, ge=1)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[str] = Field(default_factory=list, max_length=30)
    files_expected: list[str] = Field(default_factory=list, max_length=50)
    allowed_paths: list[str] = Field(default_factory=lambda: ["."], max_length=50)
    expected_artifacts: list[str] = Field(default_factory=list, max_length=50)
    required_tools: list[str] = Field(default_factory=list, max_length=30)
    required_permissions: list[RiskLevel] = Field(default_factory=list, max_length=20)
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    stages: list[StageSpec] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)
    requires_file_changes: bool | None = None
    token_budget: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=0, ge=0, le=100)
    max_minutes: int = Field(default=0, ge=0, le=10080)
    routing_level: int = Field(default=0, ge=0, le=3)

    @field_validator("acceptance_criteria", "steps", "constraints", "risks")
    @classmethod
    def _no_blank_items(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) != len(value):
            raise ValueError("list items must be non-blank")
        return cleaned

    @field_validator("allowed_paths", "expected_artifacts", "files_expected")
    @classmethod
    def _safe_relative_paths(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            item = item.strip()
            if not item:
                continue
            path = PathLike(item)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"paths in the task contract must stay inside the project: {item}")
            cleaned.append(item)
        return cleaned

    @field_validator("required_tools")
    @classmethod
    def _safe_tool_names(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            if not item.strip().replace("_", "").isalnum():
                raise ValueError(f"invalid tool name: {item!r}")
            cleaned.append(item.strip().lower())
        return cleaned

    @model_validator(mode="after")
    def _derive_file_change_requirement(self) -> TaskSpec:
        if self.requires_file_changes is None:
            self.requires_file_changes = self.task_type in {
                TaskType.MODIFY,
                TaskType.COMPOSITE,
            }
        if self.task_type is TaskType.READ_ONLY and self.requires_file_changes:
            raise ValueError("READ_ONLY task cannot require file changes")
        if self.task_type is TaskType.COMPOSITE:
            if not self.stages:
                raise ValueError("COMPOSITE task requires at least one executable stage")
            self._validate_stage_graph()
        elif self.stages:
            raise ValueError("only a COMPOSITE task may declare stages")
        return self

    def _validate_stage_graph(self) -> None:
        ids = [stage.id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("COMPOSITE stage ids must be unique")
        known = set(ids)
        for stage in self.stages:
            unknown = [dep for dep in stage.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"stage {stage.id}: unknown dependencies: {unknown}")
            if stage.id in stage.depends_on:
                raise ValueError(f"stage {stage.id}: cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()
        graph = {stage.id: stage.depends_on for stage in self.stages}

        def visit(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                raise ValueError(f"COMPOSITE dependency cycle includes {node}")
            visiting.add(node)
            for parent in graph[node]:
                visit(parent)
            visiting.remove(node)
            visited.add(node)

        for stage_id in ids:
            visit(stage_id)


class IssueSeverity(StrEnum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class RootCause(StrEnum):
    UNKNOWN = "unknown"
    EXECUTOR_ERROR = "executor_error"
    PLANNER_ERROR = "planner_error"
    ENVIRONMENT_ERROR = "environment_error"
    PERMISSION_ERROR = "permission_error"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EXTERNAL_SERVICE_ERROR = "external_service_error"


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    REWORK_REQUIRED = "rework_required"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"
    FAILED = "failed"

    # Backward-compatible aliases for stored v1 reviews and callers.
    ACCEPT = APPROVED
    REWORK = REWORK_REQUIRED
    FAIL = FAILED

    @classmethod
    def _missing_(cls, value: object) -> ReviewVerdict | None:
        if isinstance(value, str):
            return {
                "accept": cls.APPROVED,
                "approved": cls.APPROVED,
                "rework": cls.REWORK_REQUIRED,
                "rework_required": cls.REWORK_REQUIRED,
                "approval_required": cls.APPROVAL_REQUIRED,
                "blocked": cls.BLOCKED,
                "fail": cls.FAILED,
                "failed": cls.FAILED,
            }.get(value.lower())
        return None


class ReviewIssue(BaseModel):
    """A concrete, evidence-backed review finding."""

    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(default="", max_length=1000)
    evidence: str = Field(default="", max_length=4000)
    severity: IssueSeverity = IssueSeverity.MAJOR
    recommendation: str = Field(default="", max_length=2000)
    within_authority: bool = True


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    summary: str = Field(min_length=1, max_length=4000)
    root_cause: RootCause = RootCause.UNKNOWN
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=30)
    required_changes: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("issues", mode="before")
    @classmethod
    def _accept_legacy_issue_strings(cls, value: object) -> object:
        if isinstance(value, list):
            return [
                ReviewIssue(evidence=item, severity=IssueSeverity.MAJOR)
                if isinstance(item, str)
                else item
                for item in value
            ]
        return value


class TokenUsage(BaseModel):
    """Provider-reported usage. Never synthesise values the provider omitted."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    def merge(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            requests=self.requests + other.requests,
            duration_ms=self.duration_ms + other.duration_ms,
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
    request_id: str | None = None
    source_request: str = ""
    request_revision: int = 1
    intent_source: str = "legacy-inferred"
    status: TaskStatus
    task_type: TaskType = TaskType.MODIFY
    parent_task_id: int | None = None
    spec_json: str = "{}"
    worker_name: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    rework_count: int = 0
    max_rework: int = 3
    attempts: int = 0
    stalled_count: int = 0
    progress_hash: str | None = None
    blocked_reason: str | None = None
    review_json: str | None = None
    evidence_json: str | None = None
    report: str | None = None
    error: str | None = None
    session_id: str | None = None
    dsh_home: str | None = None
    diff_path: str | None = None
    logs_path: str | None = None
    cancel_requested: bool = False
    ui_chat_id: int | None = None
    ui_message_id: int | None = None
    ui_hash: str | None = None
    ui_updated_at: datetime | None = None
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


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    task_id: int
    stage_key: str
    position: int
    operation: StageOperation
    executor: StageExecutor
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    required_permissions: list[RiskLevel] = Field(default_factory=list)
    source: str | None = None
    destination: str | None = None
    overwrite: bool = False
    commit_message: str | None = None
    repo_name: str | None = None
    repo_owner: str | None = None
    private: bool = True
    remote: str = "origin"
    branch: str | None = None
    merge: bool = True
    instruction: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    require_change: bool = False
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    approval_id: int | None = None
    approval_granted: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_db_row(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        import json

        data = dict(value)
        for source, target in (
            ("depends_on_json", "depends_on"),
            ("acceptance_criteria_json", "acceptance_criteria"),
            ("inputs_json", "inputs"),
            ("outputs_json", "outputs"),
            ("required_permissions_json", "required_permissions"),
            ("allowed_paths_json", "allowed_paths"),
            ("result_json", "result"),
        ):
            raw = data.get(source)
            if isinstance(raw, str):
                try:
                    data[target] = json.loads(raw)
                except json.JSONDecodeError:
                    data[target] = [] if target != "result" else {}
        for source, target in (
            ("overwrite", "overwrite"),
            ("private", "private"),
            ("merge", "merge"),
            ("require_change", "require_change"),
            ("approval_granted", "approval_granted"),
        ):
            if isinstance(data.get(source), int):
                data[target] = bool(data[source])
            elif data.get(source) is None:
                data[target] = False
        return data


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


class ProjectPolicy(BaseModel):
    """Per-project policy. Defaults preserve Phase 1 behaviour."""

    model_config = ConfigDict(extra="forbid")

    allow_git_push: bool = False
    allow_remote_change: bool = False
    allow_system_changes: bool = False
    allow_config_import: bool = False
    auto_approve_medium: bool = True
    allowed_read_paths: list[str] = Field(default_factory=list, max_length=50)
    blocked_globs: list[str] = Field(default_factory=list, max_length=50)
    token_budget: int = Field(default=0, ge=0)
    max_rework: int = Field(default=3, ge=0, le=10)


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
    remote_url: str | None = None
    permissions_json: str = "{}"
    extra_read_paths_json: str = "[]"
    token_budget: int = 0
    max_rework: int = 3
    policy_json: str = "{}"
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
    ui_mode: str = "compact"
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
    category: str = "verified_fact"
    task_id: int | None = None
    applicability: str = ""
    verified: bool = False
    stale: bool = False
    last_confirmed_at: datetime | None = None
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
