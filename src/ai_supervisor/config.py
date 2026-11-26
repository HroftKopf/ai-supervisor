from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

PROJECT_ROOT_MARKER = "pyproject.toml"


class ConfigError(RuntimeError):
    pass


class AppSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: str = "var"
    timezone: str = "Europe/Moscow"
    log_level: str = "INFO"
    task_log_retention_days: int = Field(default=14, ge=1, le=3650)

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value


class KimiSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = "https://api.kimi.ai/coding/v1"
    model: str = "k3.1-pro"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    max_completion_tokens: int = Field(default=32768, ge=1024, le=1_048_576)
    request_timeout_seconds: float = Field(default=180.0, gt=0, le=1800)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_concurrent_requests: int = Field(default=2, ge=1, le=10)
    verify_model_on_startup: bool = True


class TelegramSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    private_chat_only: bool = True
    polling_timeout_seconds: int = Field(default=30, ge=5, le=90)
    max_message_chars: int = Field(default=4000, ge=500, le=4096)


class GitHubSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_url: str = "https://api.github.com"
    timeout_seconds: float = Field(default=30.0, gt=0, le=180)
    default_private: bool = True

    @model_validator(mode="after")
    def _valid_api_url(self) -> GitHubSection:
        parsed = urlparse(self.api_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("github.api_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("github.api_url must not contain credentials/query/fragment")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("github.api_url must use https outside localhost")
        return self


class SecuritySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_project_roots: list[str] = Field(
        default_factory=lambda: [str(Path.home() / "Projects")]
    )
    require_env_mode_600: bool = True
    allow_unsafe_no_sandbox: bool = False
    max_file_read_bytes: int = Field(default=262144, ge=4096, le=10_000_000)
    max_project_context_chars: int = Field(default=30000, ge=4000, le=200000)
    max_diff_chars: int = Field(default=80000, ge=4000, le=1_000_000)
    max_merge_conflict_chars: int = Field(default=12000, ge=1000, le=200000)


class LimitsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_workers: int = Field(default=3, ge=1, le=16)
    max_rework: int = Field(default=3, ge=0, le=10)
    max_task_minutes: int = Field(default=45, ge=1, le=1440)
    max_iterations: int = Field(default=12, ge=1, le=200)
    max_task_kimi_requests: int = Field(default=40, ge=1, le=1000)
    # 0 means unlimited. Applies to all providers for one task.
    max_task_total_tokens: int = Field(default=0, ge=0)
    daily_token_limit: int = Field(default=0, ge=0)
    daily_request_limit: int = Field(default=0, ge=0)
    warn_at_percent: int = Field(default=80, ge=1, le=100)
    max_worker_log_chars: int = Field(default=20000, ge=1000, le=1_000_000)


class DeepSeekSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    profile: str = "sdk-minimal"
    max_tokens: int = Field(default=32768, ge=1024, le=1_048_576)
    reasoning_effort: str = ""
    initialize_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    shutdown_timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class SandboxSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["bwrap", "none"] = "bwrap"
    network: bool = True
    memory_mb: int = Field(default=4096, ge=128, le=131072)
    cpu_seconds_per_worker: int = Field(default=3000, ge=10, le=86400)
    max_open_files: int = Field(default=1024, ge=64, le=65536)
    max_processes: int = Field(default=1024, ge=32, le=65536)


class GitSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author_name: str = "AI Supervisor"
    author_email: str = "ai-supervisor@localhost"
    base_branch: str = "main"
    commit_prefix: str = "ai-supervisor"

    @field_validator("base_branch")
    @classmethod
    def _valid_branch(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", value):
            raise ValueError("base_branch contains invalid characters")
        if ".." in value or value.endswith("/"):
            raise ValueError("base_branch contains invalid path segments")
        return value


class MemorySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_context_messages: int = Field(default=16, ge=2, le=100)
    max_context_chars: int = Field(default=24000, ge=2000, le=200000)
    summarize_after_messages: int = Field(default=40, ge=10, le=1000)
    summary_max_chars: int = Field(default=6000, ge=500, le=50000)
    max_search_results: int = Field(default=20, ge=1, le=100)


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_per_mtok: float = Field(default=0.0, ge=0)
    output_per_mtok: float = Field(default=0.0, ge=0)
    cache_read_per_mtok: float = Field(default=0.0, ge=0)
    cache_write_per_mtok: float = Field(default=0.0, ge=0)
    currency: str = "USD"

    @property
    def configured(self) -> bool:
        return any(
            value > 0
            for value in (
                self.input_per_mtok,
                self.output_per_mtok,
                self.cache_read_per_mtok,
                self.cache_write_per_mtok,
            )
        )


class UsageSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Legacy flat prices remain supported.
    kimi_input: float = Field(default=0.0, ge=0)
    kimi_output: float = Field(default=0.0, ge=0)
    deepseek_input: float = Field(default=0.0, ge=0)
    deepseek_output: float = Field(default=0.0, ge=0)
    currency: str = "USD"
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.prices) or any(
            value > 0
            for value in (
                self.kimi_input,
                self.kimi_output,
                self.deepseek_input,
                self.deepseek_output,
            )
        )

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        for key in (f"{provider}/{model}", f"{provider}:{model}", model):
            if key in self.prices:
                return self.prices[key]
        if provider == "kimi" and (self.kimi_input or self.kimi_output):
            return ModelPrice(
                input_per_mtok=self.kimi_input,
                output_per_mtok=self.kimi_output,
                currency=self.currency,
            )
        if provider == "deepseek" and (self.deepseek_input or self.deepseek_output):
            return ModelPrice(
                input_per_mtok=self.deepseek_input,
                output_per_mtok=self.deepseek_output,
                currency=self.currency,
            )
        return None


class Secrets(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kimi_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    telegram_owner_id: int | None = None

    def missing(self) -> list[str]:
        names: list[str] = []
        if not self.kimi_api_key.get_secret_value().strip():
            names.append("KIMI_API_KEY")
        if not self.deepseek_api_key.get_secret_value().strip():
            names.append("DEEPSEEK_API_KEY")
        if not self.telegram_bot_token.get_secret_value().strip():
            names.append("TELEGRAM_BOT_TOKEN")
        if self.telegram_owner_id is None:
            names.append("TELEGRAM_OWNER_ID")
        return names


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    config_path: Path
    env_path: Path
    app: AppSection
    kimi: KimiSection
    telegram: TelegramSection
    security: SecuritySection
    github: GitHubSection
    limits: LimitsSection
    deepseek: DeepSeekSection
    sandbox: SandboxSection
    git: GitSection
    memory: MemorySection
    usage: UsageSection
    secrets: Secrets

    @model_validator(mode="after")
    def _validate_combined(self) -> Settings:
        validate_provider_url(self.kimi.base_url, label="KIMI_BASE_URL")
        if self.deepseek_base_url:
            validate_provider_url(self.deepseek_base_url, label="DEEPSEEK_BASE_URL")
        self._validate_platform_model_pair()
        if self.sandbox.backend == "none" and not self.security.allow_unsafe_no_sandbox:
            raise ValueError(
                "sandbox.backend='none' requires security.allow_unsafe_no_sandbox=true; "
                "running workers without isolation is refused"
            )
        if self.limits.max_task_minutes * 60 > 7 * 24 * 3600:
            raise ValueError("max_task_minutes is unreasonably large")
        return self

    def _validate_platform_model_pair(self) -> None:
        host = (urlparse(self.kimi.base_url).hostname or "").lower()
        model = self.kimi.model
        kimi_code_ids = {
            "k3.1-pro",
            "k3.1-pro-highspeed",
            "k3",
            "k3-256k",
            "kimi-for-coding",
            "kimi-for-coding-highspeed",
        }
        if host in {"api.kimi.ai", "api.kimi.com"} and model not in kimi_code_ids:
            raise ConfigError(
                f"Kimi Code model ID {model!r} is not in the documented set. "
                "Use k3.1-pro (Kimi Code) or switch KIMI_BASE_URL to https://api.moonshot.ai/v1 "
                "with a kimi-* model ID."
            )
        if host == "api.moonshot.ai" and not model.startswith("kimi-"):
            raise ConfigError(
                f"Moonshot Open Platform model ID {model!r} must start with 'kimi-'; "
                "for example kimi-k3.1-pro. Keys and base URLs are not interchangeable."
            )
        if not urlparse(self.kimi.base_url).path.rstrip("/").endswith("/v1"):
            raise ConfigError(
                "KIMI_BASE_URL must end with /v1 for the OpenAI-compatible Chat Completions API"
            )

    @property
    def data_dir(self) -> Path:
        return self._resolve_data_path(self.app.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ai_supervisor.db"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def dsh_homes_dir(self) -> Path:
        return self.data_dir / "dsh-homes"

    @property
    def task_logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def rollbacks_dir(self) -> Path:
        return self.data_dir / "rollbacks"

    @property
    def deepseek_base_url(self) -> str:
        return os.environ.get("DEEPSEEK_BASE_URL", "").strip()

    @property
    def deepseek_model(self) -> str:
        return os.environ.get("DEEPSEEK_MODEL", "").strip() or self.deepseek.model

    @property
    def deepseek_key(self) -> str:
        return self.secrets.deepseek_api_key.get_secret_value().strip()

    @property
    def kimi_key(self) -> str:
        return self.secrets.kimi_api_key.get_secret_value().strip()

    @property
    def github_token(self) -> str:
        return self.secrets.github_token.get_secret_value().strip()

    @property
    def telegram_token(self) -> str:
        return self.secrets.telegram_bot_token.get_secret_value().strip()

    @property
    def owner_id(self) -> int:
        if self.secrets.telegram_owner_id is None:
            raise ConfigError("TELEGRAM_OWNER_ID is not configured")
        return self.secrets.telegram_owner_id

    def _resolve_data_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.worktrees_dir,
            self.dsh_homes_dir,
            self.task_logs_dir,
            self.uploads_dir,
            self.rollbacks_dir,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        for raw in self.security.allowed_project_roots:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                raise ConfigError(
                    f"allowed project root does not exist or is not a directory: {root}"
                )
            if root == Path("/") or root == Path.home():
                raise ConfigError(f"refusing an over-broad allowed project root: {root}")
            roots.append(root)
        return roots

    def assert_secret_file_permissions(self) -> None:
        if not self.env_path.exists() or not self.security.require_env_mode_600:
            return
        mode = stat.S_IMODE(self.env_path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(
                f"{self.env_path} is accessible by group/others (mode {mode:o}); run: chmod 600 {self.env_path}"
            )

    def public_summary(self) -> dict[str, Any]:
        return {
            "kimi": {
                "base_url": self.kimi.base_url,
                "model": self.kimi.model,
                "reasoning_effort": self.kimi.reasoning_effort,
            },
            "deepseek": {
                "model": self.deepseek_model,
                "profile": self.deepseek.profile,
                "base_url": self.deepseek_base_url or "official default",
            },
            "telegram": {
                "owner_id": self.secrets.telegram_owner_id,
                "private_chat_only": self.telegram.private_chat_only,
            },
            "github": {
                "default_private": self.github.default_private,
                "configured": bool(self.github_token),
            },
            "limits": self.limits.model_dump(),
            "sandbox": self.sandbox.model_dump(),
            "data_dir": str(self.data_dir),
            "prices_configured": self.usage.configured,
        }

    def require_credentials(self) -> None:
        missing = self.secrets.missing()
        if missing:
            raise ConfigError(
                "missing required credentials: "
                + ", ".join(missing)
                + f". Fill {self.env_path} and chmod 600 it."
            )


def validate_provider_url(url: str, *, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{label} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ConfigError(f"{label} must not embed credentials")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and parsed.hostname not in local_hosts:
        raise ConfigError(
            f"{label} must use https (http is allowed only for localhost test endpoints)"
        )


def _find_root(explicit_root: str | Path | None) -> Path:
    if explicit_root is not None:
        return Path(explicit_root).expanduser().resolve()
    env_root = os.environ.get("AI_SUPERVISOR_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / PROJECT_ROOT_MARKER).exists():
        return cwd
    package_root = Path(__file__).resolve().parents[2]
    if (package_root / PROJECT_ROOT_MARKER).exists():
        return package_root
    raise ConfigError(f"cannot locate project root containing {PROJECT_ROOT_MARKER}")


def load_settings(
    *,
    root: str | Path | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    check_env_permissions: bool = True,
) -> Settings:
    project_root = _find_root(root)
    env_file = Path(env_path) if env_path else project_root / ".env"
    config_file = (
        Path(config_path)
        if config_path
        else Path(os.environ.get("AI_SUPERVISOR_CONFIG", project_root / "config.toml"))
    )
    if not config_file.is_absolute():
        config_file = project_root / config_file
    if not config_file.is_file():
        raise ConfigError(f"configuration file not found: {config_file}")
    if env_file.exists():
        load_dotenv(env_file, override=False)

    try:
        with config_file.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_file}: {exc}") from exc

    # Environment wins over config for deployment-specific values.
    kimi = dict(raw.get("kimi", {}))
    if os.environ.get("KIMI_BASE_URL", "").strip():
        kimi["base_url"] = os.environ["KIMI_BASE_URL"].strip()
    if os.environ.get("KIMI_MODEL", "").strip():
        kimi["model"] = os.environ["KIMI_MODEL"].strip()
    raw["kimi"] = kimi
    deepseek = dict(raw.get("deepseek", {}))
    if os.environ.get("DEEPSEEK_MODEL", "").strip():
        deepseek["model"] = os.environ["DEEPSEEK_MODEL"].strip()
    raw["deepseek"] = deepseek

    owner_raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    owner_id: int | None = None
    if owner_raw:
        if not owner_raw.isdigit():
            raise ConfigError(
                "TELEGRAM_OWNER_ID must be a numeric Telegram user ID, not a username"
            )
        owner_id = int(owner_raw)
        if owner_id <= 0:
            raise ConfigError("TELEGRAM_OWNER_ID must be positive")

    secrets = Secrets(
        kimi_api_key=SecretStr(os.environ.get("KIMI_API_KEY", "").strip()),
        deepseek_api_key=SecretStr(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        telegram_bot_token=SecretStr(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        telegram_owner_id=owner_id,
        github_token=SecretStr(
            (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
        ),
    )

    allowed_top_level = {
        "app",
        "kimi",
        "telegram",
        "security",
        "limits",
        "deepseek",
        "sandbox",
        "git",
        "github",
        "memory",
        "usage",
    }
    unknown_sections = set(raw) - allowed_top_level
    if unknown_sections:
        raise ConfigError(
            "unknown top-level config section(s): " + ", ".join(sorted(unknown_sections))
        )

    try:
        settings = Settings(
            root=project_root,
            config_path=config_file.resolve(),
            env_path=env_file.resolve(),
            app=AppSection.model_validate(raw.get("app", {})),
            kimi=KimiSection.model_validate(kimi),
            telegram=TelegramSection.model_validate(raw.get("telegram", {})),
            security=SecuritySection.model_validate(raw.get("security", {})),
            github=GitHubSection.model_validate(raw.get("github", {})),
            limits=LimitsSection.model_validate(raw.get("limits", {})),
            deepseek=DeepSeekSection.model_validate(deepseek),
            sandbox=SandboxSection.model_validate(raw.get("sandbox", {})),
            git=GitSection.model_validate(raw.get("git", {})),
            memory=MemorySection.model_validate(raw.get("memory", {})),
            usage=UsageSection.model_validate(raw.get("usage", {})),
            secrets=secrets,
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

    if check_env_permissions:
        settings.assert_secret_file_permissions()
    # Fail early on over-broad / missing roots.
    settings.allowed_roots()
    return settings
