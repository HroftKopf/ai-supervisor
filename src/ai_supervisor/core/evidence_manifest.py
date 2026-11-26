from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from ai_supervisor.core.git_utils import git_output
from ai_supervisor.models import Evidence, ProjectRecord, TaskRecord, TaskSpec

FORMAT_VERSION = 2
_SENSITIVE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class EvidenceError(RuntimeError):
    pass


class EvidenceUnsafe(EvidenceError):
    pass


class EvidenceManifest(BaseModel):
    format_version: int = 1
    project_id: int
    task_id: int
    worktree_path: str
    base_ref: str = ""
    base_commit: str = ""
    head_sha: str
    index_hash: str
    tracked_change_hash: str
    untracked_hash: str
    content_tree_hash: str = ""
    unsafe_paths: list[str] = Field(default_factory=list)
    validation_hash: str = ""
    evidence_hash: str
    request_revision: int
    spec_hash: str
    manifest_hash: str = ""

    def with_hash(self) -> EvidenceManifest:
        payload = self.model_dump(exclude={"manifest_hash"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return self.model_copy(update={"manifest_hash": digest})


def _sensitive(name: str) -> bool:
    lower = name.lower()
    if lower in _SENSITIVE_NAMES or lower.startswith(".env"):
        return True
    return Path(lower).suffix in _SENSITIVE_SUFFIXES


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_fingerprint(root: Path) -> tuple[str, list[str]]:
    """Hash worktree content without following symlinks or reading secrets."""
    root = root.resolve()
    digest = hashlib.sha256()
    unsafe: list[str] = []
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(base)
        rel_base = current.relative_to(root)
        if ".git" in rel_base.parts:
            dirs[:] = []
            continue
        ignored_dirs = {
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".venv",
            "node_modules",
        }
        dirs[:] = sorted(name for name in dirs if name not in ignored_dirs)
        for name in sorted(files):
            path = current / name
            rel = str(path.relative_to(root))
            try:
                stat = path.lstat()
            except OSError:
                unsafe.append(rel)
                continue
            if path.is_symlink():
                # Never read through a symlink; the link target is part of the
                # identity, and acceptance must explicitly decide about it.
                unsafe.append(rel)
                target = os.readlink(path)
                digest.update(f"{rel}\x00symlink\x00{target}\x00".encode())
                continue
            if _sensitive(name):
                unsafe.append(rel)
                digest.update(f"{rel}\x00sensitive\x00".encode())
                continue
            try:
                digest.update(f"{rel}\x00{stat.st_size}\x00{_hash_file(path)}\x00".encode())
            except OSError:
                unsafe.append(rel)
    return digest.hexdigest(), sorted(set(unsafe))


def _validation_hash(project: ProjectRecord, spec: TaskSpec, evidence: Evidence) -> str:
    payload = {
        "test_command": project.test_command or "",
        "build_command": project.build_command or "",
        "acceptance_criteria": spec.acceptance_criteria,
        "checks": [
            {"name": check.name, "command": check.command, "exit_code": check.exit_code}
            for check in evidence.checks
        ],
        "requires_file_changes": spec.requires_file_changes,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def build_manifest(
    *,
    project: ProjectRecord,
    task: TaskRecord,
    worktree: Path,
    evidence: Evidence,
    spec: TaskSpec,
    validation_content_hash: str = "",
) -> EvidenceManifest:
    root = Path(worktree).resolve()
    top = (await git_output(root, "rev-parse", "--show-toplevel")).strip()
    if Path(top).resolve() != root:
        raise EvidenceError("worktree path is not the canonical git top-level")
    head = (await git_output(root, "rev-parse", "HEAD")).strip()
    index = (await git_output(root, "diff", "--cached", "--binary")).encode()
    tracked = (await git_output(root, "diff", "HEAD", "--binary")).encode()
    untracked = await git_output(root, "ls-files", "--others", "--exclude-standard", "-z")
    entries: list[str] = []
    unsafe: list[str] = []
    for raw in untracked.split("\x00"):
        if not raw:
            continue
        rel = raw
        path = root / rel
        try:
            stat = path.lstat()
        except OSError:
            unsafe.append(rel)
            continue
        if path.is_symlink():
            unsafe.append(rel)
            entries.append(f"{rel}\x00symlink\x00{os.readlink(path)}")
            continue
        if _sensitive(path.name):
            unsafe.append(rel)
            entries.append(f"{rel}\x00sensitive")
            continue
        try:
            entries.append(f"{rel}\x00{stat.st_size}\x00{_hash_file(path)}")
        except OSError:
            unsafe.append(rel)
    untracked_hash = hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()
    content_hash, content_unsafe = content_fingerprint(root)
    if validation_content_hash and validation_content_hash != content_hash:
        raise EvidenceError("worktree content changed during validation")
    evidence_payload = json.loads(evidence.model_dump_json())
    manifest = EvidenceManifest(
        format_version=FORMAT_VERSION,
        project_id=project.id,
        task_id=task.id,
        worktree_path=str(root),
        base_ref=task.base_ref or project.base_branch,
        base_commit=task.base_commit or "",
        head_sha=head,
        index_hash=hashlib.sha256(index).hexdigest(),
        tracked_change_hash=hashlib.sha256(tracked).hexdigest(),
        untracked_hash=untracked_hash,
        content_tree_hash=content_hash,
        unsafe_paths=sorted(set(unsafe + content_unsafe)),
        validation_hash=_validation_hash(project, spec, evidence),
        evidence_hash=hashlib.sha256(
            json.dumps(evidence_payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        request_revision=task.request_revision,
        spec_hash=hashlib.sha256(
            json.dumps(spec.model_dump(), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    )
    return manifest.with_hash()


def parse_manifest(raw: str | None) -> EvidenceManifest | None:
    if not raw:
        return None
    try:
        return EvidenceManifest.model_validate_json(raw)
    except Exception:
        return None
