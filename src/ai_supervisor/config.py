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
    auto_resume_composite: bool = True
    auto_resume_git_auth_blocked: bool = True

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


class LLMSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_provider: Literal["kimi", "openai", "openai_compatible"] = "kimi"
    roles: dict[str, str] = Field(default_factory=dict)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1"
    openai_reasoning: bool = False
    openai_prompt_cache: bool = True
    compatible_base_url: str = ""
    compatible_model: str = ""
    compatible_reasoning: bool = False
    compatible_prompt_cache: bool = False

    @field_validator("roles")
    @classmethod
    def _roles(cls, value: dict[str, str]) -> dict[str, str]:
        allowed_roles = {"chat", "classifier", "planner", "reviewer", "summarizer", "experience"}
        allowed_providers = {"kimi", "openai", "openai_compatible"}
        for role, provider in value.items():
            if role not in allowed_roles:
                raise ValueError(f"unknown LLM role: {role}")
            if provider not in allowed_providers:
                raise ValueError(f"unknown LLM provider: {provider}")
        return value


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
    push_hosts: list[str] = Field(default_factory=lambda: ["github.com"], max_length=20)

    @field_validator("push_hosts")
    @classmethod
    def _push_hosts(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            host = item.strip().lower()
            if not host or any(ch.isspace() for ch in host) or "://" in host or "/" in host:
                raise ValueError("github.push_hosts must contain host names only")
            cleaned.append(host)
        return cleaned

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


class ContextSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_project_context_chars: int = Field(default=30000, ge=4000, le=200000)
    max_file_read_bytes: int = Field(default=262144, ge=4096, le=10_000_000)
    max_diff_chars: int = Field(default=80000, ge=4000, le=1_000_000)


class StrategiesSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_selected: int = Field(default=2, ge=0, le=5)


class BackupsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: str = "var/backups"
    keep: int = Field(default=20, ge=1, le=1000)
    before_schema_migration: bool = True


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
    base_url: str = ""
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


class RoutingSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level1_effort: Literal["low", "high", "max"] = "low"
    level1_max_tokens: int = Field(default=2048, ge=512, le=32768)
    level2_effort: Literal["low", "high", "max"] = "high"
    level2_max_tokens: int = Field(default=8192, ge=1024, le=65536)
    level3_effort: Literal["low", "high", "max"] = "max"
    level3_max_tokens: int = Field(default=16384, ge=2048, le=131072)
    classifier_effort: Literal["low", "high", "max"] = "low"
    classifier_max_tokens: int = Field(default=2048, ge=512, le=16384)


class EgressSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    max_connections: int = Field(default=32, ge=1, le=256)
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443], max_length=10)

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            item = item.strip().lower()
            if not item or any(ch.isspace() for ch in item):
                raise ValueError("egress host must be a hostname without spaces")
            cleaned.append(item)
        return cleaned


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
    # Optional display-only conversion. No rate is invented; empty means disabled.
    display_currency: str = ""
    fx_rates: dict[str, float] = Field(default_factory=dict)

    @field_validator("fx_rates")
    @classmethod
    def _positive_fx(cls, value: dict[str, float]) -> dict[str, float]:
        for key, rate in value.items():
            if rate <= 0:
                raise ValueError(f"usage.fx_rates[{key}] must be positive")
        return value

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
    openai_api_key: SecretStr = SecretStr("")
    openai_compatible_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    telegram_owner_id: int | None = None

    def missing(self) -> list[str]:
        names: list[str] = []
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
    llm: LLMSection
    telegram: TelegramSection
    security: SecuritySection
    context: ContextSection
    strategies: StrategiesSection
    backups: BackupsSection
    github: GitHubSection
    egress: EgressSection
    routing: RoutingSection
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
        providers = {self.llm.default_provider, *self.llm.roles.values()}
        if "openai" in providers:
            validate_provider_url(self.llm.openai_base_url, label="OPENAI_BASE_URL")
            if not self.llm.openai_model.strip():
                raise ConfigError("llm.openai_model must not be empty")
        if "openai_compatible" in providers:
            if not self.llm.compatible_base_url or not self.llm.compatible_model:
                raise ConfigError(
                    "llm.compatible_base_url and llm.compatible_model are required for "
                    "openai_compatible roles"
                )
            validate_provider_url(self.llm.compatible_base_url, label="OPENAI_COMPATIBLE_BASE_URL")
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
        # Environment remains an explicit deployment/test override; config.toml is
        # the normal location for this non-secret value.
        return os.environ.get("DEEPSEEK_BASE_URL", "").strip() or self.deepseek.base_url.strip()

    @property
    def deepseek_model(self) -> str:
        return os.environ.get("DEEPSEEK_MODEL", "").strip() or self.deepseek.model.strip()

    @property
    def backup_dir(self) -> Path:
        return self._resolve_data_path(self.backups.directory)

    @property
    def deepseek_key(self) -> str:
        return self.secrets.deepseek_api_key.get_secret_value().strip()

    @property
    def kimi_key(self) -> str:
        return self.secrets.kimi_api_key.get_secret_value().strip()

    @property
    def openai_key(self) -> str:
        return self.secrets.openai_api_key.get_secret_value().strip()

    @property
    def openai_compatible_key(self) -> str:
        return self.secrets.openai_compatible_api_key.get_secret_value().strip()

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
            "llm": {
                "default_provider": self.llm.default_provider,
                "roles": self.llm.roles,
                "openai_model": self.llm.openai_model,
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
            "egress": {
                "enabled": self.egress.enabled,
                "allowed_hosts": self.egress.allowed_hosts,
                "allowed_ports": self.egress.allowed_ports,
            },
            "data_dir": str(self.data_dir),
            "prices_configured": self.usage.configured,
        }

    def require_credentials(self) -> None:
        missing = self.secrets.missing()
        providers = {self.llm.default_provider, *self.llm.roles.values()}
        if "kimi" in providers and not self.kimi_key:
            missing.append("KIMI_API_KEY")
        if "openai" in providers and not self.openai_key:
            missing.append("OPENAI_API_KEY")
        if "openai_compatible" in providers and not self.openai_compatible_key:
            missing.append("OPENAI_COMPATIBLE_API_KEY")
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


