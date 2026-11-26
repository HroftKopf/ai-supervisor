from __future__ import annotations

import hashlib

from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.models import ProjectRecord, StageRecord, TaskRecord


class ExperienceExtractor:
    """Deterministic, low-cost extraction of confirmed task experience."""

    def __init__(self, conversations: ConversationService) -> None:
        self.conversations = conversations

    async def record_success(
        self, task: TaskRecord, project: ProjectRecord, stages: list[StageRecord]
    ) -> None:
        if not stages:
            await self.conversations.upsert_experience(
                project_id=project.id,
                key=f"task-success-{task.task_type.value}",
                content=(
                    f"Успешное выполнение задачи типа {task.task_type.value} в {project.name}; "
                    "использован сохранённый план и фактические проверки."
                ),
                category="successful_strategy",
                task_id=task.id,
                applicability=f"project:{project.name}; {task.task_type.value}",
            )
            return
        operations = [
            stage.operation.value for stage in stages if stage.status.value == "completed"
        ]
        if not operations:
            return
        digest = hashlib.sha256(
            (project.name + "|" + ",".join(operations)).encode("utf-8")
        ).hexdigest()[:16]
        content = (
            f"Проверенная стратегия COMPOSITE для {project.name}: "
            f"{' → '.join(operations)}. "
            "Этапы выполняются детерминированными инструментами; завершённые этапы сохраняются, "
            "повтор начинается с незавершённого."
        )
        await self.conversations.upsert_experience(
            project_id=project.id,
            key=f"strategy-{digest}",
            content=content,
            category="successful_strategy",
            task_id=task.id,
            applicability=f"project:{project.name}; composite",
        )
        for stage in stages:
            if stage.operation.value == "publish" and stage.status.value == "completed":
                commit = str((stage.result or {}).get("commit") or "")
                remote = str((stage.result or {}).get("remote") or "")
                branch = str((stage.result or {}).get("branch") or "")
                if commit:
                    await self.conversations.upsert_experience(
                        project_id=project.id,
                        key=f"publish-{branch}-{digest}",
                        content=(
                            f"Публикация {remote}/{branch} подтверждена удалённым SHA "
                            f"{commit[:12]} через GitAuthManager."
                        ),
                        category="verified_fact",
                        task_id=task.id,
                        applicability=f"project:{project.name}; git-publish",
                    )

    async def record_failure(
        self, task: TaskRecord, project: ProjectRecord, *, category: str, reason: str
    ) -> None:
        digest = hashlib.sha256(
            (project.name + "|" + category + "|" + reason[:300]).encode("utf-8")
        ).hexdigest()[:16]
        await self.conversations.upsert_experience(
            project_id=project.id,
            key=f"failure-{digest}",
            content=(f"Наблюдённый failure_pattern ({category}) в {project.name}: {reason[:500]}"),
            category="failure_pattern",
            task_id=task.id,
            applicability=f"project:{project.name}; {category}",
            verified=True,
        )
