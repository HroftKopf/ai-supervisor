from __future__ import annotations

import asyncio
from pathlib import Path

from ai_supervisor.config import Settings
from ai_supervisor.core.git_utils import git
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import ProjectRecord

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".next",
    ".cache",
    "var",
}
SKIP_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
}
SKIP_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db", ".db-wal", ".db-shm"}
IMPORTANT_NAMES = {
    "README.md",
    "README",
    "AGENTS.md",
    "CLAUDE.md",
    "pyproject.toml",
    "Cargo.toml",
    "package.json",
    "go.mod",
    "Makefile",
    "justfile",
    "requirements.txt",
    "config.toml",
    "docker-compose.yml",
    "compose.yml",
}


class ContextError(RuntimeError):
    pass


class ProjectContextBuilder:
    def __init__(self, settings: Settings, redactor: SecretRedactor) -> None:
        self.settings = settings
        self.redactor = redactor

    async def build(
        self,
        project: ProjectRecord,
        *,
        include_files: bool = True,
        goal: str = "",
        role: str = "planner",
    ) -> str:
        root = Path(project.path).resolve()
        if not root.is_dir():
            raise ContextError(f"каталог проекта недоступен: {root}")
        parts = [
            f"Имя: {project.name}",
            f"Путь: {root}",
            f"Описание: {project.description or '(нет)'}",
            f"Стек: {project.stack or '(не указан)'}",
            f"Базовая ветка: {project.base_branch}",
            f"Инструкции проекта:\n{project.instructions or '(нет)'}",
        ]
        branch_result = await git(
            root, "symbolic-ref", "--short", "HEAD", timeout_seconds=15, check=False
        )
        head_result = await git(
            root, "rev-parse", "--short", "HEAD", timeout_seconds=15, check=False
        )
        status_result = await git(root, "status", "--porcelain=v1", timeout_seconds=15, check=False)
        log_result = await git(root, "log", "--oneline", "-8", timeout_seconds=15, check=False)
        branch = branch_result.stdout.strip()
        head = head_result.stdout.strip()
        status = status_result.stdout.strip()
        log = log_result.stdout.strip()
        parts.extend(
            [
                f"Текущая ветка: {branch or '(detached)'}",
                f"HEAD: {head or '(нет коммитов)'}",
                f"Незакоммиченные изменения: {status or '(чисто)'}",
                f"Последние коммиты:\n{log or '(нет)'}",
            ]
        )
        files_result = await git(root, "ls-files", "-z", timeout_seconds=30, check=False)
        files = [item for item in files_result.stdout.split("\x00") if item]
        if not files:
            files = await _walk_files(root, limit=300)
        visible = [item for item in files if not _skip_path(Path(item))]
        parts.append(
            "Файлы (ограниченный список):\n" + "\n".join(f"- {item}" for item in visible[:250])
        )
        if include_files:
            selected = _select_important(visible, goal=goal)
            budget = max(
                2000, self.settings.security.max_project_context_chars - len("\n".join(parts))
            )
            for path in selected:
                if budget <= 0:
                    break
                content = await self._safe_read(root, root / path)
                if content is None:
                    continue
                content = content[: min(budget, 12000)]
                content = self.redactor.redact(content)
                parts.append(f"\n--- file: {path} ---\n{content}\n--- end: {path} ---")
                budget -= len(content)
        text = "\n".join(parts)
        return text[: self.settings.security.max_project_context_chars]

    async def _safe_read(self, project_root: Path, path: Path) -> str | None:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        # Containment is checked against the project root first, then the allowed root.
        try:
            resolved.relative_to(project_root)
        except ValueError:
            return None
        allowed = False
        for root_path in self.settings.allowed_roots():
            try:
                resolved.relative_to(root_path)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            return None
        try:
            stat = resolved.stat()
        except OSError:
            return None
        if not resolved.is_file() or stat.st_size > self.settings.security.max_file_read_bytes:
            return None
        try:
            data = await asyncio.to_thread(resolved.read_bytes)
        except OSError:
            return None
        if b"\x00" in data[:4096]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _skip_path(path: Path) -> bool:
    if path.name in SKIP_NAMES or path.name.startswith(".env"):
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def _select_important(files: list[str], *, goal: str = "") -> list[str]:
    selected: list[str] = []
    terms = [term for term in goal.lower().replace("/", " ").split() if len(term) >= 3][:8]
    for term in terms:
        for file in files:
            if term in file.lower() and file not in selected:
                selected.append(file)
                break
    if len(selected) < 6:
        for name in IMPORTANT_NAMES:
            for file in files:
                if Path(file).name == name and file not in selected:
                    selected.append(file)
                    break
    if len(selected) < 6:
        for file in files:
            if file not in selected and not file.startswith("."):
                selected.append(file)
                if len(selected) >= 6:
                    break
    return selected[:8]


async def _walk_files(root: Path, limit: int = 300) -> list[str]:
    result: list[str] = []

    def walk() -> None:
        for path in sorted(root.rglob("*")):
            if len(result) >= limit:
                return
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if _skip_path(rel):
                continue
            if path.is_file() and not path.is_symlink():
                result.append(rel.as_posix())

    await asyncio.to_thread(walk)
    return result
