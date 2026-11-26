from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ai_supervisor.core.evidence_manifest import EvidenceManifest
from ai_supervisor.models import (
    Evidence,
    ReviewResult,
    ReviewVerdict,
    TaskRecord,
    TaskSpec,
    TaskStatus,
)


class AcceptanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class AcceptanceDecision:
    allowed: bool
    reason: str = ""
    manifest: EvidenceManifest | None = None

    def require(self) -> EvidenceManifest:
        if not self.allowed or self.manifest is None:
            raise AcceptanceError(self.reason or "acceptance gate rejected the result")
        return self.manifest


class AcceptanceGate:
    """Deterministic acceptance checks; no model output can override them."""

    @staticmethod
    def check(
        *,
        task: TaskRecord,
        spec: TaskSpec,
        evidence: Evidence,
        stored_manifest: EvidenceManifest | None,
        current_manifest: EvidenceManifest | None,
        review: ReviewResult | None,
        expected_manifest_hash: str | None,
        branch_head: str,
        base_head: str,
        require_merge: bool,
        permissions_ok: bool = True,
    ) -> AcceptanceDecision:
        if task.status is not TaskStatus.WAITING_APPROVAL:
            return AcceptanceDecision(False, f"task status is {task.status.value}")
        if not permissions_ok:
            return AcceptanceDecision(False, "permission check failed")
        if task.spec is None or task.spec.request_revision != task.request_revision:
            return AcceptanceDecision(False, "TaskSpec does not match active revision")
        if spec.request_revision != task.request_revision:
            return AcceptanceDecision(False, "review spec revision is stale")
        if stored_manifest is None or current_manifest is None:
            return AcceptanceDecision(False, "evidence manifest is missing")
        if evidence is None:
            return AcceptanceDecision(False, "evidence is missing")
        evidence_payload = json.loads(evidence.model_dump_json())
        evidence_hash = hashlib.sha256(
            json.dumps(evidence_payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if evidence_hash != stored_manifest.evidence_hash:
            return AcceptanceDecision(False, "evidence does not match manifest")
        if stored_manifest.format_version < 2:
            return AcceptanceDecision(False, "evidence manifest format is too old")
        if stored_manifest.with_hash().manifest_hash != stored_manifest.manifest_hash:
            return AcceptanceDecision(False, "evidence manifest hash is corrupted")
        if stored_manifest.unsafe_paths:
            return AcceptanceDecision(False, "evidence contains unverifiable/sensitive paths")
        if stored_manifest.task_id != task.id or stored_manifest.project_id != task.project_id:
            return AcceptanceDecision(False, "manifest belongs to another task/project")
        if stored_manifest.request_revision != task.request_revision:
            return AcceptanceDecision(False, "manifest belongs to another revision")
        if stored_manifest.manifest_hash != current_manifest.manifest_hash:
            return AcceptanceDecision(False, "worktree/evidence changed since review")
        if stored_manifest.head_sha != current_manifest.head_sha:
            return AcceptanceDecision(False, "HEAD changed since review")
        if not task.head_commit or task.head_commit != stored_manifest.head_sha:
            return AcceptanceDecision(False, "task head_commit does not match evidence")
        if not branch_head or branch_head != stored_manifest.head_sha:
            return AcceptanceDecision(False, "task branch no longer points at reviewed commit")
        if require_merge:
            if not task.base_commit or base_head != task.base_commit:
                return AcceptanceDecision(False, "base branch changed since review")
        if review is None or review.verdict is not ReviewVerdict.APPROVED:
            return AcceptanceDecision(False, "review verdict is not APPROVED")
        if review.task_id != task.id:
            return AcceptanceDecision(False, "review belongs to another task")
        if review.request_revision != task.request_revision:
            return AcceptanceDecision(False, "review belongs to another revision")
        if review.spec_fingerprint != stored_manifest.spec_hash:
            return AcceptanceDecision(False, "review spec fingerprint does not match")
        if review.manifest_fingerprint != stored_manifest.manifest_hash:
            return AcceptanceDecision(False, "review manifest fingerprint does not match")
        if review.reviewed_commit != stored_manifest.head_sha:
            return AcceptanceDecision(False, "review commit does not match evidence")
        if expected_manifest_hash and expected_manifest_hash != stored_manifest.manifest_hash:
            return AcceptanceDecision(False, "acceptance callback belongs to another result")
        if stored_manifest.spec_hash != current_manifest.spec_hash:
            return AcceptanceDecision(False, "TaskSpec changed since evidence")
        if stored_manifest.validation_hash != current_manifest.validation_hash:
            return AcceptanceDecision(False, "validation configuration changed")
        return AcceptanceDecision(True, manifest=stored_manifest)
