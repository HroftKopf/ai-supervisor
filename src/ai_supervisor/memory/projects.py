from __future__ import annotations

import re
import shlex
from pathlib import Path

from ai_supervisor.config import Settings
from ai_supervisor.core.git_utils import git, git_toplevel, is_git_repository
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.memory.database import Database, iso_now, row_to_dict
from ai_supervisor.models import ProjectRecord

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
DANGEROUS_COMMANDS = {"rm", "shutdown", "reboot", "poweroff", "mkfs", "dd"}


class ProjectError(RuntimeError):
    pass


class ProjectService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._allowed_roots = settings.allowed_roots()
        self.redactor = SecretRedactor.from_settings(settings)

    def validate_name(self, name: str) -> str:
        name = name.strip()
        if not NAME_RE.fullmatch(name) or name in {".", ".."}:
            raise ProjectError("имя проекта: 1-64 символа, буквы/цифры/._- и не точка")
        return name

    def resolve_allowed_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise ProjectError("путь проекта должен быть абсолютным")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProjectError(f"путь не существует: {path}") from exc
        if not resolved.is_dir():
            raise ProjectError(f"путь не является директорией: {resolved}")
        if resolved == Path("/") or resolved == Path.home():
            raise ProjectError(
                "нельзя зарегистрировать корень файловой системы или домашнюю директорию"
            )
        allowed = False
        for root in self._allowed_roots:
            try:
                resolved.relative_to(root)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            roots = ", ".join(str(root) for root in self._allowed_roots)
            raise ProjectError(f"проект вне разрешённых директорий ({roots}): {resolved}")
        return resolved

    async def validate_repository(self, path: Path) -> Path:
        if not await is_git_repository(path):
            raise ProjectError(f"не Git-репозиторий: {path}")
        top = await git_toplevel(path)
        if top != path:
            raise ProjectError(f"путь должен быть корнем репозитория (корень: {top})")
        await self._reject_tracked_secrets(path)
        return top

    async def add(
        self,
        name: str,
        raw_path: str,
        *,
        description: str = "",
        stack: str = "",
        instructions: str = "",
        base_branch: str | None = None,
        test_command: str | None = None,
        build_command: str | None = None,
    ) -> ProjectRecord:
        clean_name = self.validate_name(name)
        path = self.resolve_allowed_path(raw_path)
        await self.validate_repository(path)
        branch = base_branch or self.settings.git.base_branch
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", branch):
            raise ProjectError("некорректное имя базовой ветки")
        existing = await self.db.fetchone(
            "SELECT id FROM projects WHERE name = ? COLLATE NOCASE", (clean_name,)
        )
        if existing:
            raise ProjectError(f"проект с именем {clean_name!r} уже существует")
        existing_path = await self.db.fetchone(
            "SELECT id, name FROM projects WHERE path = ?", (str(path),)
        )
        if existing_path:
            raise ProjectError(f"этот путь уже зарегистрирован как {existing_path['name']!r}")
        await self._check_branch(path, branch)
        ensure_safe_command(test_command)
        ensure_safe_command(build_command)
        self._reject_command_secrets(test_command)
        self._reject_command_secrets(build_command)
        now = iso_now()
        cursor = await self.db.execute(
            """
            INSERT INTO projects(name, path, description, stack, instructions, base_branch,
                                 test_command, build_command, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_name,
                str(path),
                self.redactor.redact(description.strip()),
                self.redactor.redact(stack.strip()),
                self.redactor.redact(instructions.strip()),
                branch,
                test_command.strip() if test_command else None,
                build_command.strip() if build_command else None,
                now,
                now,
            ),
        )
        project_id = int(cursor.lastrowid or 0)
        await cursor.close()
        record = await self.get(project_id)
        if record is None:
            raise ProjectError("не удалось прочитать созданный проект")
        return record

    async def create(
        self,
        name: str,
        *,
        description: str = "",
        stack: str = "",
        instructions: str = "",
    ) -> ProjectRecord:
        clean_name = self.validate_name(name)
        root = self._allowed_roots[0]
        path = (root / clean_name).resolve()
        if path.exists():
            raise ProjectError(f"директория уже существует: {path}")
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ProjectError("некорректный путь нового проекта") from exc
        path.mkdir(parents=False, mode=0o755)
        branch = self.settings.git.base_branch
        try:
            await git(path, "init", "-b", branch, timeout_seconds=30)
            readme = path / "README.md"
            readme.write_text(f"# {clean_name}\n\n{description.strip()}\n", encoding="utf-8")
            gitignore = path / ".gitignore"
            gitignore.write_text(".env\n*.log\n__pycache__/\n.venv/\n", encoding="utf-8")
            await git(path, "add", "README.md", ".gitignore", timeout_seconds=30)
            await git(
                path,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"user.name={self.settings.git.author_name}",
                "-c",
                f"user.email={self.settings.git.author_email}",
                "commit",
                "-m",
                "Initial commit",
                "--no-verify",
                timeout_seconds=30,
            )
        except Exception:
            # Do not leave a half-created unregistered project silently.
            raise
        return await self.add(
            clean_name,
            str(path),
            description=description,
            stack=stack,
            instructions=self.redactor.redact(instructions),
        )

    async def get(self, project_id: int) -> ProjectRecord | None:
        row = await self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        data = row_to_dict(row)
        return ProjectRecord.model_validate(data) if data else None

    async def get_by_name(self, name: str) -> ProjectRecord | None:
        row = await self.db.fetchone(
            "SELECT * FROM projects WHERE name = ? COLLATE NOCASE", (name.strip(),)
        )
        data = row_to_dict(row)
        return ProjectRecord.model_validate(data) if data else None

    async def list(self) -> list[ProjectRecord]:
        rows = await self.db.fetchall("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
        return [ProjectRecord.model_validate(row_to_dict(row)) for row in rows]

    async def update_instructions(
        self,
        project_id: int,
        *,
        instructions: str | None = None,
        description: str | None = None,
        stack: str | None = None,
        test_command: str | None = None,
        build_command: str | None = None,
        base_branch: str | None = None,
    ) -> ProjectRecord:
        project = await self.get(project_id)
        if project is None:
            raise ProjectError("проект не найден")
        if test_command is not None:
            ensure_safe_command(test_command)
            self._reject_command_secrets(test_command)
        if build_command is not None:
            ensure_safe_command(build_command)
            self._reject_command_secrets(build_command)
        if base_branch is not None:
            await self._check_branch(Path(project.path), base_branch)
        fields: dict[str, str | None] = {}
        for key, value in {
            "instructions": instructions,
            "description": description,
            "stack": stack,
            "test_command": test_command,
            "build_command": build_command,
            "base_branch": base_branch,
        }.items():
            if value is not None:
                fields[key] = (
                    self.redactor.redact(value.strip())
                    if key in {"instructions", "description", "stack"}
                    else value.strip()
                )
        if not fields:
            return project
        fields["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?",
            (*fields.values(), project_id),
        )
        updated = await self.get(project_id)
        if updated is None:
            raise ProjectError("проект не найден после обновления")
        return updated

    async def remove(self, project_id: int) -> None:
        """Remove only the registry entry; never delete user files or task history."""
        tasks_count = await self.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)
        )
        if int(tasks_count or 0) > 0:
            raise ProjectError(
                "нельзя удалить проект с историей задач; остановите задачи и сохраните аудит"
            )
        await self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def _reject_command_secrets(self, command: str | None) -> None:
        if command and self.redactor.redact(command) != command:
            raise ProjectError(
                "команда проверки содержит секрет; секреты должны быть в окружении, не в репозитории"
            )

    async def _reject_tracked_secrets(self, path: Path) -> None:
        result = await git(path, "ls-files", "-z", timeout_seconds=30, check=False)
        tracked = [item for item in result.stdout.split("\x00") if item]
        bad = [item for item in tracked if _sensitive_name(Path(item))]
        if bad:
            raise ProjectError(
                "в Git отслеживаются потенциально секретные файлы; уберите их из репозитория: "
                + ", ".join(bad[:10])
            )

    async def _check_branch(self, path: Path, branch: str) -> None:
        result = await git(path, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
        if result.returncode != 0:
            raise ProjectError(f"базовая ветка {branch!r} не существует в {path}")

    async def default_project(self) -> ProjectRecord | None:
        projects = await self.list()
        return projects[0] if projects else None


def _sensitive_name(path: Path) -> bool:
    safe_examples = {".env.example", ".env.sample", ".env.template", ".env.test.example"}
    if path.name in {"id_rsa", "id_ed25519", "credentials.json", "secrets.yaml", "secrets.yml"}:
        return True
    if path.name.startswith(".env") and path.name not in safe_examples:
        return True
    return path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}


def ensure_safe_command(command: str | None) -> None:
    """Reject clearly destructive host commands. Execution still happens in bwrap."""
    if not command:
        return
    if "\x00" in command or "\n" in command:
        raise ProjectError("команда проверки не должна содержать NUL или переводы строк")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ProjectError(f"некорректная команда проверки: {exc}") from exc
    if not argv:
        raise ProjectError("пустая команда проверки")
    executable = Path(argv[0]).name
    if executable in DANGEROUS_COMMANDS:
        raise ProjectError(f"команда {executable!r} запрещена как проверка проекта")
    for arg in argv:
        if arg in {"/", "~", "$HOME"}:
            raise ProjectError(
                "команда проверки не должна работать с корнем или домашней директорией"
            )
        if arg.startswith("/etc/") or arg.startswith("/boot/"):
            raise ProjectError("команда проверки не должна изменять системные пути")
