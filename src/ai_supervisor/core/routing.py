from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from ai_supervisor.models import RiskLevel, TaskSpec, TaskType


class RoutingLevel(IntEnum):
    DETERMINISTIC = 0
    SIMPLE = 1
    STANDARD = 2
    COMPLEX = 3


@dataclass(frozen=True)
class ReasoningProfile:
    effort: str
    max_tokens: int


_COMPLEX_MARKERS = re.compile(
    r"(?i)(архитектур|безопасн|security|рефактор|миграц|переписать|"
    r"нескольк|композит|deploy|инфраструктур|race|concurrency)"
)
_SIMPLE_MARKERS = re.compile(
    r"(?i)(статус|покажи|найди|прочитай|аудит|диагностик|логи|git status|"
    r"проверь|read.only|readonly)"
)


class RoutingPolicy:
    """Small deterministic router. Level 0 is reserved for native commands/tools."""

    def __init__(
        self,
        *,
        level1_effort: str = "low",
        level1_max_tokens: int = 2048,
        level2_effort: str = "high",
        level2_max_tokens: int = 8192,
        level3_effort: str = "max",
        level3_max_tokens: int = 16384,
        classifier_effort: str = "low",
        classifier_max_tokens: int = 2048,
    ) -> None:
        self.profiles = {
            RoutingLevel.SIMPLE: ReasoningProfile(level1_effort, level1_max_tokens),
            RoutingLevel.STANDARD: ReasoningProfile(level2_effort, level2_max_tokens),
            RoutingLevel.COMPLEX: ReasoningProfile(level3_effort, level3_max_tokens),
        }
        self.classifier_profile = ReasoningProfile(classifier_effort, classifier_max_tokens)

    def for_request(self, text: str) -> RoutingLevel:
        if _COMPLEX_MARKERS.search(text or ""):
            return RoutingLevel.COMPLEX
        if _SIMPLE_MARKERS.search(text or ""):
            return RoutingLevel.SIMPLE
        return RoutingLevel.STANDARD

    def for_spec(self, spec: TaskSpec) -> RoutingLevel:
        if spec.task_type in {TaskType.SYSTEM_OPERATION, TaskType.EXTERNAL_OPERATION}:
            return RoutingLevel.COMPLEX
        if spec.task_type is TaskType.COMPOSITE:
            if any(
                stage.required_permissions and RiskLevel.HIGH in stage.required_permissions
                for stage in spec.stages
            ):
                return RoutingLevel.COMPLEX
            return RoutingLevel.STANDARD
        if spec.task_type in {TaskType.READ_ONLY, TaskType.EXECUTE, TaskType.GIT_OPERATION}:
            return RoutingLevel.SIMPLE
        if RiskLevel.HIGH in spec.required_permissions:
            return RoutingLevel.COMPLEX
        return RoutingLevel.STANDARD

    def reasoning(self, level: RoutingLevel) -> ReasoningProfile:
        return self.profiles.get(level, self.profiles[RoutingLevel.STANDARD])
