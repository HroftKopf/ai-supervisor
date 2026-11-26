from __future__ import annotations

import os
from collections.abc import Iterable

# Named boundaries used by recovery tests and, when explicitly enabled in a
# disposable environment, by crash-injection runs. Production default is off.
POINTS = frozenset(
    {
        "before_task_create",
        "after_task_create",
        "before_worker_start",
        "during_worker",
        "after_worker_finish",
        "before_validation",
        "after_validation",
        "before_git_commit",
        "after_git_commit",
        "before_git_push",
        "after_git_push",
        "before_notification",
        "during_memory_update",
    }
)


class FaultInjected(RuntimeError):
    pass


class FaultInjector:
    def __init__(self, points: Iterable[str] = ()) -> None:
        self.points = set(points)
        unknown = self.points - POINTS
        if unknown:
            raise ValueError(f"unknown fault point(s): {', '.join(sorted(unknown))}")

    @classmethod
    def from_env(cls) -> FaultInjector:
        raw = os.environ.get("AI_SUPERVISOR_FAULT_POINTS", "")
        return cls(item.strip() for item in raw.split(",") if item.strip())

    def hit(self, point: str) -> None:
        if point not in POINTS:
            raise ValueError(f"unknown fault point: {point}")
        if point in self.points:
            raise FaultInjected(f"injected fault at {point}")

    def enabled(self, point: str) -> bool:
        return point in self.points
