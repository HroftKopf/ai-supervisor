from __future__ import annotations

import pytest

from ai_supervisor.core.fault_injection import FaultInjected, FaultInjector


def test_fault_injector_requires_named_points() -> None:
    injector = FaultInjector(["after_git_push"])
    assert injector.enabled("after_git_push")
    with pytest.raises(FaultInjected, match="after_git_push"):
        injector.hit("after_git_push")
    with pytest.raises(ValueError):
        FaultInjector(["not-a-point"])


def test_fault_injector_from_env_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_SUPERVISOR_FAULT_POINTS", raising=False)
    assert not FaultInjector.from_env().enabled("after_git_push")
    monkeypatch.setenv("AI_SUPERVISOR_FAULT_POINTS", "before_worker_start,after_git_push")
    injector = FaultInjector.from_env()
    assert injector.points == {"before_worker_start", "after_git_push"}
