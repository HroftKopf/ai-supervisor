from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from pydantic import SecretStr

from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.sandbox import BwrapSandbox
from ai_supervisor.core.worker_manager import WorkerManager
from ai_supervisor.core.workspace import WorkspaceManager
from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.tasks import TaskService
from ai_supervisor.models import TaskStatus, TokenUsage

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_SUPERVISOR_RUN_INTEGRATION") != "1" or shutil.which("bwrap") is None,
    reason="set AI_SUPERVISOR_RUN_INTEGRATION=1 to run the real DSH/bwrap smoke test",
)


async def _messages(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    model = str(body.get("model") or "deepseek-v4-flash")
    response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 42, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 42, "output_tokens": 7, "cache_read_input_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    for event in events:
        await response.write(f"data: {json.dumps(event)}\n\n".encode())
    return response


@pytest.mark.asyncio
async def test_real_dsh_worker_in_bwrap(monkeypatch, settings, repo, database, project) -> None:
    app = web.Application()
    app.router.add_post("/v1/messages", _messages)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    try:
        monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{port}/v1")
        settings.secrets.deepseek_api_key = SecretStr("integration-placeholder")
        sandbox = BwrapSandbox(settings)
        await sandbox.check_available()
        conversations = ConversationService(database, settings)
        tasks = TaskService(database)
        conversation = await conversations.get_or_create_conversation(1, project.id)
        task = await tasks.create(project.id, conversation.id, "dsh smoke", max_rework=1)
        task = await tasks.transition(task.id, TaskStatus.PLANNING)
        task = await tasks.transition(task.id, TaskStatus.QUEUED)
        workspace = WorkspaceManager(settings, sandbox, SecretRedactor([]))
        prepared = await workspace.prepare(task, project)
        task = await tasks.update_fields(
            task.id,
            worktree_path=prepared["worktree_path"],
            branch=prepared["branch"],
            base_ref=prepared["base_ref"],
            base_commit=prepared["base_commit"],
            dsh_home=str(settings.dsh_homes_dir / f"task-{task.id:04d}"),
        )
        task = await tasks.transition(task.id, TaskStatus.RUNNING)
        workers = WorkerManager(
            settings=settings,
            sandbox=sandbox,
            workspace=workspace,
            tasks=tasks,
            redactor=SecretRedactor([]),
        )
        result = await workers.run(
            task=task,
            project=project,
            prompt="Ответь одним словом, ничего не меняй.",
            attempt=1,
            resume=False,
            timeout_seconds=180,
        )
        assert result.completed
        assert result.finish_reason == "completed"
        assert result.final_response
        assert result.usage.input_tokens >= 42
        assert result.usage.requests >= 1
        assert result.usage == TokenUsage(
            input_tokens=42,
            output_tokens=7,
            cached_tokens=3,
            requests=1,
        )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_real_dsh_tool_edits_worktree(monkeypatch, settings, repo, database, project) -> None:
    """A Messages mock requests one bash tool call; the real DSH executes it."""
    requests = 0

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal requests
        requests += 1
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if requests == 1:
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_tool",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "deepseek-v4-flash",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "bash",
                        "input": {
                            "command": "printf 'from dsh tool\\n' >> README.md",
                            "description": "Append verification line to README",
                        },
                    },
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"input_tokens": 10, "output_tokens": 8},
                },
                {"type": "message_stop"},
            ]
        else:
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_final",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "deepseek-v4-flash",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 20, "output_tokens": 1},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Готово."},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": 20, "output_tokens": 4},
                },
                {"type": "message_stop"},
            ]
        for event in events:
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
        return response

    app = web.Application()
    app.router.add_post("/v1/messages", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    try:
        monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{port}/v1")
        settings.secrets.deepseek_api_key = SecretStr("integration-placeholder")
        sandbox = BwrapSandbox(settings)
        await sandbox.check_available()
        conversations = ConversationService(database, settings)
        tasks = TaskService(database)
        conversation = await conversations.get_or_create_conversation(1, project.id)
        task = await tasks.create(project.id, conversation.id, "dsh tool smoke", max_rework=1)
        task = await tasks.transition(task.id, TaskStatus.PLANNING)
        task = await tasks.transition(task.id, TaskStatus.QUEUED)
        workspace = WorkspaceManager(settings, sandbox, SecretRedactor([]))
        prepared = await workspace.prepare(task, project)
        task = await tasks.update_fields(
            task.id,
            worktree_path=prepared["worktree_path"],
            branch=prepared["branch"],
            base_ref=prepared["base_ref"],
            base_commit=prepared["base_commit"],
            dsh_home=str(settings.dsh_homes_dir / f"task-{task.id:04d}"),
        )
        task = await tasks.transition(task.id, TaskStatus.RUNNING)
        workers = WorkerManager(
            settings=settings,
            sandbox=sandbox,
            workspace=workspace,
            tasks=tasks,
            redactor=SecretRedactor([]),
        )
        result = await workers.run(
            task=task,
            project=project,
            prompt="Append the requested line to README.md.",
            attempt=1,
            resume=False,
            timeout_seconds=180,
        )
        assert result.completed
        worktree = Path(prepared["worktree_path"])
        assert "from dsh tool" in (worktree / "README.md").read_text()
        assert requests >= 2
        assert result.usage.requests >= 2
    finally:
        await runner.cleanup()