_OLD_TOP_LEVEL = {
    "app",
    "kimi",
    "llm",
    "telegram",
    "security",
    "limits",
    "deepseek",
    "sandbox",
    "git",
    "github",
    "egress",
    "routing",
    "memory",
    "usage",
}
_NEW_TOP_LEVEL = {
    "application",
    "storage",
    "logging",
    "recovery",
    "providers",
    "agent_roles",
    "reasoning",
    "task_limits",
    "scheduler",
    "strategies",
    "context",
    "network",
    "pricing",
    "backups",
}


def _as_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"config section [{key}] must be a table")
    return dict(value)


def _check_keys(section: str, value: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown setting(s) in [{section}]: " + ", ".join(sorted(unknown)))


def _merge(*sections: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section in sections:
        result.update(section)
    return result


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Map the grouped 2.9 layout and the legacy flat layout to internal sections.

    The on-disk layout is deliberately decoupled from the runtime model so old
    config.toml files continue to load. New grouped values win over legacy values.
    """
    unknown = set(raw) - _OLD_TOP_LEVEL - _NEW_TOP_LEVEL
    if unknown:
        raise ConfigError("unknown top-level config section(s): " + ", ".join(sorted(unknown)))

    application = _as_section(raw, "application")
    storage = _as_section(raw, "storage")
    logging = _as_section(raw, "logging")
    recovery = _as_section(raw, "recovery")
    _check_keys(
        "application",
        application,
        {
            "timezone",
            "auto_resume_composite",
            "auto_resume_git_auth_blocked",
            "log_level",
            "task_log_retention_days",
            "data_dir",
        },
    )
    _check_keys("storage", storage, {"data_dir"})
    _check_keys(
        "logging", logging, {"log_level", "task_log_retention_days", "max_worker_log_chars"}
    )
    _check_keys("recovery", recovery, {"auto_resume_composite", "auto_resume_git_auth_blocked"})
    app = _merge(_as_section(raw, "app"), application)
    if "data_dir" in storage:
        app["data_dir"] = storage["data_dir"]
    if "log_level" in logging:
        app["log_level"] = logging["log_level"]
    if "task_log_retention_days" in logging:
        app["task_log_retention_days"] = logging["task_log_retention_days"]
    if "auto_resume_composite" in recovery:
        app["auto_resume_composite"] = recovery["auto_resume_composite"]
    if "auto_resume_git_auth_blocked" in recovery:
        app["auto_resume_git_auth_blocked"] = recovery["auto_resume_git_auth_blocked"]

    providers = _as_section(raw, "providers")
    unknown_providers = set(providers) - {"kimi", "deepseek", "openai", "openai_compatible"}
    if unknown_providers:
        raise ConfigError("unknown provider(s): " + ", ".join(sorted(unknown_providers)))
    provider_kimi = _as_section(providers, "kimi")
    provider_deepseek = _as_section(providers, "deepseek")
    provider_openai = _as_section(providers, "openai")
    provider_compatible = _as_section(providers, "openai_compatible")
    _check_keys("providers.kimi", provider_kimi, set(KimiSection.model_fields))
    _check_keys("providers.deepseek", provider_deepseek, set(DeepSeekSection.model_fields))
    _check_keys(
        "providers.openai", provider_openai, {"base_url", "model", "reasoning", "prompt_cache"}
    )
    _check_keys(
        "providers.openai_compatible",
        provider_compatible,
        {"base_url", "model", "reasoning", "prompt_cache"},
    )
    kimi = _merge(_as_section(raw, "kimi"), provider_kimi)
    # Explicit deployment/test override. Normal .env files contain secrets only;
    # config.toml is canonical for these non-secret values.
    kimi["base_url"] = (
        os.environ.get("KIMI_BASE_URL", "").strip()
        or kimi.get("base_url")
        or "https://api.kimi.ai/coding/v1"
    )
    kimi["model"] = os.environ.get("KIMI_MODEL", "").strip() or kimi.get("model") or "k3.1-pro"
    deepseek = _merge(_as_section(raw, "deepseek"), provider_deepseek)
    if not deepseek.get("base_url"):
        deepseek["base_url"] = os.environ.get("DEEPSEEK_BASE_URL", "").strip()
    if not deepseek.get("model"):
        deepseek["model"] = os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-v4-flash"

    llm = _merge(_as_section(raw, "llm"))
    openai_map = {
        "base_url": "openai_base_url",
        "model": "openai_model",
        "reasoning": "openai_reasoning",
        "prompt_cache": "openai_prompt_cache",
    }
    for source, target in openai_map.items():
        if source in provider_openai:
            llm[target] = provider_openai[source]
    compatible_map = {
        "base_url": "compatible_base_url",
        "model": "compatible_model",
        "reasoning": "compatible_reasoning",
        "prompt_cache": "compatible_prompt_cache",
    }
    for source, target in compatible_map.items():
        if source in provider_compatible:
            llm[target] = provider_compatible[source]
    roles = _as_section(raw, "agent_roles")
    _check_keys(
        "agent_roles",
        roles,
        {
            "default",
            "default_provider",
            "chat",
            "classifier",
            "planner",
            "reviewer",
            "summarizer",
            "experience",
        },
    )
    old_roles = llm.get("roles", {})
    if not isinstance(old_roles, dict):
        raise ConfigError("llm.roles must be a table")
    merged_roles = dict(old_roles)
    for role in ("chat", "classifier", "planner", "reviewer", "summarizer", "experience"):
        if role in roles:
            merged_roles[role] = roles[role]
    if "default_provider" in roles:
        llm["default_provider"] = roles["default_provider"]
    elif "default" in roles:
        llm["default_provider"] = roles["default"]
    if merged_roles:
        llm["roles"] = merged_roles

    routing = _merge(_as_section(raw, "routing"), _as_section(raw, "reasoning"))

    limits = _merge(_as_section(raw, "limits"), _as_section(raw, "task_limits"))
    scheduler = _as_section(raw, "scheduler")
    if "max_workers" in scheduler:
        limits["max_workers"] = scheduler["max_workers"]
    if "max_worker_log_chars" in logging:
        limits["max_worker_log_chars"] = logging["max_worker_log_chars"]

    usage = _merge(_as_section(raw, "usage"))
    for key in ("daily_token_limit", "daily_request_limit", "warn_at_percent"):
        if key in usage:
            limits[key] = usage.pop(key)
    pricing = _merge(_as_section(raw, "pricing"), usage.pop("pricing", {}))
    if pricing:
        usage["prices"] = pricing

    security = _as_section(raw, "security")
    context = _as_section(raw, "context")
    _check_keys("context", context, set(ContextSection.model_fields))
    for key in (
        "max_project_context_chars",
        "max_file_read_bytes",
        "max_diff_chars",
    ):
        if key in security and key not in context:
            context[key] = security.pop(key)
        elif key in security:
            security.pop(key)

    sandbox = _as_section(raw, "sandbox")
    _check_keys("sandbox", sandbox, set(SandboxSection.model_fields))
    network = _as_section(raw, "network")
    _check_keys("network", network, {"sandbox_network", "worker_network", "egress"})
    egress = _merge(_as_section(raw, "egress"), _as_section(network, "egress"))
    if "sandbox_network" in network:
        sandbox["network"] = network["sandbox_network"]
    elif "worker_network" in network:
        sandbox["network"] = network["worker_network"]

    _check_keys("strategies", _as_section(raw, "strategies"), set(StrategiesSection.model_fields))
    _check_keys("backups", _as_section(raw, "backups"), set(BackupsSection.model_fields))
    return {
        "app": app,
        "kimi": kimi,
        "deepseek": deepseek,
        "llm": llm,
        "telegram": _as_section(raw, "telegram"),
        "security": security,
        "context": context,
        "strategies": _as_section(raw, "strategies"),
        "backups": _as_section(raw, "backups"),
        "github": _as_section(raw, "github"),
        "egress": egress,
        "routing": routing,
        "limits": limits,
        "sandbox": sandbox,
        "git": _as_section(raw, "git"),
        "memory": _as_section(raw, "memory"),
        "usage": usage,
    }


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

    raw = normalize_config(raw)

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
        openai_api_key=SecretStr(os.environ.get("OPENAI_API_KEY", "").strip()),
        openai_compatible_api_key=SecretStr(
            os.environ.get("OPENAI_COMPATIBLE_API_KEY", "").strip()
        ),
        deepseek_api_key=SecretStr(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        telegram_bot_token=SecretStr(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        telegram_owner_id=owner_id,
        github_token=SecretStr(
            (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
        ),
    )

    try:
        settings = Settings(
            root=project_root,
            config_path=config_file.resolve(),
            env_path=env_file.resolve(),
            app=AppSection.model_validate(raw.get("app", {})),
            kimi=KimiSection.model_validate(raw.get("kimi", {})),
            llm=LLMSection.model_validate(raw.get("llm", {})),
            telegram=TelegramSection.model_validate(raw.get("telegram", {})),
            security=SecuritySection.model_validate(raw.get("security", {})),
            context=ContextSection.model_validate(raw.get("context", {})),
            strategies=StrategiesSection.model_validate(raw.get("strategies", {})),
            backups=BackupsSection.model_validate(raw.get("backups", {})),
            github=GitHubSection.model_validate(raw.get("github", {})),
            egress=EgressSection.model_validate(raw.get("egress", {})),
            routing=RoutingSection.model_validate(raw.get("routing", {})),
            limits=LimitsSection.model_validate(raw.get("limits", {})),
            deepseek=DeepSeekSection.model_validate(raw.get("deepseek", {})),
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
