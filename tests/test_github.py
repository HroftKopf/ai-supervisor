from __future__ import annotations

import pytest
from aiohttp import web

from ai_supervisor.config import GitHubSection
from ai_supervisor.core.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubNotFound,
)


@pytest.mark.asyncio
async def test_github_exists_create_and_not_found() -> None:
    created: dict[str, object] = {}

    async def repo(request: web.Request) -> web.Response:
        if request.match_info["repo"] == "missing":
            raise web.HTTPNotFound()
        return web.json_response(
            {
                "full_name": "owner/demo",
                "private": True,
                "default_branch": "main",
                "html_url": "https://github.com/owner/demo",
            }
        )

    async def create(request: web.Request) -> web.Response:
        created.update(await request.json())
        return web.json_response(
            {
                "full_name": "owner/new-repo",
                "private": created.get("private"),
                "html_url": "https://github.com/owner/new-repo",
            },
            status=201,
        )

    app = web.Application()
    app.router.add_get("/repos/owner/{repo}", repo)
    app.router.add_post("/user/repos", create)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    client = GitHubClient(
        token="test-token",
        config=GitHubSection(api_url=f"http://127.0.0.1:{port}", timeout_seconds=5),
    )
    try:
        assert await client.repository_exists("owner", "demo") is True
        assert await client.repository_exists("owner", "missing") is False
        with pytest.raises(GitHubNotFound):
            await client.verify_repository("owner", "missing")
        result = await client.create_repository("new-repo", private=True)
        assert result["full_name"] == "owner/new-repo"
        assert created["private"] is True
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_github_auth_error_is_classified() -> None:
    async def unauthorized(_: web.Request) -> web.Response:
        return web.json_response({"message": "Bad credentials"}, status=401)

    app = web.Application()
    app.router.add_get("/repos/owner/repo", unauthorized)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    client = GitHubClient(
        token="bad-token",
        config=GitHubSection(api_url=f"http://127.0.0.1:{port}", timeout_seconds=5),
    )
    try:
        with pytest.raises(GitHubAuthError):
            await client.repository_exists("owner", "repo")
    finally:
        await client.close()
        await runner.cleanup()
