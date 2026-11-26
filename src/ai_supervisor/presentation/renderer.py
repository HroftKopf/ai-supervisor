from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime

from ai_supervisor.models import (
    Evidence,
    ProjectRecord,
    ReviewResult,
    StageRecord,
    StageStatus,
    TaskRecord,
    TaskSpec,
    TaskStatus,
)

MAX_CARD_CHARS = 3800

_STATUS_RU = {
    TaskStatus.CREATED: "создана",
    TaskStatus.PLANNING: "планируется",
    TaskStatus.QUEUED: "в очереди",
    TaskStatus.RUNNING: "выполняется",
    TaskStatus.VALIDATING: "проверяется",
    TaskStatus.REVIEWING: "на ревью",
    TaskStatus.REWORK: "на доработке",
    TaskStatus.WAITING_APPROVAL: "ожидает подтверждения",
    TaskStatus.WAITING_INPUT: "ожидает данных",
    TaskStatus.RETRY_WAIT: "ожидает повтора",
    TaskStatus.PARTIALLY_COMPLETED: "частично завершена",
    TaskStatus.BLOCKED: "заблокирована",
    TaskStatus.COMPLETED: "завершена",
    TaskStatus.FAILED: "не завершена",
    TaskStatus.PAUSED: "приостановлена",
    TaskStatus.CANCELLED: "отменена",
    TaskStatus.INTERRUPTED: "прервана",
}

_STAGE_RU = {
    StageStatus.PENDING: "ожидает",
    StageStatus.READY: "готова",
    StageStatus.RUNNING: "выполняется",
    StageStatus.WAITING_APPROVAL: "ожидает подтверждения",
    StageStatus.COMPLETED: "завершён",
    StageStatus.FAILED: "ошибка",
    StageStatus.BLOCKED: "заблокирован",
    StageStatus.CANCELLED: "отменён",
    StageStatus.SKIPPED: "пропущен",
}


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _b(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def _code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    if not re.match(r"^https?://", value):
        return None
    return html.escape(value, quote=True)


def _finalize(lines: Iterable[str]) -> str:
    text = "\n".join(line for line in lines if line is not None).strip()
    if len(text) <= MAX_CARD_CHARS:
        return text
    cut = text[: MAX_CARD_CHARS - 20]
    if "<" in cut and cut.rfind(">") < cut.rfind("<"):
        cut = cut[: cut.rfind("<")]
    return cut.rstrip() + "\n…"


def _duration(task: TaskRecord) -> str:
    end = task.finished_at or datetime.now(UTC)
    seconds = max(0, int((end - task.created_at).total_seconds()))
    return f"{seconds // 60} мин {seconds % 60} сек"


def _task_head(task: TaskRecord, project: ProjectRecord | None) -> list[str]:
    project_name = project.name if project else "—"
    return [
        _b(task.title),
        f"Задача #{task.id:03d} · {esc(project_name)}",
        "",
        f"{esc(_STATUS_RU.get(task.status, task.status.value))} · {esc(_duration(task))}",
    ]


def _stage_line(stage: StageRecord) -> str:
    icon = {
        StageStatus.COMPLETED: "✓",
        StageStatus.SKIPPED: "✓",
        StageStatus.RUNNING: "→",
        StageStatus.READY: "→",
        StageStatus.WAITING_APPROVAL: "⏸",
        StageStatus.FAILED: "✕",
        StageStatus.BLOCKED: "✕",
        StageStatus.CANCELLED: "×",
    }.get(stage.status, "○")
    result = stage.result or {}
    suffix = ""
    if "commit" in result:
        suffix = f" · {_code(str(result['commit'])[:12])}"
    if "secret_scan" in result:
        suffix += f" · secret-scan: {esc(result['secret_scan'])}"
    return f"{icon} {esc(stage.goal[:100])}{suffix}"


def progress_card(
    task: TaskRecord,
    project: ProjectRecord | None,
    stages: list[StageRecord],
    *,
    mode: str = "compact",
) -> str:
    lines = _task_head(task, project)
    if stages:
        done = sum(stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED} for stage in stages)
        prefix = {
            TaskStatus.COMPLETED: "Завершено",
            TaskStatus.BLOCKED: "Остановлено",
            TaskStatus.FAILED: "Остановлено",
            TaskStatus.WAITING_APPROVAL: "Ожидает подтверждения",
            TaskStatus.WAITING_INPUT: "Ожидает данных",
        }.get(task.status, "Выполняется")
        lines += ["", f"{prefix} · {done}/{len(stages)} этапов", ""]
        lines.extend(_stage_line(stage) for stage in stages)
    elif task.status in {TaskStatus.PLANNING, TaskStatus.QUEUED}:
        lines += ["", "Планирую задачу. Это может занять несколько секунд."]
    else:
        lines += ["", "Обновляю состояние задачи."]
    if mode != "compact":
        if task.error:
            lines += ["", f"Технически: {_code(task.error[:500])}"]
        if task.branch:
            lines.append(f"Ветка: {_code(task.branch)}")
    return _finalize(lines)


