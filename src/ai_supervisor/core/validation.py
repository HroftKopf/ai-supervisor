from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ai_supervisor.models import (
    Evidence,
    IssueSeverity,
    ReviewIssue,
    TaskSpec,
    TaskType,
)


@dataclass(slots=True)
class ValidationResult:
    """Deterministic result that a model is not allowed to override."""

    status: str  # pass | rework | blocked | failed | partial
    issues: list[ReviewIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    progress_hash: str = ""
    checked_artifacts: list[str] = field(default_factory=list)


class TaskValidator:
    """Validate actual execution against the task type and contract."""

    @staticmethod
    def validate(
        *,
        spec: TaskSpec,
        evidence: Evidence,
        worktree: Path | None,
    ) -> ValidationResult:
        issues: list[ReviewIssue] = []
        notes: list[str] = []
        artifacts_checked: list[str] = []

        if not evidence.worker.completed:
            reason = (
                evidence.worker.error
                or evidence.worker.finish_reason
                or "executor did not complete successfully"
            )
            issues.append(
                ReviewIssue(
                    criterion="execution completed",
                    evidence=str(reason),
                    severity=IssueSeverity.CRITICAL,
                    recommendation="Inspect the worker log and retry after fixing the cause.",
                    within_authority=True,
                )
            )
            return ValidationResult(
                status="failed",
                issues=issues,
                notes=notes,
                progress_hash=_progress_hash(evidence),
            )

        # Allowed-path enforcement is independent of task type.
        violations = _path_violations(spec, evidence.changed_files)
        if violations:
            issues.append(
                ReviewIssue(
                    criterion="changes stay within allowed_paths",
                    evidence=", ".join(violations[:20]),
                    severity=IssueSeverity.CRITICAL,
                    recommendation="Revert changes outside allowed_paths and rerun the task.",
                    within_authority=True,
                )
            )
            notes.append("Changes outside allowed_paths are a hard policy violation.")
            return ValidationResult(
                status="blocked",
                issues=issues,
                notes=notes,
                progress_hash=_progress_hash(evidence),
            )

        failed_checks = [check for check in evidence.checks if not check.passed]
        for check in failed_checks:
            issues.append(
                ReviewIssue(
                    criterion=f"configured check: {check.name}",
                    evidence=f"exit={check.exit_code} timed_out={check.timed_out}",
                    severity=IssueSeverity.CRITICAL,
                    recommendation="Fix the failing check and rerun it.",
                    within_authority=True,
                )
            )

        requires_changes = bool(spec.requires_file_changes)
        if requires_changes and not evidence.has_changes:
            issues.append(
                ReviewIssue(
                    criterion="task requires a concrete change",
                    evidence="git diff/commit/untracked evidence is empty",
                    severity=IssueSeverity.MAJOR,
                    recommendation="Implement the required change or explain why the state is already satisfied.",
                    within_authority=True,
                )
            )
        elif not requires_changes and not evidence.has_changes:
            notes.append(
                f"{spec.task_type.value}: empty diff is acceptable; success is judged by the task goal."
            )

        if spec.task_type is TaskType.EXECUTE and not evidence.checks:
            notes.append(
                "EXECUTE task has no configured command checks; execution evidence is unverified by the orchestrator."
            )
        if (
            spec.task_type is TaskType.READ_ONLY
            and not evidence.worker.final_response.strip()
            and not evidence.checks
        ):
            issues.append(
                ReviewIssue(
                    criterion="analysis result",
                    evidence="worker returned no report and no command checks were run",
                    severity=IssueSeverity.MAJOR,
                    recommendation="Provide a factual analysis report or configure allowed checks.",
                    within_authority=True,
                )
            )

        if worktree is not None:
            for artifact in spec.expected_artifacts:
                candidate = (worktree / artifact).resolve()
                try:
                    candidate.relative_to(worktree.resolve())
                except ValueError:
                    issues.append(
                        ReviewIssue(
                            criterion=f"artifact {artifact}",
                            evidence="artifact path escapes the worktree",
                            severity=IssueSeverity.CRITICAL,
                            recommendation="Use a relative path inside the project.",
                            within_authority=True,
                        )
                    )
                    continue
                artifacts_checked.append(artifact)
                if not candidate.exists():
                    issues.append(
                        ReviewIssue(
                            criterion=f"artifact {artifact}",
                            evidence="expected artifact does not exist",
                            severity=IssueSeverity.MAJOR,
                            recommendation="Create the expected artifact or correct the task contract.",
                            within_authority=True,
                        )
                    )

        status = "pass"
        if any(issue.severity is IssueSeverity.CRITICAL for issue in issues):
            status = "rework"
        elif issues:
            status = "rework"
        return ValidationResult(
            status=status,
            issues=issues,
            notes=notes,
            progress_hash=_progress_hash(evidence),
            checked_artifacts=artifacts_checked,
        )


def _progress_hash(evidence: Evidence) -> str:
    digest = hashlib.sha256()
    digest.update(evidence.base_commit.encode())
    digest.update(evidence.head_commit.encode())
    for name in sorted(evidence.changed_files):
        digest.update(name.encode())
    digest.update(evidence.diff.encode())
    for check in evidence.checks:
        digest.update(f"{check.name}:{check.exit_code}:{check.timed_out}".encode())
        digest.update(check.stdout_tail[-2000:].encode())
        digest.update(check.stderr_tail[-2000:].encode())
    digest.update(evidence.worker.final_response[:8000].encode())
    return digest.hexdigest()


def _path_violations(spec: TaskSpec, changed_files: list[str]) -> list[str]:
    if not spec.allowed_paths:
        return []
    allowed = [PurePosixPath(item) for item in spec.allowed_paths]
    violations: list[str] = []
    for raw in changed_files:
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            violations.append(raw)
            continue
        if not any(_is_under(path, prefix) for prefix in allowed):
            violations.append(raw)
    return violations


def _is_under(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    if prefix in {PurePosixPath("."), PurePosixPath("")}:
        return True
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False
