from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_supervisor.config import Settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import ProjectRecord

MAX_FILES = 200
MAX_BYTES = 10 * 1024 * 1024
FORBIDDEN_PARTS = {".git", ".env", ".ssh", ".gnupg", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")


class ConfigImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportedFile:
    relative_path: str
    size: int


@dataclass(frozen=True)
class ImportPlan:
    project_id: int
    source: Path
    destination: Path
    overwrite: bool
    source_hash: str
    files: tuple[ImportedFile, ...]
    total_bytes: int
    already_applied: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class ImportResult:
    destination: str
    files: int
    total_bytes: int
    backup: str | None


class ConfigImportService:
    """Copy an owner-approved config into a project without exposing arbitrary host paths."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redactor = SecretRedactor.from_settings(settings)

    async def plan(
        self,
        project: ProjectRecord,
        source_raw: str,
        destination_raw: str,
        *,
        overwrite: bool = False,
        target_root: Path | None = None,
        allow_existing_identical: bool = False,
    ) -> ImportPlan:
        return await asyncio.to_thread(
            self._plan_sync,
            project,
            source_raw,
            destination_raw,
            overwrite,
            target_root,
            allow_existing_identical,
        )

    async def discover(self, project: ProjectRecord, query: str) -> Path:
        return await asyncio.to_thread(self._discover_sync, project, query)

    async def execute(self, plan: ImportPlan) -> ImportResult:
        return await asyncio.to_thread(self._execute_sync, plan)

    # ------------------------------------------------------------------
    # synchronous implementation (one short blocking filesystem pass)
    # ------------------------------------------------------------------

    def _plan_sync(
        self,
        project: ProjectRecord,
        source_raw: str,
        destination_raw: str,
        overwrite: bool,
        target_root: Path | None,
        allow_existing_identical: bool,
    ) -> ImportPlan:
        project_root = (target_root or Path(project.path)).resolve(strict=True)
        source = self._resolve_source(project, source_raw)
        destination = self._resolve_destination(project_root, destination_raw)
        files = self._scan_source(source)
        total = sum(item.size for item in files)
        if total > MAX_BYTES:
            raise ConfigImportError(f"import is too large: {total} bytes > {MAX_BYTES}")
        digest = self._hash_files(source, files)
        already_applied = False
        if destination.exists():
            if source.is_dir() != destination.is_dir():
                raise ConfigImportError("source and destination types differ")
            if (
                not overwrite
                and allow_existing_identical
                and self._matches(source, destination, files)
            ):
                already_applied = True
            elif not overwrite:
                raise ConfigImportError(
                    f"destination already exists: {destination}; use --overwrite after reviewing it"
                )
        return ImportPlan(
            project_id=project.id,
            source=source,
            destination=destination,
            overwrite=overwrite,
            source_hash=digest,
            files=tuple(files),
            total_bytes=total,
            already_applied=already_applied,
        )

    def _discover_sync(self, project: ProjectRecord, raw_query: str) -> Path:
        stopwords = {
            "import",
            "imports",
            "config",
            "configuration",
            "file",
            "files",
            "copy",
            "github",
            "project",
            "the",
            "and",
            "to",
            "my",
            "импорт",
            "импортировать",
            "конфигурация",
            "конфигурацию",
            "файл",
            "файлы",
            "скопировать",
            "залей",
            "мою",
            "мой",
            "проект",
            "на",
            "в",
            "и",
        }
        terms = [
            term
            for term in re.findall(r"[a-zа-я0-9._-]{3,}", raw_query.lower())
            if term not in stopwords
        ]
        if not terms:
            raise ConfigImportError("не указан источник конфигурации и нет запроса для поиска")
        roots = self._allowed_roots(project)
        if not roots:
            raise ConfigImportError(
                "project has no extra_read_paths; configure /project extra-read first"
            )
        candidates: list[Path] = []
        for root in roots:
            if root.is_file():
                if any(term in root.name.lower() for term in terms):
                    candidates.append(root)
                continue
            if any(term in root.name.lower() for term in terms) and not (root / ".git").exists():
                candidates.append(root)
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                depth = len(Path(dirpath).relative_to(root).parts)
                if depth >= 5:
                    dirnames[:] = []
                    continue
                for name in list(dirnames):
                    if name.startswith("."):
                        continue
                    path = Path(dirpath) / name
                    if (
                        any(term in name.lower() for term in terms)
                        and path.is_dir()
                        and not path.is_symlink()
                        and not (path / ".git").exists()
                    ):
                        candidates.append(path)
                for name in filenames:
                    if name.startswith("."):
                        continue
                    path = Path(dirpath) / name
                    if any(term in name.lower() for term in terms) and path.is_file():
                        candidates.append(path)
                dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        unique = sorted({path.resolve() for path in candidates})
        if not unique:
            raise ConfigImportError(
                f"конфигурация по запросу {raw_query!r} не найдена в extra_read_paths"
            )
        # Prefer a directory over its matched children.
        unique = [
            path
            for path in unique
            if not any(parent != path and path.is_relative_to(parent) for parent in unique)
        ]
        if len(unique) != 1:
            preview = ", ".join(str(path) for path in unique[:8])
            raise ConfigImportError(f"найдено несколько кандидатов: {preview}")
        return unique[0]

    def _resolve_source(self, project: ProjectRecord, source_raw: str) -> Path:
        raw = (source_raw or "").strip()
        if not raw:
            raise ConfigImportError("source path is required")
        try:
            source = Path(raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ConfigImportError(f"source does not exist: {raw}") from exc
        roots = self._allowed_roots(project)
        if not roots:
            raise ConfigImportError(
                "project has no extra_read_paths; configure /project extra-read first"
            )
        allowed = False
        for root in roots:
            if root.is_file():
                allowed = allowed or source == root
            else:
                allowed = allowed or source == root or source.is_relative_to(root)
        if not allowed:
            raise ConfigImportError("source is outside the project's explicit extra_read_paths")
        if source.is_symlink():
            raise ConfigImportError("source itself must not be a symlink")
        return source

    @staticmethod
    def _resolve_destination(project_root: Path, raw: str) -> Path:
        value = (raw or "").strip()
        if not value or value in {".", "./"}:
            raise ConfigImportError("destination must be a relative path inside the project")
        candidate = Path(value)
        if candidate.is_absolute():
            raise ConfigImportError("destination must be relative to the project root")
        if any(part in {"..", ""} for part in candidate.parts):
            raise ConfigImportError("destination must not contain '..'")
        destination = (project_root / candidate).resolve()
        if destination == project_root or not destination.is_relative_to(project_root):
            raise ConfigImportError("destination escapes the project root")
        for part in destination.relative_to(project_root).parts:
            if part in FORBIDDEN_PARTS or part.startswith(".env"):
                raise ConfigImportError(f"destination contains forbidden path component: {part}")
        if destination.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ConfigImportError("destination looks like a secret/key file")
        return destination

    @staticmethod
    def _allowed_roots(project: ProjectRecord) -> list[Path]:
        try:
            raw = json.loads(project.extra_read_paths_json or "[]")
        except json.JSONDecodeError:
            return []
        roots: list[Path] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                roots.append(Path(str(item)).expanduser().resolve(strict=True))
            except OSError:
                continue
        return roots

    def _scan_source(self, source: Path) -> list[ImportedFile]:
        if source.is_symlink():
            raise ConfigImportError("symlink sources are not allowed")
        if source.is_file():
            return [self._inspect_file(source, source.name)]
        if not source.is_dir():
            raise ConfigImportError("source must be a regular file or directory")
        files: list[ImportedFile] = []
        for root, dirnames, filenames in os.walk(source, followlinks=False):
            dirnames[:] = sorted(dirnames)
            for dirname in list(dirnames):
                if (Path(root) / dirname).is_symlink():
                    raise ConfigImportError(f"symlink directory is not allowed: {dirname}")
            for name in sorted(filenames):
                path = Path(root) / name
                if path.is_symlink():
                    raise ConfigImportError(f"symlink file is not allowed: {path.name}")
                rel = str(path.relative_to(source))
                files.append(self._inspect_file(path, rel))
                if len(files) > MAX_FILES:
                    raise ConfigImportError(f"import has more than {MAX_FILES} files")
        if not files:
            raise ConfigImportError("source directory is empty")
        return files

    def _inspect_file(self, path: Path, relative_name: str) -> ImportedFile:
        if not path.is_file() or path.is_symlink():
            raise ConfigImportError(f"not a regular file: {relative_name}")
        parts = Path(relative_name).parts
        for part in parts:
            if part in FORBIDDEN_PARTS or part.startswith(".env"):
                raise ConfigImportError(f"refusing secret-like file: {relative_name}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ConfigImportError(f"refusing key-like file: {relative_name}")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ConfigImportError(f"file is too large: {relative_name}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ConfigImportError(f"cannot read source file: {relative_name}") from exc
        if b"\x00" not in data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and self.redactor.redact(text) != text:
                raise ConfigImportError(
                    f"potential secret detected in {relative_name}; sanitize it before import"
                )
        return ImportedFile(relative_path=relative_name, size=size)

    @staticmethod
    def _hash_files(source: Path, files: list[ImportedFile]) -> str:
        digest = hashlib.sha256()
        for item in files:
            path = source / item.relative_path if source.is_dir() else source
            digest.update(item.relative_path.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _matches(source: Path, destination: Path, files: list[ImportedFile]) -> bool:
        try:
            if source.is_file() != destination.is_file():
                return False
            if source.is_file():
                return source.read_bytes() == destination.read_bytes()
            for item in files:
                target = destination / item.relative_path
                if not target.is_file() or target.is_symlink():
                    return False
                if (source / item.relative_path).read_bytes() != target.read_bytes():
                    return False
            return True
        except OSError:
            return False

    def _execute_sync(self, plan: ImportPlan) -> ImportResult:
        source = plan.source
        destination = plan.destination
        if not source.exists():
            raise ConfigImportError("source disappeared after approval")
        current_files = self._scan_source(source)
        if self._hash_files(source, current_files) != plan.source_hash:
            raise ConfigImportError("source changed after approval; request a new import")
        if plan.already_applied:
            if not self._matches(source, destination, current_files):
                raise ConfigImportError("destination changed after approval; request a new import")
            return ImportResult(
                destination=str(destination),
                files=plan.file_count,
                total_bytes=plan.total_bytes,
                backup=None,
            )
        if destination.exists() and not plan.overwrite:
            raise ConfigImportError("destination appeared after approval; refusing to overwrite")
        if destination.exists() and source.is_dir() != destination.is_dir():
            raise ConfigImportError("source/destination types changed")

        backup_path: Path | None = None
        if destination.exists():
            backup_root = self.settings.data_dir / "backups" / "config-import"
            backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_root / f"project-{plan.project_id}-{stamp}-{uuid.uuid4().hex[:8]}"
            shutil.move(str(destination), str(backup_path))

        temp = destination.parent / f".ai-supervisor-import-{uuid.uuid4().hex}"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                self._copy_tree(source, temp)
            else:
                temp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, temp)
                temp.chmod(0o644)
            os.replace(temp, destination)
        except Exception as exc:
            if temp.exists():
                if temp.is_dir():
                    shutil.rmtree(temp, ignore_errors=True)
                else:
                    temp.unlink(missing_ok=True)
            if backup_path is not None and backup_path.exists() and not destination.exists():
                shutil.move(str(backup_path), str(destination))
            raise ConfigImportError(f"config import failed: {exc}") from exc
        return ImportResult(
            destination=str(destination),
            files=plan.file_count,
            total_bytes=plan.total_bytes,
            backup=str(backup_path) if backup_path else None,
        )

    @staticmethod
    def _copy_tree(source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, mode=0o755)
        for root, dirnames, filenames in os.walk(source, followlinks=False):
            root_path = Path(root)
            rel = root_path.relative_to(source)
            target_root = destination / rel
            target_root.mkdir(parents=True, exist_ok=True)
            for dirname in sorted(dirnames):
                source_dir = root_path / dirname
                if source_dir.is_symlink():
                    raise ConfigImportError(f"symlink directory is not allowed: {dirname}")
                (target_root / dirname).mkdir(exist_ok=True, mode=0o755)
            for name in sorted(filenames):
                source_file = root_path / name
                if source_file.is_symlink():
                    raise ConfigImportError(f"symlink file is not allowed: {name}")
                target_file = target_root / name
                shutil.copyfile(source_file, target_file)
                target_file.chmod(stat.S_IMODE(source_file.stat().st_mode) & 0o755 or 0o644)