def task_card(task: TaskRecord, project: ProjectRecord | None, *, mode: str = "compact") -> str:
    lines = _task_head(task, project)
    if task.spec:
        lines += ["", esc(task.spec.goal[:600])]
    if mode == "debug" and task.error:
        lines += ["", _code(task.error[:1000])]
    return _finalize(lines)


def completion_card(
    task: TaskRecord,
    project: ProjectRecord | None,
    *,
    evidence: Evidence | None = None,
    review: ReviewResult | None = None,
    stages: list[StageRecord] | None = None,
    model_requests: int | None = None,
    mode: str = "compact",
    repo_url: str | None = None,
) -> str:
    lines = _task_head(task, project)
    lines.append("")
    summary = ""
    if review and review.summary:
        summary = review.summary
    elif task.report:
        summary = task.report.split("\n\n", 1)[0]
    lines.append(esc(summary[:900] or "Задача завершена."))
    lines.append("")
    if evidence and evidence.changed_files:
        lines.append(f"Файлов: {len(evidence.changed_files)}")
    if stages:
        done = sum(stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED} for stage in stages)
        lines.append(f"Этапов: {done}/{len(stages)}")
    if model_requests is not None:
        lines.append(f"Запросов к моделям: {model_requests}")
    if evidence:
        if evidence.checks:
            passed = sum(check.passed for check in evidence.checks)
            lines.append(f"Проверки: {passed}/{len(evidence.checks)} OK")
        else:
            lines.append("Проверки: не запускались")
        if task.head_commit:
            lines.append(f"Коммит: {_code(task.head_commit[:12])}")
    if repo_url:
        lines.append(f"Репозиторий: {esc(repo_url)}")
    if mode != "compact" and task.error:
        lines += ["", _code(task.error[:1000])]
    return _finalize(lines)


def error_card(
    task: TaskRecord,
    project: ProjectRecord | None,
    *,
    reason: str | None = None,
    mode: str = "compact",
    stages: list[StageRecord] | None = None,
) -> str:
    title, human = classify_error(reason or task.error or "Неизвестная ошибка")
    lines = [_b(title), f"Задача #{task.id:03d} · {esc(project.name if project else '—')}", ""]
    lines.append(f"Причина: {esc(human)}")
    if stages:
        done = sum(stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED} for stage in stages)
        if done:
            lines.append(f"Выполненные этапы сохранены: {done}/{len(stages)}")
    if mode != "compact":
        lines += ["", f"Технически: {_code((reason or task.error or '')[:1000])}"]
    return _finalize(lines)


def approval_card(
    *,
    title: str,
    why: str,
    target: str,
    consequences: str,
    volume: str | None = None,
    details: str | None = None,
) -> str:
    lines = [_b(title), "", f"Что: {esc(why)}", f"Ресурс: {esc(target)}"]
    if volume:
        lines.append(f"Объём: {esc(volume)}")
    lines.append(f"Последствия: {esc(consequences)}")
    if details:
        lines += ["", esc(details[:1000])]
    return _finalize(lines)


