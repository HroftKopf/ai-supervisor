#!/usr/bin/env python3
"""Offline infrastructure benchmark. No network, no paid model calls."""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_supervisor.config import load_settings  # noqa: E402
from ai_supervisor.core.worker_manager import WorkerScheduler  # noqa: E402
from ai_supervisor.memory.conversations import ConversationService  # noqa: E402
from ai_supervisor.memory.database import Database  # noqa: E402
from ai_supervisor.memory.projects import ProjectService  # noqa: E402
from ai_supervisor.memory.tasks import TaskService  # noqa: E402
from ai_supervisor.models import TaskStatus  # noqa: E402


async def timed(callable_):
    start = time.perf_counter()
    value = await callable_()
    return (time.perf_counter() - start) * 1000, value


async def main() -> int:
    base = load_settings(check_env_permissions=False)
    results: dict[str, float | int | str] = {}
    with tempfile.TemporaryDirectory(prefix="ai-supervisor-bench-") as tmp:
        tmp_path = Path(tmp)
        project_root = tmp_path / "projects"
        project_root.mkdir()
        data = tmp_path / "var"
        app = base.app.model_copy(update={"data_dir": str(data)})
        security = base.security.model_copy(update={"allowed_project_roots": [str(project_root)]})
        settings = base.model_copy(update={"app": app, "security": security})
        settings.ensure_runtime_dirs()
        db = Database(settings.db_path)
        await db.connect()
        try:
            projects = ProjectService(db, settings)
            conversations = ConversationService(db, settings)
            tasks = TaskService(db)
            project = await projects.create("demo")
            conversation = await conversations.get_or_create_conversation(1, project.id)

            for index in range(500):
                await conversations.add_message(
                    conversation.id,
                    "user" if index % 2 == 0 else "assistant",
                    f"memory benchmark message {index} about scheduler and context",
                )
            ms, found = await timed(lambda: conversations.search_messages("scheduler", limit=20))
            results["memory_search_ms"] = round(ms, 3)
            results["memory_search_results"] = len(found)
            ms, built = await timed(lambda: conversations.build_context(conversation.id))
            results["context_build_ms"] = round(ms, 3)
            results["context_messages"] = len(built[1])

            task = await tasks.create(project.id, conversation.id, "benchmark", max_rework=1)
            await tasks.transition(task.id, TaskStatus.PLANNING)
            await tasks.transition(task.id, TaskStatus.QUEUED)
            await tasks.transition(task.id, TaskStatus.RUNNING)
            transitions = 100
            times: list[float] = []
            for _ in range(transitions):
                start = time.perf_counter()
                await tasks.transition(task.id, TaskStatus.VALIDATING)
                await tasks.transition(task.id, TaskStatus.REVIEWING)
                await tasks.transition(task.id, TaskStatus.REWORK)
                await tasks.transition(task.id, TaskStatus.QUEUED)
                await tasks.transition(task.id, TaskStatus.RUNNING)
                times.append((time.perf_counter() - start) * 1000)
            results["state_machine_cycles_ms_median"] = round(statistics.median(times), 3)

            scheduler = WorkerScheduler(4)

            async def no_op() -> None:
                await asyncio.sleep(0)

            ms, _ = await timed(
                lambda: asyncio.gather(*(scheduler.run(i, no_op) for i in range(20)))
            )
            results["scheduler_20_jobs_ms"] = round(ms, 3)

            await tasks.create_worker(
                worker_id="w1", task_id=task.id, kind="deepseek", session_id=None, dsh_home=None
            )
            ms, interrupted = await timed(tasks.mark_interrupted_on_startup)
            results["recovery_ms"] = round(ms, 3)
            results["recovered_tasks"] = len(interrupted)

            tracemalloc.start()
            await conversations.build_context(conversation.id)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            results["context_peak_bytes"] = peak
            results["sqlite_path"] = str(settings.db_path)
            results["status"] = "ok"
        finally:
            await db.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
