from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Any

MAX_PROMPT_BYTES = 2_000_000
MAX_FINAL_CHARS = 100_000


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one DeepSeek Harness task inside the sandbox."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dsh-home", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--provider", default="deepseek-official")
    parser.add_argument("--profile", default="sdk-minimal")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--reasoning-effort", default="")
    parser.add_argument("--patch", action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--cpu-seconds", type=int, default=3000)
    parser.add_argument("--max-open-files", type=int, default=1024)
    parser.add_argument("--max-processes", type=int, default=1024)
    return parser.parse_args(argv)


def _apply_limits(args: argparse.Namespace) -> None:
    # RLIMIT_AS is a real virtual-memory ceiling. Node (bundled with the DSH
    # runtime) works comfortably at 4 GiB; the value is configurable.
    memory = max(256, args.memory_mb) * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except (ValueError, OSError):
        pass
    cpu = max(10, args.cpu_seconds)
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 30))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (args.max_open_files, args.max_open_files))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (args.max_processes, args.max_processes))
    except (ValueError, OSError):
        pass


def _read_prompt() -> str:
    data = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
    if len(data) > MAX_PROMPT_BYTES:
        raise ValueError("prompt is too large")
    text = data.decode("utf-8", errors="strict").strip()
    if not text:
        raise ValueError("empty prompt")
    return text


def _extract_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "requests": 0,
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "usage" and isinstance(value.get("usage"), dict):
                usage = value["usage"]
                totals["input_tokens"] += int(usage.get("inputTokens") or 0)
                totals["output_tokens"] += int(usage.get("outputTokens") or 0)
                totals["cached_tokens"] += int(usage.get("cacheReadTokens") or 0)
                totals["cache_write_tokens"] += int(usage.get("cacheWriteTokens") or 0)
                totals["reasoning_tokens"] += int(usage.get("reasoningTokens") or 0)
                totals["requests"] += 1
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for event in events:
        walk(event)
    return totals


def _extract_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        reason = event.get("data", {}).get("reason")
        if not isinstance(reason, dict) or reason.get("kind") != "error":
            continue
        error = reason.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return str(message)[:4000]
        return "DSH turn ended with an unspecified error"
    for event in reversed(events):
        if event.get("type") == "error":
            return str(event.get("message") or "DSH error event")[:4000]
    return None


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for event in events:
        event_type = event.get("type")
        if isinstance(event_type, str):
            result.append(event_type)
    return result[-40:]


def _write_result(state_dir: Path, payload: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = state_dir / "worker-result.json"
    temp = state_dir / "worker-result.json.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, target)
    os.chmod(target, 0o600)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    _apply_limits(args)
    state_dir = Path(args.state_dir)
    started = time.monotonic()
    result: dict[str, Any] = {
        "session_id": args.session_id,
        "final_response": "",
        "finish_reason": None,
        "error": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "requests": 0,
        },
        "events_count": 0,
        "event_types": [],
        "duration_seconds": 0.0,
    }
    harness = None
    try:
        prompt = _read_prompt()
        from deepseek_harness import DeepSeekHarness

        kwargs: dict[str, Any] = {
            "provider": args.provider,
            "model": args.model,
            "profile": args.profile,
            "cwd": args.workspace,
            "dsh_home": args.dsh_home,
            "max_tokens": args.max_tokens,
            "patches": tuple(args.patch),
            "initialize_timeout_seconds": 60.0,
            "shutdown_timeout_seconds": 5.0,
        }
        if args.reasoning_effort:
            kwargs["reasoning_effort"] = args.reasoning_effort
        harness = DeepSeekHarness(**kwargs)
        if args.resume:
            session = harness.resume_session(args.session_id)
            run_result = session.run(prompt)
        else:
            run_result = harness.run(prompt, session_id=args.session_id)
        usage = _extract_usage(run_result.events)
        result.update(
            {
                "session_id": run_result.session_id,
                "final_response": (run_result.final_response or "")[:MAX_FINAL_CHARS],
                "finish_reason": run_result.finish_reason,
                "usage": usage,
                "events_count": len(run_result.events),
                "event_types": _event_types(run_result.events),
            }
        )
        if run_result.finish_reason != "completed":
            detail = _extract_error(run_result.events)
            result["error"] = detail or f"DSH turn ended with reason: {run_result.finish_reason}"
        result["duration_seconds"] = time.monotonic() - started
        _write_result(state_dir, result)
        return 0
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:4000]}"
        result["duration_seconds"] = time.monotonic() - started
        try:
            _write_result(state_dir, result)
        except Exception:
            traceback.print_exc(file=sys.stderr)
        print(f"worker_runner error: {type(exc).__name__}: {str(exc)[:2000]}", file=sys.stderr)
        return 1
    finally:
        if harness is not None:
            try:
                harness.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
