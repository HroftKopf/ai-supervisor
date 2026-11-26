from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Strategy:
    id: str
    text: str


class StrategyLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.version = str(manifest.get("version") or "unknown")
        self._items: dict[str, tuple[list[str], Strategy]] = {}
        for item in manifest.get("strategies", []):
            name = str(item["id"])
            path = root / str(item["file"])
            self._items[name] = (
                [str(marker).lower() for marker in item.get("markers", [])],
                Strategy(name, path.read_text(encoding="utf-8")),
            )

    def select(self, request: str, *, limit: int = 2) -> str:
        text = request.lower()
        scored: list[tuple[int, str]] = []
        for name, (markers, _strategy) in self._items.items():
            score = sum(1 for marker in markers if marker in text)
            if score:
                scored.append((score, name))
        scored.sort(reverse=True)
        chosen = [self._items[name][1] for _score, name in scored[:limit]]
        if not chosen:
            return ""
        return "## Подходящие стратегии (только релевантные)\n\n" + "\n\n".join(
            item.text for item in chosen
        )
