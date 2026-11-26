from __future__ import annotations

from dataclasses import dataclass

from ai_supervisor.models import RiskLevel, StageOperation


@dataclass(frozen=True)
class Capability:
    name: str
    executor: str
    arguments: str
    permissions: RiskLevel
    approval: bool
    constraints: str


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "import_config",
        "python",
        "source?, destination, overwrite?",
        RiskLevel.HIGH,
        True,
        "только extra_read_paths; secret-scan; без symlink",
    ),
    Capability(
        "git_commit",
        "git",
        "commit_message?",
        RiskLevel.LOW,
        False,
        "только подготовленные изменения в worktree",
    ),
    Capability(
        "validate",
        "validator",
        "allowed_paths?, require_change?",
        RiskLevel.LOW,
        False,
        "diff, checks, allowed_paths, secret-scan",
    ),
    Capability(
        "github_ensure_repo",
        "github",
        "repo_name, repo_owner?, private?, remote?",
        RiskLevel.HIGH,
        True,
        "GitHub API; существующий repo не пересоздаётся",
    ),
    Capability(
        "publish",
        "git",
        "remote?, branch?, merge?",
        RiskLevel.HIGH,
        True,
        "ff-only merge; push без force; remote SHA check",
    ),
    Capability(
        "kimi_analyze", "kimi", "instruction", RiskLevel.MEDIUM, False, "без изменения файлов"
    ),
    Capability(
        "deepseek_change",
        "deepseek",
        "instruction, allowed_paths?",
        RiskLevel.MEDIUM,
        False,
        "только bwrap worktree; commit/push запрещены",
    ),
)


def capability_block() -> str:
    lines = ["## Фактические возможности оркестратора", ""]
    for item in CAPABILITIES:
        approval = "требуется approval" if item.approval else "без отдельного approval"
        lines.append(
            f"- {item.name}: executor={item.executor}; args={item.arguments}; "
            f"permissions={item.permissions.value}; {approval}; {item.constraints}"
        )
    lines += [
        "- Не предлагай операции вне этого списка как доступные.",
        "- Для SYSTEM_OPERATION/EXTERNAL_OPERATION без поддерживаемого этапа верни блокирующее ограничение.",
    ]
    return "\n".join(lines)


def unsupported_operations() -> set[str]:
    return {operation.value for operation in StageOperation} - {item.name for item in CAPABILITIES}
