from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
    ) -> ImportPlan:
        return await asyncio.to_thread(
            self._plan_sync, project, source_raw, destination_raw, overwrite
        )

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
    ) -> ImportPlan:
        project_root = Path(project.path).resolve(strict=True)
        source = self._resolve_source(project, source_raw)
        destination = self._resolve_destination(project_root, destination_raw)
        if destination.exists() and not overwrite:
            raise ConfigImportError(
                f"destination already exists: {destination}; use --overwrite after reviewing it"
            )
        if destination.exists():
            if source.is_dir() != destination.is_dir():
                raise ConfigImportError("source and destination types differ")
        files = self._scan_source(source)
        total = sum(item.size for item in files)
        if total > MAX_BYTES:
            raise ConfigImportError(f"import is too large: {total} bytes > {MAX_BYTES}")
        digest = self._hash_files(source, files)
        return ImportPlan(
            project_id=project.id,
            source=source,
            destination=destination,
            overwrite=overwrite,
            source_hash=digest,
            files=tuple(files),
            total_bytes=total,
        )

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

    def _execute_sync(self, plan: ImportPlan) -> ImportResult:
        source = plan.source
        destination = plan.destination
        if not source.exists():
            raise ConfigImportError("source disappeared after approval")
        current_files = self._scan_source(source)
        if self._hash_files(source, current_files) != plan.source_hash:
            raise ConfigImportError("source changed after approval; request a new import")
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
