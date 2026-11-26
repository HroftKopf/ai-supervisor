from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_supervisor.models import TaskSpec, TaskType

SCENARIOS = Path(__file__).resolve().parents[3] / "evaluation" / "scenarios.json"


@dataclass(slots=True)
class ScenarioResult:
    scenario_id: str
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values()) and not self.errors


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or SCENARIOS
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("scenario file must contain a list")
    return data


def evaluate_spec(
    scenario: dict[str, Any], spec: TaskSpec, *, level: int | None = None
) -> ScenarioResult:
    result = ScenarioResult(str(scenario.get("id") or "unknown"))
    expected_type = scenario.get("expected_task_type")
    if expected_type:
        result.checks["task_type"] = spec.task_type.value == expected_type
        if not result.checks["task_type"]:
            result.errors.append(f"expected {expected_type}, got {spec.task_type.value}")
    if "requires_file_changes" in scenario:
        result.checks["requires_file_changes"] = bool(spec.requires_file_changes) is bool(
            scenario["requires_file_changes"]
        )
    max_stages = scenario.get("max_stages")
    if max_stages is not None:
        result.checks["max_stages"] = len(spec.stages) <= int(max_stages)
    required = set(scenario.get("required_operations") or [])
    if required:
        actual = {stage.operation.value for stage in spec.stages}
        result.checks["required_operations"] = required <= actual
    forbidden = set(scenario.get("forbidden_tools") or [])
    if forbidden:
        result.checks["forbidden_tools"] = not (forbidden & set(spec.required_tools))
    if "must_mention" in scenario:
        needle = str(scenario["must_mention"]).lower()
        haystack = json.dumps(spec.model_dump(), ensure_ascii=False).lower()
        result.checks["must_mention"] = needle in haystack
    if "must_not_contain" in scenario:
        needle = str(scenario["must_not_contain"]).lower()
        haystack = json.dumps(spec.model_dump(), ensure_ascii=False).lower()
        result.checks["must_not_contain"] = needle not in haystack
    if "min_criteria" in scenario:
        result.checks["min_criteria"] = len(spec.acceptance_criteria) >= int(
            scenario["min_criteria"]
        )
    if level is not None and "min_level" in scenario:
        result.checks["min_level"] = level >= int(scenario["min_level"])
    if spec.task_type is TaskType.COMPOSITE:
        ids = [stage.id for stage in spec.stages]
        result.checks["unique_stage_ids"] = len(ids) == len(set(ids))
        known = set(ids)
        result.checks["known_dependencies"] = all(
            dep in known for stage in spec.stages for dep in stage.depends_on
        )
    return result
