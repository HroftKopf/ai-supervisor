from __future__ import annotations

from ai_supervisor.core.reviewer import ReviewGate
from ai_supervisor.models import (
    Evidence,
    IssueSeverity,
    ReviewIssue,
    ReviewResult,
    ReviewVerdict,
    TaskSpec,
    TaskType,
    WorkerResult,
)


def _evidence(diff: str = "") -> Evidence:
    return Evidence(
        task_id=1,
        base_commit="a" * 40,
        head_commit="a" * 40,
        changed_files=["README.md"] if diff else [],
        diff=diff,
        worker=WorkerResult(
            session_id="s", final_response="report", finish_reason="completed", return_code=0
        ),
    )


def _spec(task_type: TaskType) -> TaskSpec:
    return TaskSpec(
        title="Review gate test",
        goal="Check that deterministic review rules are enforced",
        task_type=task_type,
        acceptance_criteria=["result is correct"],
    )


def test_read_only_empty_diff_can_be_approved() -> None:
    review = ReviewResult(verdict=ReviewVerdict.APPROVED, summary="audit complete")
    result = ReviewGate.enforce(review, _evidence(), _spec(TaskType.READ_ONLY))
    assert result.verdict is ReviewVerdict.APPROVED


def test_modify_empty_diff_cannot_be_approved() -> None:
    review = ReviewResult(verdict=ReviewVerdict.APPROVED, summary="approved")
    result = ReviewGate.enforce(review, _evidence(), _spec(TaskType.MODIFY))
    assert result.verdict is ReviewVerdict.REWORK_REQUIRED


def test_critical_issue_cannot_be_approved() -> None:
    review = ReviewResult(
        verdict=ReviewVerdict.APPROVED,
        summary="approved",
        issues=[
            ReviewIssue(
                criterion="security",
                evidence="secret leaked",
                severity=IssueSeverity.CRITICAL,
                recommendation="remove secret",
            )
        ],
    )
    result = ReviewGate.enforce(review, _evidence("diff"), _spec(TaskType.MODIFY))
    assert result.verdict is ReviewVerdict.FAILED
