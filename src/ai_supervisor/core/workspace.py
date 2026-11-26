from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ai_supervisor.config import Settings
from ai_supervisor.core.git_utils import GitError, git, git_output, run_process
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.sandbox import BwrapSandbox, SandboxPaths
from ai_supervisor.models import CheckResult, Evidence, ProjectRecord, TaskRecord, WorkerResult

SENSITIVE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
MAX_CHECK_SECONDS = 900


class WorkspaceError(RuntimeError):
    pass


class SensitiveChangeError(WorkspaceError):
    pass


class WorkspaceManager:
    def __init__(self, settings: Settings, sandbox: BwrapSandbox, redactor: SecretRedactor) -> None:
        self.settings = settings
        self.sandbox = sandbox
        self.redactor = redactor

    def branch_name(self, task: TaskRecord) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", task.title.lower()).strip("-")[:40] or "task"
        return f"ai-supervisor/task-{task.id:04d}-{slug}"

    def worktree_path(self, task: TaskRecord) -> Path:
        return (self.settings.worktrees_dir / f"task-{task.id:04d}").resolve()

    async def prepare(self, task: TaskRecord, project: ProjectRecord) -> dict[str, str]:
        repo = Path(project.path).resolve()
        self._assert_inside_allowed(repo)
        if not (repo / ".git").exists():
            raise WorkspaceError(f"project is not a git repository: {repo}")
        base_ref = task.base_ref or project.base_branch
        base_result = await git(
            repo, "rev-parse", "--verify", f"refs/heads/{base_ref}", check=False
        )
        if base_result.returncode != 0:
            raise WorkspaceError(f"base branch does not exist: {base_ref}")
        base_commit = (await git_output(repo, "rev-parse", base_ref)).strip()
        if task.worktree_path:
            path = Path(task.worktree_path).resolve()
            if path != self.worktree_path(task):
                raise WorkspaceError("stored worktree path is not the managed path for this task")
            if path.exists():
                await self._verify_worktree(path, repo)
                branch = task.branch or self.branch_name(task)
                await self._verify_worktree_branch(path, branch)
                return {
                    "worktree_path": str(path),
                    "branch": branch,
                    "base_ref": base_ref,
                    "base_commit": task.base_commit or base_commit,
                }
        branch = task.branch or self.branch_name(task)
        path = self.worktree_path(task)
        if path.exists():
            await self._verify_worktree(path, repo)
            await self._verify_worktree_branch(path, branch)
            return {
                "worktree_path": str(path),
                "branch": branch,
                "base_ref": base_ref,
                "base_commit": task.base_commit or base_commit,
            }
        self.settings.worktrees_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        await git(repo, "worktree", "prune", timeout_seconds=30, check=False)
        start_point = task.base_commit or base_ref
        if task.base_commit:
            exists = await git(
                repo, "cat-file", "-e", f"{task.base_commit}^{{commit}}", check=False
            )
            if exists.returncode != 0:
                raise WorkspaceError(f"task base commit no longer exists: {task.base_commit}")
        existing_branch = await git(
            repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False
        )
        try:
            if existing_branch.returncode == 0:
                await git(
                    repo,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "worktree",
                    "add",
                    str(path),
                    branch,
                    timeout_seconds=120,
                )
            else:
                await git(
                    repo,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    start_point,
                    timeout_seconds=120,
                )
        except GitError as exc:
            raise WorkspaceError(f"не удалось создать git worktree: {exc}") from exc
        await self._verify_worktree(path, repo)
        return {
            "worktree_path": str(path),
            "branch": branch,
            "base_ref": base_ref,
            "base_commit": task.base_commit or base_commit,
        }

    async def _verify_worktree(self, path: Path, repo: Path) -> None:
        try:
            path.resolve().relative_to(self.settings.worktrees_dir)
        except ValueError as exc:
            raise WorkspaceError(f"worktree path escapes the managed directory: {path}") from exc
        result = await git(path, "rev-parse", "--is-inside-work-tree", check=False)
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise WorkspaceError(f"not a valid git worktree: {path}")
        top = await git_output(path, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != path.resolve():
            raise WorkspaceError(f"worktree top-level mismatch: {top}")

    async def _verify_worktree_branch(self, path: Path, branch: str) -> None:
        current = await git(path, "symbolic-ref", "--short", "HEAD", check=False)
        if current.returncode != 0 or current.stdout.strip() != branch:
            raise WorkspaceError(
                f"worktree is on branch {current.stdout.strip() or '(detached)'}, expected {branch}"
            )

    async def run_checks(
        self,
        *,
        task: TaskRecord,
        project: ProjectRecord,
        worktree: Path,
        dsh_home: Path,
        state_dir: Path,
        timeout_seconds: float,
    ) -> list[CheckResult]:
        dsh_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths = self.sandbox_paths(worktree, dsh_home, state_dir, self.extra_read_paths(project))
        commands = [("test", project.test_command), ("build", project.build_command)]
        results: list[CheckResult] = []
        for name, command in commands:
            if not command:
                continue
            argv = self.sandbox.check_argv(paths, command)
            result = await run_process(
                argv,
                timeout_seconds=min(timeout_seconds, MAX_CHECK_SECONDS),
                max_output_bytes=500_000,
            )
            results.append(
                CheckResult(
                    name=name,
                    command=command,
                    exit_code=result.returncode,
                    duration_seconds=result.duration_seconds,
                    timed_out=result.timed_out,
                    stdout_tail=self.redactor.redact(result.stdout[-6000:]),
                    stderr_tail=self.redactor.redact(result.stderr[-6000:]),
                )
            )
        return results

    def sandbox_paths(
        self,
        worktree: Path,
        dsh_home: Path,
        state_dir: Path,
        readonly_roots: tuple[Path, ...] = (),
    ) -> SandboxPaths:
        # Derive the worktree-specific gitdir synchronously; this happens once per task.
        common = self._common_dir_sync(worktree)
        if common is None:
            return SandboxPaths(
                worktree=worktree,
                dsh_home=dsh_home,
                state_dir=state_dir,
                readonly_roots=readonly_roots,
            )
        git_dir = self._worktree_git_dir_sync(worktree, common)
        return SandboxPaths(
            worktree=worktree,
            dsh_home=dsh_home,
            state_dir=state_dir,
            git_common_dir=common,
            git_worktree_name=git_dir.name if git_dir else None,
            readonly_roots=readonly_roots,
        )

    @staticmethod
    def extra_read_paths(project: ProjectRecord) -> tuple[Path, ...]:
        import json

        try:
            raw = json.loads(project.extra_read_paths_json or "[]")
        except json.JSONDecodeError:
            return ()
        result: list[Path] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                path = Path(str(item)).expanduser().resolve(strict=True)
            except OSError:
                continue
            if path == Path("/") or path == Path.home():
                continue
            result.append(path)
        return tuple(result)

    def _common_dir_sync(self, worktree: Path) -> Path | None:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            return Path(out).resolve()
        except Exception:
            return None

    def _worktree_git_dir_sync(self, worktree: Path, common: Path) -> Path | None:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            git_dir = Path(out).resolve()
            if git_dir.parent != (common / "worktrees"):
                return None
            return git_dir
        except Exception:
            return None

    async def collect_evidence(
        self,
        *,
        task: TaskRecord,
        project: ProjectRecord,
        worktree: Path,
        dsh_home: Path,
        state_dir: Path,
        worker: WorkerResult,
        check_timeout_seconds: float,
        diff_path: Path,
    ) -> Evidence:
        status_result = await git(
            worktree, "status", "--porcelain=v1", timeout_seconds=30, check=False
        )
        checks = await self.run_checks(
            task=task,
            project=project,
            worktree=worktree,
            dsh_home=dsh_home,
            state_dir=state_dir,
            timeout_seconds=check_timeout_seconds,
        )
        staged = await git(worktree, "add", "-A", timeout_seconds=60, check=False)
        if staged.returncode != 0:
            raise WorkspaceError(f"git add failed: {staged.stderr.strip()}")
        base_commit = task.base_commit or "HEAD"
        changed_result = await git(
            worktree,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            base_commit,
            timeout_seconds=30,
            check=False,
        )
        changed_files = [item for item in changed_result.stdout.split("\x00") if item]
        self._guard_sensitive_paths(changed_files)
        diff_result = await git(
            worktree,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base_commit,
            timeout_seconds=120,
            check=False,
        )
        stat_result = await git(
            worktree, "diff", "--cached", "--stat", base_commit, timeout_seconds=60, check=False
        )
        full_diff = self.redactor.redact(diff_result.stdout)
        diff_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        diff_path.write_text(full_diff, encoding="utf-8")
        diff_path.chmod(0o600)
        truncated = len(full_diff) > self.settings.context.max_diff_chars
        diff = _truncate_middle(full_diff, self.settings.context.max_diff_chars)
        head_commit = (await git_output(worktree, "rev-parse", "HEAD")).strip()
        verification_note = ""
        if not checks:
            verification_note = (
                "В проекте не настроены test_command/build_command. "
                "Фактические проверки не запускались; статус проверки ограничен diff и exit code исполнителя."
            )
        elif any(not check.passed for check in checks):
            verification_note = "Одна или несколько проверок оркестратора не пройдены."
        return Evidence(
            task_id=task.id,
            base_commit=base_commit,
            head_commit=head_commit,
            status_porcelain=status_result.stdout[: self.settings.limits.max_worker_log_chars],
            diff_stat=stat_result.stdout[:20000],
            diff=diff,
            diff_truncated=truncated,
            changed_files=changed_files,
            checks=checks,
            worker=worker,
            verification_note=verification_note,
        )

    async def commit_changes(
        self,
        *,
        task: TaskRecord,
        worktree: Path,
        changed: bool,
        message: str | None = None,
    ) -> str:
        if not changed:
            return (await git_output(worktree, "rev-parse", "HEAD")).strip()
        staged = await git(worktree, "diff", "--cached", "--quiet", "HEAD", check=False)
        if staged.returncode == 0:
            # The cumulative task diff may be non-empty after an earlier rework
            # commit while this attempt introduced no new changes.
            return (await git_output(worktree, "rev-parse", "HEAD")).strip()
        commit_message = (
            message or f"{self.settings.git.commit_prefix}: task {task.id:04d} — {task.title}"
        )[:200]
        result = await git(
            worktree,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"user.name={self.settings.git.author_name}",
            "-c",
            f"user.email={self.settings.git.author_email}",
            "commit",
            "--no-verify",
            "-m",
            commit_message,
            timeout_seconds=120,
            check=False,
        )
        if result.returncode != 0:
            raise WorkspaceError(f"git commit failed: {result.stderr.strip()[:2000]}")
        return (await git_output(worktree, "rev-parse", "HEAD")).strip()

    async def is_applied(self, task: TaskRecord, project: ProjectRecord) -> bool:
        """Detect a merge that completed before the DB transition (crash recovery)."""
        if not task.head_commit:
            return False
        repo = Path(project.path).resolve()
        base_head = (await git_output(repo, "rev-parse", project.base_branch)).strip()
        if base_head == task.head_commit:
            return True
        result = await git(
            repo,
            "merge-base",
            "--is-ancestor",
            task.head_commit,
            project.base_branch,
            check=False,
        )
        return result.returncode == 0

    async def apply(self, task: TaskRecord, project: ProjectRecord, *, cleanup: bool = True) -> str:
        repo = Path(project.path).resolve()
        self._assert_inside_allowed(repo)
        if not task.branch or not task.head_commit:
            raise WorkspaceError("task has no branch/head commit")
        branch_now = await git(repo, "symbolic-ref", "--short", "HEAD", check=False)
        if branch_now.returncode != 0 or branch_now.stdout.strip() != project.base_branch:
            raise WorkspaceError(
                f"главная рабочая копия должна быть на ветке {project.base_branch!r}; "
                f"сейчас: {branch_now.stdout.strip() or '(detached)'}"
            )
        status = await git_output(repo, "status", "--porcelain=v1")
        if status.strip():
            raise WorkspaceError(
                "главная рабочая копия содержит незакоммиченные изменения; merge отменён"
            )
        base_head = (await git_output(repo, "rev-parse", project.base_branch)).strip()
        if task.base_commit and base_head != task.base_commit:
            raise WorkspaceError(
                "базовая ветка изменилась после постановки задачи; автоматический merge невозможен "
                "(требуется интеграция в отдельной ветке)"
            )
        result = await git(
            repo,
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--ff-only",
            task.branch,
            timeout_seconds=120,
            check=False,
        )
        if result.returncode != 0:
            await git(repo, "merge", "--abort", check=False)
            raise WorkspaceError(
                f"merge отклонён: {result.stderr.strip()[:2000] or result.stdout.strip()[:2000]}"
            )
        new_head = (await git_output(repo, "rev-parse", "HEAD")).strip()
        if new_head != task.head_commit:
            raise WorkspaceError(
                "merge завершился не на ожидаемом коммите; проверьте ветку вручную"
            )
        if cleanup:
            warnings = await self.cleanup(task, project, remove_branch=True, strict=False)
            for warning in warnings:
                logging.getLogger(__name__).warning("post-merge cleanup: %s", warning)
        return new_head

    async def cleanup(
        self,
        task: TaskRecord,
        project: ProjectRecord,
        *,
        remove_branch: bool,
        force_branch: bool = False,
        strict: bool = True,
    ) -> list[str]:
        repo = Path(project.path).resolve()
        warnings: list[str] = []
        if task.worktree_path:
            path = Path(task.worktree_path)
            if path.exists():
                result = await git(
                    repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(path),
                    timeout_seconds=120,
                    check=False,
                )
                if result.returncode != 0:
                    message = f"git worktree remove failed: {result.stderr.strip()[:1000]}"
                    if strict:
                        raise WorkspaceError(message)
                    warnings.append(message)
        if remove_branch and task.branch:
            flag = "-D" if force_branch else "-d"
            result = await git(repo, "branch", flag, task.branch, timeout_seconds=30, check=False)
            if result.returncode != 0:
                message = f"git branch cleanup failed: {result.stderr.strip()[:1000]}"
                if strict:
                    raise WorkspaceError(message)
                warnings.append(message)
        return warnings

    async def rollback(self, task: TaskRecord, project: ProjectRecord) -> Path:
        repo = Path(project.path).resolve()
        self._assert_inside_allowed(repo)
        rollback_dir = self.settings.rollbacks_dir
        rollback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        patch_path = rollback_dir / f"task-{task.id:04d}-{int(time.time())}.patch"
        if task.base_commit and task.head_commit and task.head_commit != task.base_commit:
            patch = await git(
                repo,
                "diff",
                "--binary",
                f"{task.base_commit}..{task.head_commit}",
                timeout_seconds=120,
                check=False,
            )
            content = self.redactor.redact(patch.stdout)
        elif task.worktree_path and Path(task.worktree_path).exists():
            worktree = Path(task.worktree_path)
            await git(worktree, "add", "-N", "-A", timeout_seconds=60, check=False)
            patch = await git(worktree, "diff", "HEAD", timeout_seconds=120, check=False)
            content = self.redactor.redact(patch.stdout)
        else:
            content = ""
        patch_path.write_text(content, encoding="utf-8")
        patch_path.chmod(0o600)
        if task.worktree_path and Path(task.worktree_path).exists():
            await self.cleanup(task, project, remove_branch=True, force_branch=True, strict=False)
        return patch_path

    def _guard_sensitive_paths(self, files: list[str]) -> None:
        bad: list[str] = []
        safe_env_examples = {".env.example", ".env.sample", ".env.template", ".env.test.example"}
        for file in files:
            path = Path(file)
            if path.name in SENSITIVE_NAMES or (
                path.name.startswith(".env") and path.name not in safe_env_examples
            ):
                bad.append(file)
            elif path.suffix.lower() in SENSITIVE_SUFFIXES:
                bad.append(file)
        if bad:
            raise SensitiveChangeError(
                "исполнитель изменил потенциально секретные файлы; коммит и merge остановлены: "
                + ", ".join(bad[:20])
            )

    def _assert_inside_allowed(self, path: Path) -> None:
        for root in self.settings.allowed_roots():
            try:
                path.relative_to(root)
                return
            except ValueError:
                continue
        raise WorkspaceError(f"project path is outside allowed roots: {path}")


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n... [diff truncated, full patch stored on disk] ...\n{tail}"
