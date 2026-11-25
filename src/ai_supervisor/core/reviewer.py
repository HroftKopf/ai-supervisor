from __future__ import annotations

from datetime import datetime

from ai_supervisor.core.usage import UsageService
from ai_supervisor.models import (
    CheckResult,
    Evidence,
    ReviewResult,
    ReviewVerdict,
    TaskRecord,
    TaskSpec,
    TaskStatus,
)


class ReviewGate:
    """Deterministic checks that a model cannot override."""

    @staticmethod
    def precheck(evidence: Evidence, spec: TaskSpec) -> ReviewResult | None:
        if not evidence.worker.completed:
            reason = (
                evidence.worker.error
                or evidence.worker.finish_reason
                or "исполнитель не завершил работу"
            )
            return ReviewResult(
                verdict=ReviewVerdict.FAIL,
                summary=f"Исполнитель не завершил задачу успешно: {reason}.",
                issues=[str(reason)],
                required_changes=[],
            )
        if not evidence.has_changes:
            return ReviewResult(
                verdict=ReviewVerdict.REWORK,
                summary="Исполнитель не изменил ни одного файла, хотя задача требует изменений.",
                issues=["git diff пуст; фактических изменений нет."],
                required_changes=[
                    "Внести изменения, предусмотренные ТЗ, и повторно запустить проверки."
                ],
            )
        failed = [check for check in evidence.checks if not check.passed]
        if failed:
            details = "; ".join(
                f"{check.name}: exit={check.exit_code} timed_out={check.timed_out}"
                for check in failed
            )
            return ReviewResult(
                verdict=ReviewVerdict.REWORK,
                summary=f"Проверки оркестратора не пройдены: {details}.",
                issues=[
                    f"Проверка {check.name} завершилась с exit={check.exit_code}"
                    for check in failed
                ],
                required_changes=[
                    "Исправить ошибки и добиться успешного прохождения настроенных проверок."
                ],
            )
        return None

    @staticmethod
    def enforce(review: ReviewResult, evidence: Evidence) -> ReviewResult:
        # A model must not flip a deterministic failure into acceptance.
        if review.verdict is ReviewVerdict.ACCEPT:
            if not evidence.has_changes:
                return ReviewResult(
                    verdict=ReviewVerdict.REWORK,
                    summary="Ревьюер попытался принять пустой diff; это запрещено оркестратором.",
                    issues=["Нет фактических изменений."],
                    required_changes=["Внести изменения по ТЗ."],
                )
            failed = [check for check in evidence.checks if not check.passed]
            if failed:
                return ReviewResult(
                    verdict=ReviewVerdict.REWORK,
                    summary="Ревьюер попытался принять работу с проваленными проверками; это запрещено.",
                    issues=[f"Провалена проверка {check.name}" for check in failed],
                    required_changes=["Исправить проверки."],
                )
        if review.verdict is ReviewVerdict.ACCEPT and evidence.has_unrun_checks:
            note = "Настроенные проверки отсутствуют; приёмка основана на diff и ревью."
            if note not in review.summary:
                review = review.model_copy(update={"summary": review.summary + "\n" + note})
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
    status_label = {
        TaskStatus.WAITING_APPROVAL: "ожидает принятия",
        TaskStatus.COMPLETED: "завершена",
        TaskStatus.FAILED: "не завершена",
        TaskStatus.REWORK: "отправлена на доработку",
        TaskStatus.CANCELLED: "отменена",
    }.get(final_status, final_status.value)
    lines = [
        f"Задача #{task.id:03d} — {status_label}",
        "",
        f"Проект: {project_name}",
        "Исполнитель: DeepSeek Harness",
        f"Ветка: {task.branch or '(нет)'}",
        "",
        "Выполнено:",
        f"- {spec.goal}",
    ]
    for criterion in spec.acceptance_criteria[:5]:
        lines.append(f"- Критерий: {criterion}")
    lines.extend(
        [
            "",
            f"Изменено файлов: {changed}",
            f"Проверки: {checks_summary(evidence.checks)}",
            f"Ревью Kimi: {review.verdict.value}",
            f"Доработок: {task.rework_count}",
            f"Время выполнения: {minutes} мин {seconds} сек",
        ]
    )
    if evidence.diff_truncated:
        lines.append("Diff передан в усечённом виде; полный патч сохранён локально.")
    if evidence.verification_note:
        lines.append(f"Примечание: {evidence.verification_note}")
    if task.error:
        lines.append(f"Причина остановки: {task.error[:1000]}")
    lines.append("")
    lines.append("Статус: " + status_label)
    token_lines: list[str] = []
    if usage_service.settings.usage.configured:
        for provider in ("kimi", "deepseek"):
            totals = usage_totals.get(provider)
            if totals is None:
                continue
            cost = usage_service.estimate_cost(totals, provider)  # type: ignore[arg-type]
            token_lines.append(
                f"{provider}: {usage_service.format_totals(totals)}"
                + (f", оценка ${cost:.4f}" if cost is not None else "")
            )
    else:
        for provider, totals in usage_totals.items():
            token_lines.append(f"{provider}: {usage_service.format_totals(totals)}")  # type: ignore[arg-type]
        if token_lines:
            token_lines.append("Стоимость не рассчитана: цены не заданы в config.toml.")
    if token_lines:
        lines.append("")
        lines.extend(token_lines)
    if review.issues:
        lines.append("")
        lines.append("Замечания ревью:")
        for issue in review.issues[:5]:
            lines.append(f"- {issue}")
    if cleanup_note:
        lines.append("")
        lines.append(f"Примечание: {cleanup_note}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900].rsplit("\n", 1)[0] + "\n… (отчёт сокращён)"
    return text


def format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z") if value else "—"
