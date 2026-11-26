from __future__ import annotations

from pathlib import Path

import pytest

from ai_supervisor.core.capabilities import CAPABILITIES, capability_block
from ai_supervisor.core.strategies import StrategyLibrary
from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.experience import ExperienceExtractor
from ai_supervisor.models import (
    StageExecutor,
    StageOperation,
    StageRecord,
    StageStatus,
    TaskRecord,
    TaskStatus,
)


def test_capabilities_and_strategy_library_are_explicit() -> None:
    text = capability_block()
    for capability in CAPABILITIES:
        assert capability.name in text
    lib = StrategyLibrary(Path("src/ai_supervisor/strategies"))
    bug = lib.select("Исправь падение теста и добавь регрессию")
    assert "bug_fix" in bug
    config = lib.select("Импортируй конфигурацию Niri и опубликуй в GitHub")
    assert "config_import" in config or "git_publish" in config
    assert "project_bootstrap" not in config


@pytest.mark.asyncio
async def test_memory_categories_upsert_and_stale(database, settings, project) -> None:
    service = ConversationService(database, settings)
    first = await service.add_memory(
        "project",
        "Проверенная стратегия X",
        project_id=project.id,
        key="strategy-x",
        category="successful_strategy",
        verified=True,
    )
    assert first.category == "successful_strategy"
    assert first.verified is True
    updated = await service.upsert_experience(
        project_id=project.id,
        key="strategy-x",
        content="Проверенная стратегия X v2",
        category="successful_strategy",
        task_id=1,
    )
    assert updated.content.endswith("v2")
    await service.update_memory(updated.id, stale=True)
    listed = await service.get_memories(project_id=project.id)
    assert all(item.id != updated.id for item in listed)


@pytest.mark.asyncio
async def test_deterministic_experience_extraction(database, settings, project) -> None:
    service = ConversationService(database, settings)
    extractor = ExperienceExtractor(service)
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    task = TaskRecord(
        id=77,
        project_id=project.id,
        conversation_id=1,
        title="Niri publish",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    stages = [
        StageRecord(
            id=1,
            task_id=77,
            stage_key="publish",
            position=0,
            operation=StageOperation.PUBLISH,
            executor=StageExecutor.GIT,
            goal="publish",
            status=StageStatus.COMPLETED,
            result={"commit": "a" * 40, "remote": "origin", "branch": "main"},
            created_at=now,
            updated_at=now,
        )
    ]
    await extractor.record_success(task, project, stages)
    memories = await service.get_memories(project_id=project.id)
    assert any(item.category == "successful_strategy" for item in memories)
    assert any(item.category == "verified_fact" for item in memories)


@pytest.mark.asyncio
async def test_memory_isolation_stale_and_unverified_labels(database, settings, project) -> None:
    from ai_supervisor.memory.conversations import ConversationService

    service = ConversationService(database, settings)
    other = await service.get_memories(project_id=project.id)
    assert other == []
    active = await service.add_memory(
        "project",
        "release branch is main",
        project_id=project.id,
        key="release",
        category="verified_fact",
        verified=True,
    )
    await service.add_memory(
        "project",
        "maybe the cache is slow",
        project_id=project.id,
        key="hypothesis",
        category="hypothesis",
        verified=False,
    )
    stale = await service.add_memory(
        "project",
        "old branch was develop",
        project_id=project.id,
        key="old",
        category="verified_fact",
        verified=True,
    )
    await service.update_memory(stale.id, stale=True)
    context = await service.memory_context(project_id=project.id, conversation_id=None)
    assert "main" in context
    assert "cache" in context and "неподтверждённое" in context
    assert "develop" not in context
    listed = await service.get_memories(project_id=project.id)
    assert active.id in {item.id for item in listed}
    assert stale.id not in {item.id for item in listed}


@pytest.mark.asyncio
async def test_memory_context_uses_query_relevance(database, settings, project) -> None:
    from ai_supervisor.memory.conversations import ConversationService

    service = ConversationService(database, settings)
    await service.add_memory(
        "project", "alpha release uses rust", project_id=project.id, key="a", verified=True
    )
    await service.add_memory(
        "project", "beta release uses python", project_id=project.id, key="b", verified=True
    )
    context = await service.memory_context(
        project_id=project.id, conversation_id=None, query="alpha"
    )
    assert "alpha release" in context
    assert "beta release" not in context


@pytest.mark.asyncio
async def test_scoped_search_excludes_other_project_before_limit(
    database, settings, project
) -> None:
    from ai_supervisor.memory.conversations import ConversationService
    from ai_supervisor.memory.projects import ProjectService

    service = ConversationService(database, settings)
    projects = ProjectService(database, settings)
    other = await projects.create("other")
    await service.add_memory(
        "project", "alpha-secret-in-other", project_id=other.id, key="other", verified=True
    )
    await service.add_memory(
        "project", "alpha-local-fact", project_id=project.id, key="local", verified=True
    )
    results = await service.search_memories("alpha", project_id=project.id)
    contents = " ".join(item.content for item in results)
    assert "alpha-local-fact" in contents
    assert "alpha-secret-in-other" not in contents
