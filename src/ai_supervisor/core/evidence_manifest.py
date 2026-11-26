from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from ai_supervisor.core.git_utils import git_output
from ai_supervisor.models import Evidence, ProjectRecord, TaskRecord, TaskSpec


class EvidenceManifest(BaseModel):
    project_id: int
    task_id: int
    worktree_path: str
    head_sha: str
    index_hash: str
    tracked_change_hash: str
    untracked_hash: str
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


async def build_manifest(
    *,
    project: ProjectRecord,
    task: TaskRecord,
    worktree: Path,
    evidence: Evidence,
    spec: TaskSpec,
) -> EvidenceManifest:
    root = Path(worktree).resolve()
    head = (await git_output(root, "rev-parse", "HEAD")).strip()
    index = (await git_output(root, "diff", "--cached", "--binary")).encode()
    tracked = (await git_output(root, "diff", "HEAD", "--binary")).encode()
    status = await git_output(root, "status", "--porcelain=v1", "-z")
    # Avoid reading file contents (they may contain secrets); bind to Git's
    # porcelain status plus file size metadata for untracked files.
    status_entries = status.replace("\x00", "\n").splitlines()
    untracked_meta: list[str] = []
    for entry in status_entries:
        if entry.startswith("?? "):
            name = entry[3:]
            path = root / name
            try:
                stat = path.stat()
                untracked_meta.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
            except OSError:
                untracked_meta.append(name)
    evidence_payload = json.loads(evidence.model_dump_json())
    manifest = EvidenceManifest(
        project_id=project.id,
        task_id=task.id,
        worktree_path=str(root),
        head_sha=head,
        index_hash=hashlib.sha256(index).hexdigest(),
        tracked_change_hash=hashlib.sha256(tracked).hexdigest(),
        untracked_hash=hashlib.sha256("\n".join(untracked_meta).encode()).hexdigest(),
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
