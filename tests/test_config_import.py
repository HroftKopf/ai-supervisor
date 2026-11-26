from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_supervisor.core.config_import import ConfigImportError, ConfigImportService
from ai_supervisor.models import ProjectRecord


def _project(project: ProjectRecord, extra: list[Path]) -> ProjectRecord:
    return project.model_copy(
        update={"extra_read_paths_json": json.dumps([str(path) for path in extra])}
    )


@pytest.mark.asyncio
async def test_import_file_and_directory_with_backup(settings, project, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("theme = 'dark'\n", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "extra.conf").write_text("value = 1\n", encoding="utf-8")
    service = ConfigImportService(settings)
    current = _project(project, [source])
    plan = await service.plan(current, str(source), "configs/app")
    assert plan.file_count == 2
    result = await service.execute(plan)
    assert (Path(project.path) / "configs/app/config.toml").read_text() == "theme = 'dark'\n"
    assert (Path(project.path) / "configs/app/nested/extra.conf").read_text() == "value = 1\n"
    assert result.backup is None

    # Overwrite creates a backup outside the project working tree.
    (source / "config.toml").write_text("theme = 'light'\n", encoding="utf-8")
    plan2 = await service.plan(current, str(source), "configs/app", overwrite=True)
    result2 = await service.execute(plan2)
    assert result2.backup is not None
    assert Path(result2.backup).exists()
    assert (Path(project.path) / "configs/app/config.toml").read_text() == "theme = 'light'\n"


@pytest.mark.asyncio
async def test_import_rejects_outside_source_and_secret(settings, project, tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.conf"
    outside.write_text("ok = 1\n", encoding="utf-8")
    service = ConfigImportService(settings)
    current = _project(project, [allowed])
    with pytest.raises(ConfigImportError, match="outside"):
        await service.plan(current, str(outside), "configs/outside.conf")

    secret = allowed / "secret.conf"
    secret.write_text("API_KEY = 'sk-abcdefghijk1234567890'\n", encoding="utf-8")
    with pytest.raises(ConfigImportError, match="secret"):
        await service.plan(current, str(secret), "configs/secret.conf")


@pytest.mark.asyncio
async def test_import_rejects_traversal_and_changed_source(
    settings, project, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.conf").write_text("value = 1\n", encoding="utf-8")
    service = ConfigImportService(settings)
    current = _project(project, [source])
    with pytest.raises(ConfigImportError, match="destination"):
        await service.plan(current, str(source / "a.conf"), "../escape.conf")
    plan = await service.plan(current, str(source / "a.conf"), "configs/a.conf")
    (source / "a.conf").write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(ConfigImportError, match="changed"):
        await service.execute(plan)


@pytest.mark.asyncio
async def test_import_requires_configured_read_path(settings, project, tmp_path: Path) -> None:
    service = ConfigImportService(settings)
    source = tmp_path / "existing.conf"
    source.write_text("ok = 1\n", encoding="utf-8")
    with pytest.raises(ConfigImportError, match="extra_read_paths"):
        await service.plan(project, str(source), "configs/x")


@pytest.mark.asyncio
async def test_discover_niri_under_extra_root(settings, project, tmp_path: Path) -> None:
    root = tmp_path / "configs"
    niri = root / "niri"
    niri.mkdir(parents=True)
    (niri / "config.kdl").write_text("layout {}\n", encoding="utf-8")
    service = ConfigImportService(settings)
    current = _project(project, [root])
    found = await service.discover(current, "niri")
    assert found == niri.resolve()
