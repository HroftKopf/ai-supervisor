from __future__ import annotations

import shutil

import pytest

from ai_supervisor.core.sandbox import BwrapSandbox, SandboxUnavailable


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
async def test_bwrap_probe(settings) -> None:
    sandbox = BwrapSandbox(settings)
    await sandbox.check_available()


def test_unknown_backend_is_refused(settings) -> None:
    from ai_supervisor.core.sandbox import create_sandbox

    settings.sandbox.backend = "none"
    settings.security.allow_unsafe_no_sandbox = False
    with pytest.raises(SandboxUnavailable):
        create_sandbox(settings)
