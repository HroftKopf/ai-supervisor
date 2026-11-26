from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ai_supervisor.core.usage import UsageService
from ai_supervisor.core.validation import TaskValidator
from ai_supervisor.models import (
    CheckResult,
    Evidence,
    IssueSeverity,
    ReviewResult,
    ReviewVerdict,
    TaskRecord,
    TaskSpec,
    TaskStatus,
)


class ReviewGate:
    """Deterministic checks that a model cannot override."""

    @staticmethod
    def precheck(
        evidence: Evidence, spec: TaskSpec, worktree: Path | None = None
    ) -> ReviewResult | None:
        validation = TaskValidator.validate(spec=spec, evidence=evidence, worktree=worktree)
        if validation.status == "pass":
            return None
        if validation.status == "failed":
            verdict = ReviewVerdict.FAILED
        elif validation.status == "blocked":
            verdict = ReviewVerdict.BLOCKED
        else:
            verdict = ReviewVerdict.REWORK_REQUIRED
        return ReviewResult(
            verdict=verdict,
            summary="; ".join(
                f"{issue.criterion}: {issue.evidence}" for issue in validation.issues[:3]
            )
            or "Детерминированная проверка не пройдена.",
            issues=validation.issues,
            required_changes=[
                issue.recommendation for issue in validation.issues if issue.recommendation
            ][:20],
        )

    @staticmethod
    def enforce(
        review: ReviewResult, evidence: Evidence, spec: TaskSpec, worktree: Path | None = None
    ) -> ReviewResult:
        # A model must not flip a deterministic failure into acceptance.
        validation = TaskValidator.validate(spec=spec, evidence=evidence, worktree=worktree)
        issues = list(review.issues)
        for issue in validation.issues:
            if issue not in issues:
                issues.append(issue)
        critical_issues = [
            issue for issue in review.issues if issue.severity is IssueSeverity.CRITICAL
        ]
        if review.verdict is ReviewVerdict.APPROVED and critical_issues:
            review = review.model_copy(
                update={
                    "verdict": ReviewVerdict.FAILED,
                    "summary": review.summary
                    + "\nОркестратор запретил приёмку: ревью содержит critical-замечания.",
                }
            )
        if review.verdict is ReviewVerdict.APPROVED and validation.status != "pass":
            if validation.status == "blocked":
                verdict = ReviewVerdict.BLOCKED
            elif validation.status == "failed":
                verdict = ReviewVerdict.FAILED
            else:
                verdict = ReviewVerdict.REWORK_REQUIRED
            review = review.model_copy(
                update={
                    "verdict": verdict,
                    "summary": review.summary
                    + "\nОркестратор не разрешил приёмку: "
                    + "; ".join(issue.evidence for issue in validation.issues[:3]),
                    "issues": issues,
                }
            )
        elif issues:
            review = review.model_copy(update={"issues": issues})
        if review.verdict is ReviewVerdict.APPROVED and validation.notes:
            review = review.model_copy(
                update={"summary": review.summary + "\n" + " ".join(validation.notes)}
            )
        return review


def checks_summary(checks: list[CheckResult]) -> str:
    if not checks:
        return "не настроены (не проверялось оркестратором)"
    return "; ".join(
        f"{check.name}: {'OK' if check.passed else 'FAIL'}"
        + (f" (exit {check.exit_code})" if check.exit_code is not None else "")
        + (" [timeout]" if check.timed_out else "")
        for check in checks
    )


