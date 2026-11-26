from __future__ import annotations

from ai_supervisor.agents.prompts import PromptRegistry
from ai_supervisor.agents.supervisor import PROMPTS_DIR
from ai_supervisor.core.routing import RoutingLevel, RoutingPolicy
from ai_supervisor.evaluation.harness import evaluate_spec, load_scenarios
from ai_supervisor.models import StageExecutor, StageOperation, StageSpec, TaskSpec, TaskType


def _spec() -> TaskSpec:
    return TaskSpec(
        title="Niri",
        goal="Импортировать Niri и опубликовать",
        task_type=TaskType.COMPOSITE,
        acceptance_criteria=["published"],
        requires_file_changes=True,
        stages=[
            StageSpec(
                id="import",
                operation=StageOperation.IMPORT_CONFIG,
                goal="import",
                executor=StageExecutor.PYTHON,
                acceptance_criteria=["exists"],
                destination="niri/config.kdl",
            ),
            StageSpec(
                id="publish",
                operation=StageOperation.PUBLISH,
                goal="publish",
                depends_on=["import"],
                executor=StageExecutor.GIT,
                acceptance_criteria=["sha"],
            ),
        ],
    )


def test_scenario_library_has_categories_and_offline_harness() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 20
    assert {"read_only", "modify", "git", "composite", "injection"} <= {
        item["category"] for item in scenarios
    }
    scenario = next(item for item in scenarios if item["id"] == "niri")
    result = evaluate_spec(scenario, _spec(), level=3)
    assert result.checks["task_type"]
    assert result.checks["required_operations"]
    assert result.checks["unique_stage_ids"]


def test_prompt_profiles_are_versioned_and_role_specific() -> None:
    active = PromptRegistry(PROMPTS_DIR)
    assert active.version == "2.6.0"
    assert "submit_intent" in active.get("classifier").text
    assert "submit_intent" not in active.get("planner").text
    assert "COMPOSITE" in active.get("planner").text
    baseline = PromptRegistry(PROMPTS_DIR / "baseline" / "2.5.1")
    assert baseline.version == "2.5.1"


def test_routing_levels_and_reasoning_profiles() -> None:
    policy = RoutingPolicy()
    assert policy.for_request("Покажи статус") is RoutingLevel.SIMPLE
    assert policy.for_request("Спроектируй безопасную архитектуру") is RoutingLevel.COMPLEX
    assert policy.for_request("Перепиши README") is RoutingLevel.STANDARD
    assert policy.reasoning(RoutingLevel.SIMPLE).max_tokens == 2048
    assert policy.reasoning(RoutingLevel.COMPLEX).effort == "max"
