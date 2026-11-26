from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp

from ai_supervisor.config import GitHubSection

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class GitHubError(RuntimeError):
    pass


class GitHubAuthError(GitHubError):
    pass


class GitHubPermissionError(GitHubError):
    pass


class GitHubNotFound(GitHubError):
    pass


class GitHubRateLimit(GitHubError):
    pass


class GitHubClient:
    """Small, token-safe GitHub REST client. No token is ever logged."""

    def __init__(self, *, token: str, config: GitHubSection) -> None:
        self.token = token
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": "ai-supervisor/0.1",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.token:
            raise GitHubAuthError("GITHUB_TOKEN is not configured")
        session = await self._get_session()
        url = self.config.api_url.rstrip("/") + path
        for attempt in range(3):
            try:
                async with session.request(method, url, **kwargs) as response:
                    if response.status in {401}:
                        raise GitHubAuthError("GitHub rejected GITHUB_TOKEN (401)")
                    if response.status == 403:
                        remaining = response.headers.get("X-RateLimit-Remaining")
                        if remaining == "0":
                            raise GitHubRateLimit("GitHub API rate limit/quota exhausted (403)")
                        raise GitHubPermissionError("GitHub permission denied (403)")
                    if response.status == 404:
                        raise GitHubNotFound("GitHub resource not found (404)")
                    if response.status == 429:
                        retry = response.headers.get("Retry-After", "?")
                        raise GitHubRateLimit(f"GitHub rate limit (429), Retry-After={retry}")
                    if response.status >= 500 and attempt < 2:
                        await asyncio.sleep(1 + attempt)
                        continue
                    if response.status >= 400:
                        body = (await response.text())[:1000]
                        raise GitHubError(f"GitHub HTTP {response.status}: {body}")
                    if response.status == 204:
                        return None
                    return await response.json()
            except aiohttp.ClientError as exc:
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise GitHubError(f"GitHub network error: {type(exc).__name__}") from exc
        raise GitHubError("GitHub request failed after retries")

    async def authenticated_user(self) -> dict[str, Any]:
        data = await self._request("GET", "/user")
        return if_dict(data)

    async def repository_exists(self, owner: str, repo: str) -> bool:
        validate_owner_repo(owner, repo)
        try:
            await self._request("GET", f"/repos/{owner}/{repo}")
            return True
        except GitHubNotFound:
            return False

    async def repository_info(self, owner: str, repo: str) -> dict[str, Any] | None:
        validate_owner_repo(owner, repo)
        try:
            data = await self._request("GET", f"/repos/{owner}/{repo}")
        except GitHubNotFound:
            return None
        return if_dict(data)

    async def create_repository(
        self,
        name: str,
        *,
        private: bool = True,
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise GitHubError("invalid GitHub repository name")
        payload: dict[str, Any] = {
            "name": name,
            "private": private,
            "description": description[:350],
            "auto_init": False,
        }
        if owner:
            if not OWNER_RE.fullmatch(owner):
                raise GitHubError("invalid GitHub owner")
            data = await self._request("POST", f"/orgs/{owner}/repos", json=payload)
        else:
            data = await self._request("POST", "/user/repos", json=payload)
        return if_dict(data)

    async def verify_repository(self, owner: str, repo: str) -> dict[str, Any]:
        info = await self.repository_info(owner, repo)
        if info is None:
            raise GitHubNotFound(f"GitHub repository {owner}/{repo} does not exist")
        return info


def validate_owner_repo(owner: str, repo: str) -> None:
    if not OWNER_RE.fullmatch(owner) or not NAME_RE.fullmatch(repo):
        raise GitHubError("invalid GitHub owner/repository name")


def if_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