def build_report(
    *,
    task: TaskRecord,
    project_name: str,
    spec: TaskSpec,
    evidence: Evidence,
    review: ReviewResult,
    usage_service: UsageService,
    usage_totals: dict[str, object],
    final_status: TaskStatus,
    cleanup_note: str | None = None,
) -> str:
    changed = len(evidence.changed_files)
    duration = evidence.worker.duration_seconds or 0.0
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    failed_checks = [check for check in evidence.checks if not check.passed]
    status_label = {
        TaskStatus.WAITING_APPROVAL: "ожидает подтверждения",
        TaskStatus.WAITING_INPUT: "ожидает данных",
        TaskStatus.RETRY_WAIT: "ожидает повтора",
        TaskStatus.PARTIALLY_COMPLETED: "частично завершена",
        TaskStatus.BLOCKED: "заблокирована",
        TaskStatus.VALIDATING: "проверяется",
        TaskStatus.COMPLETED: "завершена",
        TaskStatus.FAILED: "не завершена",
        TaskStatus.REWORK: "отправлена на доработку",
        TaskStatus.CANCELLED: "отменена",
    }.get(final_status, final_status.value)
    lines = [
        f"Задача #{task.id:03d} — {status_label}",
        "",
        f"Проект: {project_name}",
        f"Тип: {spec.task_type.value}",
        f"Исполнитель: {task.worker_name or 'DeepSeek Harness'}",
        f"Ветка: {task.branch or '(нет)'}",
        "",
        "Требовалось выполнить:",
        f"- {spec.goal}",
    ]
    for criterion in spec.acceptance_criteria[:5]:
        lines.append(f"- {criterion}")
    lines.extend(
        [
            "",
            "Фактически:",
            f"- Исполнитель сообщил: {(evidence.worker.final_response or '(нет отчёта)')[:700]}",
            f"- Изменено файлов: {changed}",
            f"- Проверки: {checks_summary(evidence.checks)}",
            f"- Ревью: {review.verdict.value} — {review.summary[:500]}",
            f"- Доработок: {task.rework_count}; попыток: {task.attempts}",
            f"- Время исполнителя: {minutes} мин {seconds} сек",
            f"- Время с учётом планирования: {_elapsed_minutes(task)}",
            "",
            "Подтверждено оркестратором:",
        ]
    )
    if evidence.checks:
        confirmed = [check for check in evidence.checks if check.passed]
        if confirmed:
            lines.append("- " + "; ".join(f"{check.name}: OK" for check in confirmed))
        if failed_checks:
            lines.append(
                "- Не пройдены: " + "; ".join(f"{check.name}: FAIL" for check in failed_checks)
            )
    else:
        lines.append("- Проверки не настроены; успешность тестов/сборки не подтверждена.")
    if not spec.requires_file_changes:
        lines.append("- Изменение файлов не требовалось для этого типа задачи.")
    if review.issues:
        lines.append("")
        lines.append("Замечания ревью:")
        for issue in review.issues[:5]:
            lines.append(
                f"- [{issue.severity.value}] {issue.criterion}: {issue.evidence} "
                f"→ {issue.recommendation}"
            )
    if evidence.diff_truncated:
        lines.append("")
        lines.append("Diff передан в усечённом виде; полный патч сохранён локально.")
    if evidence.verification_note:
        lines.append("")
        lines.append(f"Примечание: {evidence.verification_note}")
    if task.error:
        lines.append("")
        lines.append(f"Причина остановки: {task.error[:1000]}")
    lines.append("")
    lines.append("Статус: " + status_label)
    token_lines: list[str] = []
    if usage_service.settings.usage.configured:
        for provider in ("kimi", "deepseek"):
            totals = usage_totals.get(provider)
            if totals is None:
                continue
            estimate = usage_service.estimate_cost(totals, provider)  # type: ignore[arg-type]
            suffix = (
                f", стоимость {estimate[0]:.4f} {estimate[1]}"
                if estimate is not None and estimate[0] > 0
                else ""
            )
            token_lines.append(f"{provider}: {usage_service.format_totals(totals)}{suffix}")
    else:
        for provider, totals in usage_totals.items():
            token_lines.append(f"{provider}: {usage_service.format_totals(totals)}")  # type: ignore[arg-type]
        if token_lines:
            token_lines.append("Стоимость не рассчитана: цены не заданы в config.toml.")
    if token_lines:
        lines.append("")
        lines.extend(token_lines)
    if cleanup_note:
        lines.append("")
        lines.append(f"Примечание: {cleanup_note}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900].rsplit("\n", 1)[0] + "\n… (отчёт сокращён)"
    return text


def _elapsed_minutes(task: object) -> str:
    from ai_supervisor.models import TaskRecord

    if not isinstance(task, TaskRecord):
        return "—"
    end = task.finished_at or datetime.now(UTC)
    seconds = max(0, int((end - task.created_at).total_seconds()))
    return f"{seconds // 60} мин {seconds % 60} сек"


def format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z") if value else "—"
