from __future__ import annotations

from pathlib import Path

import pytest

from ai_supervisor.config import ConfigError, load_settings


def _roots(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    return root


def _write(tmp_path: Path, text: str, *, env: str = "") -> tuple[Path, Path, Path]:
    root = _roots(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(text, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(env, encoding="utf-8")
    env_file.chmod(0o600)
    return root, config, env_file


def _load(tmp_path: Path, text: str, *, env: str = ""):
    root, config, env_file = _write(tmp_path, text, env=env)
    return load_settings(
        root=root,
        config_path=config,
        env_path=env_file,
        check_env_permissions=False,
    )


def test_grouped_layout_and_example_are_valid(tmp_path: Path) -> None:
    root = _roots(tmp_path)
    text = Path("config.example.toml").read_text(encoding="utf-8")
    text = text.replace("/home/youruser/Projects", str(root))
    settings = _load(tmp_path, text)
    assert settings.kimi.model == "k3.1-pro"
    assert settings.deepseek.model == "deepseek-v4-flash"
    assert settings.sandbox.backend == "bwrap"
    assert settings.context.max_diff_chars == 80000
    assert settings.backups.keep == 20


def test_legacy_flat_config_is_migrated_and_context_moved(tmp_path: Path) -> None:
    text = f"""
[app]
data_dir = "var"
log_level = "debug"
task_log_retention_days = 7

[kimi]
model = "k3.1-pro"
base_url = "https://api.kimi.ai/coding/v1"

[deepseek]
model = "legacy-model"

[security]
allowed_project_roots = ["{_roots(tmp_path)}"]
max_file_read_bytes = 123456
max_diff_chars = 45678

[limits]
max_workers = 2
max_rework = 1

[sandbox]
network = true

[egress]
enabled = false
"""
    settings = _load(tmp_path, text)
    assert settings.app.log_level == "DEBUG"
    assert settings.app.task_log_retention_days == 7
    assert settings.limits.max_workers == 2
    assert settings.limits.max_rework == 1
    assert settings.context.max_file_read_bytes == 123456
    assert settings.context.max_diff_chars == 45678
    assert settings.egress.enabled is False


def test_unknown_section_and_setting_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown top-level"):
        _load(tmp_path, "[mystery]\nx = 1\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        _load(tmp_path, "[application]\ntypo = 1\n")


def test_unknown_provider_and_agent_role_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown provider"):
        _load(tmp_path, "[providers.mystery]\nmodel='x'\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        _load(tmp_path, "[agent_roles]\nbogus = 'kimi'\n")


def test_wrong_types_and_unsupported_provider_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid configuration"):
        _load(tmp_path, "[scheduler]\nmax_workers = 'many'\n")
    with pytest.raises(ConfigError, match="unknown LLM provider"):
        _load(tmp_path, "[agent_roles]\nchat = 'mystery'\n")


def test_legacy_env_urls_are_a_compatible_override(tmp_path: Path) -> None:
    text = """
[providers.kimi]
base_url = "https://api.kimi.ai/coding/v1"
model = "k3.1-pro"
"""
    settings = _load(
        tmp_path,
        text,
        env="KIMI_BASE_URL=https://override.example/v1\nKIMI_MODEL=override-model\n",
    )
    assert settings.kimi.base_url == "https://override.example/v1"
    assert settings.kimi.model == "override-model"


def test_secrets_are_read_from_env_and_not_from_config(tmp_path: Path, monkeypatch) -> None:
    for key in (
        "KIMI_API_KEY",
        "DEEPSEEK_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    text = """
[providers.kimi]
base_url = "https://api.kimi.ai/coding/v1"
model = "k3.1-pro"
"""
    settings = _load(
        tmp_path,
        text,
        env="KIMI_API_KEY=test-key\nDEEPSEEK_API_KEY=test-deepseek\nTELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_OWNER_ID=123\n",
    )
    assert settings.kimi_key == "test-key"
    assert settings.deepseek_key == "test-deepseek"
    assert settings.telegram_token == "test-token"
    assert settings.owner_id == 123
    assert "test-key" not in settings.config_path.read_text(encoding="utf-8")


def test_templates_and_local_env_are_safe() -> None:
    env_keys = {
        line.split("=", 1)[0].strip()
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert env_keys == {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_ID",
        "KIMI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "GITHUB_TOKEN",
    }
    local_keys = {
        line.split("=", 1)[0].strip()
        for line in Path(".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert local_keys <= env_keys
    assert not ({"KIMI_BASE_URL", "KIMI_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"} & local_keys)
    assert Path(".env").stat().st_mode & 0o077 == 0
    example = Path("config.example.toml").read_text(encoding="utf-8")
    assert "/home/hks" not in example
    assert "allowed_project_roots" in example
    assert "[application]" in example and "[backups]" in example
