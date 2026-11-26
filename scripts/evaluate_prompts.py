#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_supervisor.agents.supervisor import SupervisorAgent
from ai_supervisor.config import load_settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.routing import RoutingPolicy
from ai_supervisor.evaluation.harness import evaluate_spec, load_scenarios
from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig


async def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate prompt profiles on fixed scenarios")
    parser.add_argument("--live", action="store_true", help="use the configured Kimi API")
    parser.add_argument(
        "--prompts",
        choices=["active", "baseline"],
        default="active",
        help="active manifest or baseline/2.5.1",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json-out", type=Path, default=Path("var/prompt-eval.json"))
    args = parser.parse_args()
    scenarios = load_scenarios()
    if args.limit:
        scenarios = scenarios[: args.limit]
    if not args.live:
        print(f"scenarios={len(scenarios)}; offline mode validates only the scenario set")
        print("Use --live with the owner's explicit approval to call the paid API.")
        return 0

    settings = load_settings()
    if not settings.kimi_key:
        print("KIMI_API_KEY is not configured", file=sys.stderr)
        return 2
    root = None
    if args.prompts == "baseline":
        root = Path(__file__).resolve().parents[1] / "src/ai_supervisor/prompts/baseline/2.5.1"
    redactor = SecretRedactor.from_settings(settings)
    client = KimiClient(
        KimiClientConfig(
            api_key=settings.kimi_key,
            base_url=settings.kimi.base_url,
            model=settings.kimi.model,
            reasoning_effort=settings.kimi.reasoning_effort,
            max_completion_tokens=settings.kimi.max_completion_tokens,
            request_timeout_seconds=settings.kimi.request_timeout_seconds,
            max_retries=0,
            max_concurrent_requests=1,
        ),
        redactor,
    )
    routing = RoutingPolicy(
        level1_effort=settings.routing.level1_effort,
        level1_max_tokens=settings.routing.level1_max_tokens,
        level2_effort=settings.routing.level2_effort,
        level2_max_tokens=settings.routing.level2_max_tokens,
        level3_effort=settings.routing.level3_effort,
        level3_max_tokens=settings.routing.level3_max_tokens,
        classifier_effort=settings.routing.classifier_effort,
        classifier_max_tokens=settings.routing.classifier_max_tokens,
    )
    supervisor = SupervisorAgent(client, settings, redactor, prompt_root=root)
    results = []
    try:
        for scenario in scenarios:
            level = routing.for_request(str(scenario.get("request") or ""))
            profile = routing.reasoning(level)
            if scenario.get("expected_intent") == "chat":
                intent, usage = await supervisor.classify(
                    project_block="name: eval\npath: /tmp/eval",
                    conversation_block="",
                    latest_user_message=str(scenario["request"]),
                    conversation_id=900100,
                    reasoning_effort=routing.classifier_profile.effort,
                    max_completion_tokens=routing.classifier_profile.max_tokens,
                )
                passed = intent.kind.value == "chat"
                results.append(
                    {
                        "id": scenario["id"],
                        "passed": passed,
                        "usage": usage.model_dump(),
                        "level": int(level),
                    }
                )
                continue
            spec, usage = await supervisor.plan(
                project_block="name: eval\npath: /tmp/eval\nbase_branch: main",
                conversation_block="",
                memory_block="",
                project_context="README.md: demo\n",
                latest_user_message=str(scenario["request"]),
                conversation_id=900100,
                reasoning_effort=profile.effort,
                max_completion_tokens=profile.max_tokens,
            )
            evaluated = evaluate_spec(scenario, spec, level=int(level))
            results.append(
                {
                    "id": scenario["id"],
                    "passed": evaluated.passed,
                    "checks": evaluated.checks,
                    "errors": evaluated.errors,
                    "usage": usage.model_dump(),
                    "level": int(level),
                }
            )
            print(scenario["id"], "PASS" if evaluated.passed else "FAIL", usage.total_tokens)
    finally:
        await client.close()
    summary = {
        "prompts": args.prompts,
        "version": supervisor.prompt_metadata().get("version"),
        "scenarios": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "usage": {
            "input_tokens": sum(item["usage"]["input_tokens"] for item in results),
            "output_tokens": sum(item["usage"]["output_tokens"] for item in results),
            "reasoning_tokens": sum(item["usage"]["reasoning_tokens"] for item in results),
            "requests": sum(item["usage"]["requests"] for item in results),
        },
        "results": results,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: summary[key] for key in ("version", "scenarios", "passed", "usage")},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