def details_view(
    task: TaskRecord,
    project: ProjectRecord | None,
    spec: TaskSpec | None,
    stages: list[StageRecord],
    evidence: Evidence | None,
    *,
    mode: str = "detailed",
) -> str:
    lines = [
        _b(f"Подробности задачи #{task.id:03d}"),
        f"Проект: {esc(project.name if project else '—')}",
        "",
    ]
    if spec:
        lines += [
            f"Запрос: {esc(spec.source_request or task.title)[:800]}",
            f"Цель: {esc(spec.goal)[:1000]}",
        ]
        if spec.acceptance_criteria:
            lines.append("Критерии:")
            lines.extend(f"• {esc(item)[:300]}" for item in spec.acceptance_criteria[:10])
    if stages:
        lines += ["", "Этапы:"]
        for stage in stages:
            result = stage.result or {}
            details: list[str] = []
            if "commit" in result:
                details.append(f"commit {_code(str(result['commit'])[:12])}")
            if "secret_scan" in result:
                details.append(f"secret-scan {esc(result['secret_scan'])}")
            if "repository" in result:
                details.append(esc(result["repository"]))
            if stage.error:
                details.append(f"ошибка: {esc(stage.error[:200])}")
            lines.append(
                f"• {esc(stage.stage_key)} [{esc(stage.status.value)}] — {'; '.join(details) or esc(stage.goal)[:160]}"
            )
    if evidence:
        lines += ["", f"Файлов: {len(evidence.changed_files)}"]
        lines.extend(f"• {esc(name)}" for name in evidence.changed_files[:20])
        if evidence.checks:
            lines.append("Проверки:")
            lines.extend(
                f"• {esc(check.name)}: {'OK' if check.passed else 'FAIL'}"
                for check in evidence.checks
            )
        else:
            lines.append("Проверки не запускались.")
    if task.error:
        lines += ["", f"Ошибка: {_code(task.error[:1000])}"]
    if mode == "debug":
        lines += ["", f"ID: {task.id}; ветка: {_code(task.branch or '—')}"]
    return _finalize(lines)


def project_card(project: ProjectRecord, *, current: bool = False) -> str:
    lines = [_b(project.name), f"Путь: {_code(project.path)}"]
    if project.description:
        lines.append(esc(project.description[:500]))
    lines.append(f"Ветка: {esc(project.base_branch)}")
    if project.remote_url:
        lines.append(f"Remote: {esc(project.remote_url)}")
    if current:
        lines.append("Текущий проект.")
    return _finalize(lines)


def usage_card(
    *,
    period: str,
    totals: str,
    models: list[str] | None = None,
) -> str:
    lines = [_b("Расход API"), f"Период: {esc(period)}", "", esc(totals)]
    if models:
        lines += ["", *[esc(item) for item in models[:10]]]
    return _finalize(lines)


def status_card(*, lines: list[str]) -> str:
    return _finalize([_b("AI Supervisor"), "", *lines])


def settings_card(*, mode: str, lines: list[str]) -> str:
    return _finalize([_b("Настройки"), f"Режим интерфейса: {esc(mode)}", "", *lines])


def main_menu(*, project_name: str | None, active: int, mode: str) -> str:
    lines = [
        _b("AI Supervisor"),
        "Напишите задачу обычным сообщением — планирование и исполнение запустятся автоматически.",
        "",
        f"Проект: {esc(project_name or 'не выбран')}",
        f"Активных задач: {active}",
        f"Режим: {esc(mode)}",
    ]
    return _finalize(lines)


def classify_error(raw: str) -> tuple[str, str]:
    text = raw.lower()
    if any(word in text for word in ("github_token", "authentication", "askpass", "credentials")):
        return (
            "Не удалось авторизоваться в GitHub",
            "GitHub отклонил авторизацию или токен не настроен.",
        )
    if any(word in text for word in ("permission", "forbidden", "not allowed", "policy")):
        return "Недостаточно полномочий", "Операция запрещена политикой безопасности."
    if any(word in text for word in ("timeout", "rate limit", "429", "temporarily", "503")):
        return "Временный сбой", "Внешний сервис временно недоступен; задачу можно повторить."
    if any(word in text for word in ("budget", "token_budget", "лимит")):
        return "Превышен бюджет", "Лимит задачи исчерпан; требуется решение владельца."
    if any(word in text for word in ("not found", "missing", "не найден", "нет данных")):
        return "Не хватает данных", "Для продолжения нужны отсутствующие данные или настройки."
    if any(word in text for word in ("rejected by owner", "отклон")):
        return "Операция отклонена", "Владелец отклонил выполнение этапа."
    if any(word in text for word in ("error", "failed", "exception", "traceback")):
        return "Не удалось завершить задачу", "Оркестратор столкнулся с технической ошибкой."
    return "Задача остановлена", "Причина указана в технических деталях."
