from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptProfile:
    name: str
    version: str
    text: str
    sha256: str


class PromptRegistry:
    """Role-specific prompt files with an explicit manifest version."""

    def __init__(self, root: Path) -> None:
        self.root = root
        manifest_path = root / "manifest.json"
        try:
            self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptError(f"prompt manifest is missing/invalid: {manifest_path}") from exc
        self.version = str(self.manifest.get("version") or "unknown")
        self._profiles: dict[str, PromptProfile] = {}
        for name, item in (self.manifest.get("profiles") or {}).items():
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file") or f"{name}.md")
            path = root / file_name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PromptError(f"prompt profile is missing: {path}") from exc
            self._profiles[str(name)] = PromptProfile(
                name=str(name),
                version=self.version,
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            )

    def compose(self, name: str) -> str:
        common = self._profiles.get("common")
        profile = self.get(name)
        return ((common.text + "\n\n") if common is not None else "") + profile.text

    def get(self, name: str) -> PromptProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise PromptError(f"unknown prompt profile: {name}") from exc

    def metadata(self) -> dict[str, str]:
        values = {"version": self.version}
        values.update({name: profile.sha256 for name, profile in self._profiles.items()})
        return values
