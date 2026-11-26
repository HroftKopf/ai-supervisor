from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_supervisor.config import ConfigError, load_settings
from ai_supervisor.core.permissions import Action, ApprovalError, ApprovalService, PermissionDenied
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.memory.projects import ProjectError, ProjectService


def test_wrong_platform_model_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    monkeypatch.setenv("KIMI_MODEL", "k3.1-pro")
    with pytest.raises(ConfigError):
        load_settings(check_env_permissions=False)


def test_username_owner_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "@someone")
    with pytest.raises(ConfigError):
        load_settings(check_env_permissions=False)


def test_redactor_removes_secrets_and_patterns() -> None:
    redactor = SecretRedactor(["topsecretvalue"])
    text = (
        "token=topsecretvalue\n"
        "key sk-abcdefghijklmnop\n"
        "telegram 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n"
        "Authorization: Bearer abcdefghijklmnop\n"
    )
    cleaned = redactor.redact(text)
    assert "topsecretvalue" not in cleaned
    assert "sk-abcdefghijklmnop" not in cleaned
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" not in cleaned
    assert "abcdefghijklmnop" not in cleaned
    assert cleaned.count("[REDACTED]") >= 3


def test_secret_file_permissions(settings, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KIMI_API_KEY=x\n")
    os.chmod(env_file, 0o644)
    settings.env_path = env_file
    settings.security.require_env_mode_600 = True
    with pytest.raises(ConfigError):
        settings.assert_secret_file_permissions()


@pytest.mark.asyncio
async def test_project_path_traversal_rejected(database, settings, tmp_path: Path) -> None:
    service = ProjectService(database, settings)
    with pytest.raises(ProjectError):
        await service.add("outside", str(tmp_path))
    evil = Path(settings.security.allowed_project_roots[0]) / "link-to-etc"
    evil.symlink_to("/etc", target_is_directory=True)
    with pytest.raises(ProjectError):
        await service.add("evil", str(evil))
    with pytest.raises(ProjectError):
        await service.add("relative", "relative/path")


@pytest.mark.asyncio
async def test_approval_is_scoped_expires_and_single_use(database) -> None:
    service = ApprovalService(database)
    approval_id = await service.request(
        action=Action.ROLLBACK,
        payload={"task_id": 7},
        created_by=42,
        ttl_seconds=600,
    )
    assert await service.latest_pending(user_id=42)
    with pytest.raises(PermissionDenied):
        await service.approve_pending(approval_id, user_id=99)
    await service.approve_pending(approval_id, user_id=42)
    payload = await service.consume(approval_id, user_id=42, action=Action.ROLLBACK)
    assert payload == {"task_id": 7}
    with pytest.raises(ApprovalError):
        await service.consume(approval_id, user_id=42, action=Action.ROLLBACK)
    with pytest.raises(PermissionDenied):
        await service.request(
            action=Action.BYPASS_PERMISSIONS,
            payload={},
            created_by=42,
        )


@pytest.mark.asyncio
async def test_expired_approval_cannot_be_approved(database) -> None:
    service = ApprovalService(database)
    approval_id = await service.request(
        action=Action.ROLLBACK, payload={"task_id": 1}, created_by=42, ttl_seconds=600
    )
    await database.execute(
        "UPDATE approvals SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (approval_id,)
    )
    with pytest.raises(ApprovalError):
        await service.approve_pending(approval_id, user_id=42)


@pytest.mark.asyncio
async def test_repository_with_tracked_secret_is_rejected(
    database, settings, tmp_path: Path
) -> None:
    import subprocess

    root = Path(settings.security.allowed_project_roots[0]) / "with-secret"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / ".env").write_text("KIMI_API_KEY=leaked\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "bad"], cwd=root, check=True)
    service = ProjectService(database, settings)
    with pytest.raises(ProjectError):
        await service.add("with-secret", str(root))


@pytest.mark.asyncio
async def test_project_settings_and_remote_validation(database, settings, repo, project) -> None:
    from ai_supervisor.models import ProjectPolicy

    service = ProjectService(database, settings)
    extra = repo / "README.md"
    project = await service.update_instructions(
        project.id,
        remote_url="https://github.com/owner/repo",
        token_budget=12345,
        max_rework=4,
        extra_read_paths=[str(extra)],
        policy=ProjectPolicy(allow_git_push=True, max_rework=4),
    )
    assert project.remote_url == "https://github.com/owner/repo"
    assert project.token_budget == 12345
    assert project.max_rework == 4
    assert "allow_git_push" in project.policy_json
    assert str(extra) in project.extra_read_paths_json
    with pytest.raises(ProjectError):
        await service.update_instructions(project.id, remote_url="https://github.com/owner")
    with pytest.raises(ProjectError):
        await service.update_instructions(project.id, remote_url="https://user:pass@github.com/a/b")


def test_unknown_top_level_config_section_is_rejected(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[app]\ndata_dir='var'\n[unknown]\nvalue=1\n", encoding="utf-8")
    # A minimal config plus an unknown section must fail before defaults apply.
    with pytest.raises(ConfigError):
        load_settings(config_path=config, env_path=tmp_path / ".env", check_env_permissions=False)


def test_worker_uses_unshared_network_and_egress_socket(settings, tmp_path) -> None:
    from ai_supervisor.core.sandbox import BwrapSandbox, SandboxPaths

    sandbox = BwrapSandbox(settings)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dsh_home = tmp_path / "dsh"
    dsh_home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    argv = sandbox.worker_argv(
        SandboxPaths(worktree=worktree, dsh_home=dsh_home, state_dir=state),
        session_id="s",
        model="deepseek-v4-flash",
        provider="deepseek-official",
        profile="sdk-minimal",
        max_tokens=1024,
        reasoning_effort="",
        system_prompt="x",
        deepseek_api_key="secret",
        deepseek_base_url="https://api.deepseek.com",
        resume=False,
        egress_socket="/state/egress.sock",
    )
    assert "--unshare-net" in argv
    assert "--unshare-user" in argv
    assert "CAP_NET_ADMIN" in argv
    assert "/state/egress.sock" in argv
