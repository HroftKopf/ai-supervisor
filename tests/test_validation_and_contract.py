from __future__ import annotations

from pathlib import Path

import pytest

from ai_supervisor.core.validation import TaskValidator
from ai_supervisor.models import (
    CheckResult,
    Evidence,
    TaskSpec,
    TaskType,
    WorkerResult,
)


def _evidence(**kwargs) -> Evidence:  # type: ignore[no-untyped-def]
    values = {
        "task_id": 1,
        "base_commit": "a" * 40,
        "head_commit": "a" * 40,
        "changed_files": [],
        "diff": "",
        "checks": [],
        "worker": WorkerResult(
            session_id="s",
            final_response="analysis report",
            finish_reason="completed",
            return_code=0,
        ),
    }
    values.update(kwargs)
    return Evidence(**values)


def _spec(task_type: TaskType, **kwargs) -> TaskSpec:  # type: ignore[no-untyped-def]
    values = {
        "title": "Test task",
        "goal": "Perform a test task with factual evidence",
        "task_type": task_type,
        "acceptance_criteria": ["result is verified"],
    }
    values.update(kwargs)
    return TaskSpec(**values)


def test_read_only_with_empty_diff_passes() -> None:
    result = TaskValidator.validate(
        spec=_spec(TaskType.READ_ONLY), evidence=_evidence(), worktree=None
    )
    assert result.status == "pass"
    assert not result.issues


def test_modify_with_empty_diff_requires_rework() -> None:
    result = TaskValidator.validate(
        spec=_spec(TaskType.MODIFY), evidence=_evidence(), worktree=None
    )
    assert result.status == "rework"
    assert any("concrete change" in issue.criterion for issue in result.issues)


def test_modify_with_diff_and_no_checks_passes_with_note() -> None:
    result = TaskValidator.validate(
        spec=_spec(TaskType.MODIFY),
        evidence=_evidence(
            head_commit="b" * 40,
            changed_files=["README.md"],
            diff="diff --git a/README.md b/README.md",
        ),
        worktree=None,
    )
    assert result.status == "pass"


def test_failed_check_blocks_acceptance() -> None:
    result = TaskValidator.validate(
        spec=_spec(TaskType.MODIFY),
        evidence=_evidence(
            head_commit="b" * 40,
            changed_files=["README.md"],
            diff="diff",
            checks=[CheckResult(name="tests", command="pytest", exit_code=1)],
        ),
        worktree=None,
    )
    assert result.status == "rework"
    assert any("configured check" in issue.criterion for issue in result.issues)


def test_path_outside_allowed_paths_is_blocked() -> None:
    result = TaskValidator.validate(
        spec=_spec(TaskType.MODIFY, allowed_paths=["src"]),
        evidence=_evidence(
            head_commit="b" * 40,
            changed_files=["README.md"],
            diff="diff",
        ),
        worktree=None,
    )
    assert result.status == "blocked"
    assert any("allowed_paths" in issue.criterion for issue in result.issues)


def test_expected_artifact_is_checked(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text("ok", encoding="utf-8")
    result = TaskValidator.validate(
        spec=_spec(TaskType.READ_ONLY, expected_artifacts=["report.md"]),
        evidence=_evidence(),
        worktree=tmp_path,
    )
    assert result.status == "pass"
    assert result.checked_artifacts == ["report.md"]


def test_missing_expected_artifact_requires_rework(tmp_path: Path) -> None:
    result = TaskValidator.validate(
        spec=_spec(TaskType.READ_ONLY, expected_artifacts=["missing.md"]),
        evidence=_evidence(),
        worktree=tmp_path,
    )
    assert result.status == "rework"
    assert any("missing.md" in issue.criterion for issue in result.issues)


def test_read_only_cannot_require_file_changes() -> None:
    with pytest.raises(ValueError):
        TaskSpec(
            title="Bad read-only",
            goal="Must not require file changes in read-only mode",
            task_type=TaskType.READ_ONLY,
            acceptance_criteria=["x"],
            requires_file_changes=True,
        )
