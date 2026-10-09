"""Local, supervised controller composition. Cloud credentials are optional."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from .app import _environment_auth_tokens, create_app
from .credentials import ControllerCredentialError, read_controller_token
from .capacity import PullRequestCapacity
from .github import GitHubClient, GitHubConfig
from .native import NativeRoleGateway
from .roles import load_config
from .scheduler import Scheduler
from .store import Store, utc_now
from .workflow import WorkflowService
from .workspaces import TaskWorkspaces


class Runtime:
    def __init__(self, config: dict[str, Any], routes, workflow: WorkflowService, principal: str | None):
        self.workflow = workflow
        self.store = workflow.store
        self.config = config
        self.pr_capacity = PullRequestCapacity(self.store, config["repositories"], config.get("max_open_prs", 5))
        self.principal = principal
        self.gateway = NativeRoleGateway(routes, config.get("adapters"))
        prompt_path = Path(os.environ.get("CODEFACTORY_BASE_PROMPT_FILE", config.get("base_prompt_file", "skills/codefactory/SKILL.md"))).expanduser()
        if not prompt_path.is_absolute():
            prompt_path = Path(__file__).resolve().parent.parent / prompt_path
        try:
            base_prompt = prompt_path.read_text(encoding="utf-8")
            if not base_prompt.strip() or len(base_prompt.encode()) > 32 * 1024:
                raise ValueError("shared base prompt is empty or too large")
            self.base_prompt_reason = None
        except (OSError, ValueError):
            base_prompt = ""
            self.base_prompt_reason = "shared_base_prompt_unavailable"
        if self.base_prompt_reason:
            for role in routes:
                self.gateway.blocked[role] = self.base_prompt_reason
        self.gateway.blocked["implementer"] = "reviewed_foundation_not_enabled"
        self.gateway.blocked["ci_babysitter"] = "bootstrap_ci_execution_not_enabled"
        self.workspaces = TaskWorkspaces(workflow, config["repositories"], Path(config["worktree_root"]))
        self.github = None
        self.github_reason = "factory_github_credential_unavailable"
        token_path = Path(config["github_token_file"]).expanduser()
        # A missing/empty bootstrap token blocks only ingestion, never the API.
        try:
            if any(token_path.resolve().is_relative_to(path) for path in (*self.workspaces.repositories.values(), self.workspaces.root)):
                raise ControllerCredentialError("controller credential must be outside agent workspaces")
            token = read_controller_token(token_path)
        except ControllerCredentialError:
            token = None
        self.github_config = GitHubConfig(token=token, allowed_repositories=frozenset(config["repositories"]))
        if token and principal:
            self.github = GitHubClient(self.github_config, draft_publication_gate=self.pr_capacity)
            self.github_reason = None
        elif token:
            self.github_reason = "single_operator_principal_not_configured"
        self.scheduler = Scheduler(
            workflow, gateway=self.gateway,
            workspace_resolver=self.workspaces.resolve, access_policy=self.authorize,
            # Bootstrap read-only tasks can run now. BUILD/CI are held until
            # reviewed foundation changes are available in the source checkout.
            commit_reader=None, github=self.github,
            role_configs={role: route.session_config() for role, route in routes.items()},
            review_context=self.workspaces.review_context,
            poll_seconds=float(config.get("poll_seconds", 2)),
            quota_retry_seconds=int(config.get("quota_retry_seconds", 900)),
            base_prompt=base_prompt,
            max_active_turns=config.get("max_concurrent_agents", 5),
            capacity_policy=self.pr_capacity.admit,
        )
        self._ingestion_task = None
        self.running = False

    async def authorize(self, task, role, session_id, cwd):
        if self.base_prompt_reason or role in {"implementer", "ci_babysitter"}:
            return False
        return await self.workspaces.authorize(task, role, session_id, cwd)

    async def start(self):
        await self.scheduler.start()
        self.running = True
        if self.github:
            self._ingestion_task = asyncio.create_task(self._ingest_loop(), name="codefactory-github-ingestion")

    async def _ingest_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.pr_capacity.refresh, self.github)
                for repository in self.config["repositories"]:
                    issues = await asyncio.to_thread(self.github.list_admitted_issues, repository)
                    for issue in issues:
                        self.workflow.admit_github_issue(
                            issue, owner_principal=self.principal, repository_allowed=True,
                            admission_label=self.github_config.admission_label,
                        )
                self.github_reason = None
            except Exception:
                self.github_reason = "github_reconciliation_unavailable"
            await asyncio.sleep(60)

    async def stop(self):
        self.running = False
        if self._ingestion_task:
            self._ingestion_task.cancel()
            await asyncio.gather(self._ingestion_task, return_exceptions=True)
        await self.scheduler.stop()
        if self.github:
            self.github.close()

    def status(self):
        with self.store._lock:
            pauses = self.store.db.execute(
                "SELECT task_id, role, reason, retry_after FROM role_pauses WHERE retry_after > ? ORDER BY retry_after", (utc_now(),)
            ).fetchall()
            counts = self.store.db.execute("SELECT state, COUNT(*) AS count FROM tasks GROUP BY state").fetchall()
            turns = self.store.db.execute("SELECT COUNT(*) AS used, SUM(native_started = 1 AND lease_expires_at <= ?) AS uncertain FROM agent_turns WHERE status = 'claimed'", (utc_now(),)).fetchone()
        agent_limit = self.scheduler.max_active_turns
        agent_reason = "native_turn_recovery_required" if turns["uncertain"] else ("agent_capacity_full" if turns["used"] >= agent_limit else None)
        return {
            "status": "running" if self.running else "starting",
            "scheduler_running": self.scheduler._started and bool(self.scheduler._loop_task and not self.scheduler._loop_task.done()),
            "roles": self.gateway.status(),
            "shared_base_prompt": {"status": "blocked" if self.base_prompt_reason else "loaded", "reason": self.base_prompt_reason},
            "github_ingestion": {"status": "blocked" if self.github_reason else "running", "reason": self.github_reason},
            "build_execution": {"status": "blocked", "reason": "reviewed_foundation_not_enabled"},
            "preview": {"status": "blocked", "reason": "local_preview_supervisor_not_implemented"},
            "cloud_ingress": {"status": "blocked", "reason": "cloud_credentials_and_zero_cost_verification_required"},
            "remote_writes": {"status": "blocked", "reason": "draft_review_and_publication_gates_not_enabled"},
            "quota_waits": [dict(row) for row in pauses],
            "task_counts": {row["state"]: row["count"] for row in counts},
            "capacity": {
                "agents": {"limit": agent_limit, "used": turns["used"], "blocked": agent_reason is not None, "reason": agent_reason},
                "active_prs": self.pr_capacity.status(),
            },
        }


def compose_runtime(db_path: str | Path | None = None):
    config_path = Path(os.environ.get("CODEFACTORY_CONFIG", "config/runtime.yaml")).expanduser()
    config, routes = load_config(config_path)
    database = db_path or os.environ.get("CODEFACTORY_DB_PATH") or Path.home() / ".local/state/codefactory/controller.sqlite3"
    store = Store(database)
    workflow = WorkflowService(store)
    tokens = _environment_auth_tokens()
    principals = set(tokens.values())
    runtime = Runtime(config, routes, workflow, next(iter(principals)) if len(principals) == 1 else None)
    return runtime, tokens


def create_runtime_app():
    runtime, tokens = compose_runtime()
    api = create_app(
        workflow=runtime.workflow, auth_tokens=tokens if tokens else None,
        scheduler=runtime, runtime_status=runtime.status,
        # Remote mutations intentionally await the remaining draft/bot review gates.
        github_config=runtime.github_config,
    )
    api.state.runtime = runtime
    return api
