"""Controller-owned, mandatory Git task worktrees and a separate turn policy."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .workflow import WorkflowService, role_for_state


class TaskWorkspaces:
    def __init__(self, workflow: WorkflowService, repositories: Mapping[str, str], root: Path):
        self.workflow = workflow
        self.repositories = {name.casefold(): Path(value).expanduser().resolve() for name, value in repositories.items()}
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @staticmethod
    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError("task worktree Git operation failed")
        return result.stdout.strip()

    def path_for(self, task: Mapping[str, Any]) -> Path:
        # Identity components are controller-generated numeric/UUID fields.
        task_id = int(task["id"])
        run_id = str(task["run_id"])
        if not run_id or any(ch not in "0123456789abcdef-" for ch in run_id):
            raise ValueError("invalid task run identity")
        return self.root / f"task-{task_id}-{run_id}"

    async def resolve(self, task: Mapping[str, Any]) -> Path:
        repository = self.repositories.get(str(task["repository"]).casefold())
        if repository is None or not task["trusted"]:
            raise PermissionError("task repository has not been admitted")
        target = self.path_for(task)
        async with self._lock:
            with self.workflow.store._lock:
                saved = self.workflow.store.db.execute("SELECT * FROM task_workspaces WHERE task_id = ?", (task["id"],)).fetchone()
            if not target.exists():
                if saved:
                    raise RuntimeError("persisted task worktree is missing; operator recovery required")
                branch = f"factory/task-{int(task['id'])}-{task['run_id']}"
                baseline = await asyncio.to_thread(self.git, repository, "rev-parse", "HEAD")
                await asyncio.to_thread(self.git, repository, "worktree", "add", "-b", branch, str(target), "HEAD")
                with self.workflow.store.transaction() as db:
                    db.execute("INSERT INTO task_workspaces(task_id, path, branch, base_sha) VALUES (?, ?, ?, ?)",
                               (task["id"], str(target), branch, baseline))
            elif not saved:
                raise PermissionError("workspace was not created by this controller")
            if target.is_symlink() or target.resolve().parent != self.root:
                raise PermissionError("task workspace is not an isolated worktree")
            common = await asyncio.to_thread(self.git, target, "rev-parse", "--path-format=absolute", "--git-common-dir")
            expected = await asyncio.to_thread(self.git, repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if Path(common).resolve() != Path(expected).resolve():
                raise PermissionError("task workspace does not belong to its repository")
            branch_now = await asyncio.to_thread(self.git, target, "branch", "--show-current")
            expected_branch = saved["branch"] if saved else branch
            if branch_now != expected_branch:
                raise PermissionError("task workspace branch identity changed")
        return target

    async def authorize(self, task: Mapping[str, Any], role: str, session_id: str, cwd: Path) -> bool:
        if not task["trusted"] or role_for_state(task) != role or str(task["repository"]).casefold() not in self.repositories:
            return False
        session = self.workflow.get_session_by_id(session_id)
        matched = bool(
            session and session["task_id"] == task["id"] and session["role"] == role
            and Path(session["cwd"]).resolve() == cwd == self.path_for(task)
            and cwd.parent == self.root and not cwd.is_symlink()
        )
        if not matched:
            return False
        try:
            return await self.resolve(task) == cwd
        except Exception:
            return False

    def read_commit(self, task: Mapping[str, Any], cwd: Path) -> dict[str, str]:
        if cwd != self.path_for(task) or self.git(cwd, "status", "--porcelain"):
            raise RuntimeError("completion requires a clean task worktree commit")
        head = self.git(cwd, "rev-parse", "HEAD")
        with self.workflow.store._lock:
            saved = self.workflow.store.db.execute("SELECT base_sha FROM task_workspaces WHERE task_id = ? AND path = ?", (task["id"], str(cwd))).fetchone()
        if not saved:
            raise RuntimeError("task baseline was not persisted")
        baseline = saved["base_sha"]
        if head == baseline or head == task.get("head_sha"):
            raise RuntimeError("completion requires a new measured commit")
        return {"head_sha": head}

    def review_context(self, task: Mapping[str, Any], cwd: Path) -> str:
        if task["state"] != "CODE_REVIEW":
            return ""
        if self.git(cwd, "rev-parse", "HEAD") != task.get("head_sha"):
            raise RuntimeError("review workspace does not match the current commit")
        with self.workflow.store._lock:
            saved = self.workflow.store.db.execute("SELECT base_sha FROM task_workspaces WHERE task_id = ? AND path = ?", (task["id"], str(cwd))).fetchone()
        if not saved:
            raise RuntimeError("task baseline was not persisted")
        diff = self.git(cwd, "diff", "--no-ext-diff", saved["base_sha"], task["head_sha"])
        if len(diff.encode()) > 128 * 1024:
            raise RuntimeError("commit exceeds bounded tool-free review context")
        return "\nController-measured commit diff:\n" + diff
