from __future__ import annotations

from pathlib import Path

from ai_supervisor.core.sandbox import BwrapSandbox, SandboxPaths


def _paths(tmp_path: Path) -> SandboxPaths:
    worktree = tmp_path / "worktree"
    dsh = tmp_path / "dsh"
    state = tmp_path / "state"
    for path in (worktree, dsh, state):
        path.mkdir()
    return SandboxPaths(worktree=worktree, dsh_home=dsh, state_dir=state)


def test_worker_env_omits_control_credentials(settings, tmp_path: Path) -> None:
    sandbox = BwrapSandbox(settings)
    argv = sandbox.worker_argv(
        _paths(tmp_path),
        session_id="s",
        model="deepseek-test",
        provider="deepseek-official",
        profile="sdk-minimal",
        max_tokens=1024,
        reasoning_effort="",
        system_prompt="x",
        deepseek_api_key="synthetic-deepseek-only",
        deepseek_base_url="https://api.deepseek.com",
        resume=False,
        egress_socket="/state/egress.sock",
    )
    joined = "\n".join(argv)
    assert "synthetic-deepseek-only" in joined  # required by DSH executor
    for forbidden in ("KIMI_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN"):
        assert forbidden not in joined
    assert "--clearenv" in argv
    assert "--unshare-net" in argv
    assert "--die-with-parent" in argv
    assert str(Path.home()) not in argv


def test_check_env_omits_all_model_credentials(settings, tmp_path: Path) -> None:
    sandbox = BwrapSandbox(settings)
    argv = sandbox.check_argv(_paths(tmp_path), "echo hello")
    joined = "\n".join(argv)
    for forbidden in (
        "DEEPSEEK_API_KEY",
        "KIMI_API_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "GITHUB_TOKEN",
    ):
        assert forbidden not in joined
    assert "--clearenv" in argv


def test_symlink_outside_worktree_is_not_mounted(settings, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    link = worktree / "link"
    link.symlink_to(outside)
    assert link.is_symlink()
    # The sandbox must mount the worktree path, not follow the symlink as a
    # separate host mount. The worker can see the link but not an extra host path.
    sandbox = BwrapSandbox(settings)
    argv = sandbox.worker_argv(
        SandboxPaths(worktree=worktree, dsh_home=tmp_path / "dsh", state_dir=tmp_path / "state"),
        session_id="s",
        model="m",
        provider="p",
        profile="sdk-minimal",
        max_tokens=1,
        reasoning_effort="",
        system_prompt="x",
        deepseek_api_key="synthetic",
        deepseek_base_url="",
        resume=False,
    )
    assert str(outside) not in argv


def test_worker_argv_is_a_command_list_not_a_shell_string(settings, tmp_path: Path) -> None:
    sandbox = BwrapSandbox(settings)
    argv = sandbox.worker_argv(
        _paths(tmp_path),
        session_id="s",
        model="m",
        provider="p",
        profile="sdk-minimal",
        max_tokens=1,
        reasoning_effort="",
        system_prompt="x; touch /tmp/pwned",
        deepseek_api_key="synthetic",
        deepseek_base_url="",
        resume=False,
    )
    assert isinstance(argv, list)
    assert argv[0] == "bwrap"
    assert "/bin/bash" not in argv
    assert "x; touch /tmp/pwned" in argv  # data in --setenv, never shell input
