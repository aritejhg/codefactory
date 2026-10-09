"""Authenticated HTTP surface for the durable workflow controller."""

from __future__ import annotations

import hmac
import json
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping
from collections.abc import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from .github import GitHubConfig, verify_webhook
from .store import Store
from .workflow import WorkflowError, WorkflowService


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: StrictStr = Field(min_length=3, max_length=200)
    issue_number: StrictInt = Field(gt=0)
    title: StrictStr = Field(min_length=1, max_length=1024)
    issue_body: StrictStr | None = Field(default=None, max_length=64 * 1024)

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        value = value.strip()
        if value.count("/") != 1 or any(not part.strip() for part in value.split("/")):
            raise ValueError("repository must use owner/name format")
        if any(ch.isspace() for ch in value):
            raise ValueError("repository cannot contain whitespace")
        return value

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: StrictStr = Field(min_length=1, max_length=64 * 1024)


class PlanApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_version: StrictInt = Field(gt=0)
    approved: StrictBool
    comment: StrictStr | None = Field(default=None, max_length=16 * 1024)


class PullRequestApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    head_sha: StrictStr = Field(min_length=40, max_length=64)
    approved: StrictBool
    comment: StrictStr | None = Field(default=None, max_length=16 * 1024)


def _read_auth_tokens(path: Path) -> dict[str, str]:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("auth token file must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("auth token file must be owned by the controller user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("auth token file must have mode 0600 or stricter")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("controller auth token file could not be loaded") from exc
    if not isinstance(value, dict) or not value:
        raise RuntimeError("controller auth token file must contain a non-empty JSON object")
    return _validate_auth_tokens(value)


def _validate_auth_tokens(value: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for token, principal in value.items():
        if (
            not isinstance(token, str)
            or len(token) < 16
            or not isinstance(principal, str)
            or not principal.strip()
            or len(principal) > 256
        ):
            raise RuntimeError("controller auth token mapping is invalid")
        result[token] = principal.strip()
    return result


def _environment_auth_tokens() -> dict[str, str]:
    configured = os.environ.get("CODEFACTORY_AUTH_TOKENS_FILE")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config" / "codefactory" / "operator-tokens.json"
    )
    return _read_auth_tokens(path)


def create_app(
    db_path: str | Path | None = None,
    *,
    store: Store | None = None,
    workflow: WorkflowService | None = None,
    auth_tokens: Mapping[str, str] | None = None,
    auth_tokens_file: str | Path | None = None,
    github_config: GitHubConfig | None = None,
    github: Any | None = None,
    scheduler: Any | None = None,
    runtime_status: Callable[[], Mapping[str, Any]] | None = None,
) -> FastAPI:
    """Build an app with fail-closed bearer auth and workflow-only mutations.

    ``auth_tokens`` maps bearer token values to owner principals and is useful
    for service composition and tests. The default on-disk mapping is JSON at
    ``~/.config/codefactory/operator-tokens.json`` and must be user-owned with
    mode 0600. No task route is available until at least one mapping is loaded.
    """
    if db_path is None:
        db_path = os.environ.get("CODEFACTORY_DB_PATH", "codefactory.sqlite3")
    if auth_tokens is not None:
        tokens = _validate_auth_tokens(auth_tokens)
    elif auth_tokens_file is not None:
        tokens = _read_auth_tokens(Path(auth_tokens_file).expanduser())
    else:
        tokens = _environment_auth_tokens()

    if workflow is not None:
        if store is not None and workflow.store is not store:
            raise ValueError("injected workflow and store must use the same Store")
        store = workflow.store
    elif store is None:
        store = Store(db_path)
        workflow = WorkflowService(store)
    else:
        workflow = WorkflowService(store)
    if scheduler is not None and getattr(scheduler, "workflow", workflow) is not workflow:
        raise ValueError("scheduler must use the app's WorkflowService")

    @asynccontextmanager
    async def lifespan(_api: FastAPI):
        if scheduler is not None:
            await scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            store.close()

    api = FastAPI(title="Codefactory", version="0.2.0", lifespan=lifespan)
    api.state.store = store
    api.state.workflow = workflow
    api.state.scheduler = scheduler
    api.state.github = github
    api.state.github_config = github_config

    @api.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, exc: WorkflowError) -> Response:
        return Response(
            content=json.dumps({"detail": str(exc)}),
            status_code=exc.status_code,
            media_type="application/json",
        )

    def authenticate(authorization: str | None = Header(default=None)) -> str:
        if not tokens:
            raise HTTPException(status_code=503, detail="controller authentication is not configured")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="bearer authentication required")
        candidate = authorization[7:]
        for expected, principal in tokens.items():
            if hmac.compare_digest(candidate, expected):
                return principal
        raise HTTPException(status_code=401, detail="invalid bearer token")

    def owned_task(task_id: int, principal: str) -> dict[str, Any]:
        return workflow.get_task(task_id, principal=principal)

    @api.get("/health")
    def health() -> dict[str, Any]:
        result = {
            "status": "ok",
            "authentication_configured": bool(tokens),
            "scheduler_configured": scheduler is not None,
        }
        if runtime_status is not None:
            result.update(runtime_status())
        return result

    @api.post("/tasks", status_code=201)
    def create_task(
        payload: TaskCreate,
        principal: str = Depends(authenticate),
    ) -> dict[str, Any]:
        result = workflow.create_manual_task(
            repository=payload.repository,
            issue_number=payload.issue_number,
            title=payload.title,
            owner_principal=principal,
            issue_body=payload.issue_body,
        )
        # POST is idempotent on repository + issue and cannot set trusted=true.
        return result

    @api.get("/tasks")
    def list_tasks(principal: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return workflow.list_tasks(principal=principal)

    @api.get("/tasks/{task_id}")
    def get_task(task_id: int, principal: str = Depends(authenticate)) -> dict[str, Any]:
        return owned_task(task_id, principal)

    @api.get("/tasks/{task_id}/events")
    def list_events(task_id: int, principal: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return workflow.list_events(task_id, principal=principal)

    @api.get("/tasks/{task_id}/messages")
    def list_messages(task_id: int, principal: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return workflow.list_messages(task_id, principal=principal)

    @api.post("/tasks/{task_id}/messages", status_code=202)
    def submit_message(
        task_id: int,
        payload: UserMessage,
        principal: str = Depends(authenticate),
    ) -> dict[str, Any]:
        owned_task(task_id, principal)
        return workflow.submit_user_message(task_id, principal=principal, content=payload.content)

    @api.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: int, principal: str = Depends(authenticate)) -> dict[str, Any]:
        owned_task(task_id, principal)
        result = workflow.cancel_task(task_id, principal=principal)
        if scheduler is not None:
            await scheduler.cancel_task(task_id)
        return result

    @api.post("/tasks/{task_id}/approve-plan")
    def approve_plan(
        task_id: int,
        payload: PlanApproval,
        principal: str = Depends(authenticate),
    ) -> dict[str, Any]:
        owned_task(task_id, principal)
        return workflow.approve_plan(
            task_id,
            principal=principal,
            plan_version=payload.plan_version,
            approved=payload.approved,
            comment=payload.comment,
        )

    @api.post("/tasks/{task_id}/approve-pr")
    def approve_pr(
        task_id: int,
        payload: PullRequestApproval,
        principal: str = Depends(authenticate),
    ) -> dict[str, Any]:
        owned_task(task_id, principal)
        return workflow.approve_pull_request(
            task_id,
            principal=principal,
            head_sha=payload.head_sha,
            approved=payload.approved,
            comment=payload.comment,
        )

    @api.post("/tasks/{task_id}/merge")
    def merge_pr(task_id: int, principal: str = Depends(authenticate)) -> dict[str, Any]:
        owned_task(task_id, principal)
        if github is None:
            raise HTTPException(status_code=503, detail="GitHub client is not configured")
        return workflow.merge_pull_request(task_id, principal=principal, github=github)

    @api.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> dict[str, Any]:
        if github_config is None or not github_config.webhook_secret:
            raise HTTPException(status_code=503, detail="GitHub webhook is not configured")
        raw_body = await request.body()
        if len(raw_body) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        try:
            verified = verify_webhook(
                secret=github_config.webhook_secret,
                raw_body=raw_body,
                signature=request.headers.get("x-hub-signature-256"),
                event=request.headers.get("x-github-event"),
                delivery_id=request.headers.get("x-github-delivery"),
            )
        except ValueError:
            raise HTTPException(status_code=401, detail="invalid GitHub webhook") from None

        payload_data = verified.payload
        repository_data = payload_data.get("repository")
        if not isinstance(repository_data, Mapping):
            # Record signed but irrelevant deliveries so they do not repeatedly
            # reach the controller.
            created = workflow.record_webhook_delivery(verified.delivery_id, verified.event)
            return {"accepted": False, "duplicate": not created}
        repository = repository_data.get("full_name")

        if verified.event == "issues":
            issue_data = payload_data.get("issue")
            action = payload_data.get("action")
            if (
                not isinstance(repository, str)
                or not github_config.allows_repository(repository)
                or action not in {"opened", "reopened", "labeled"}
                or not isinstance(issue_data, Mapping)
                or "pull_request" in issue_data
            ):
                created = workflow.record_webhook_delivery(verified.delivery_id, verified.event)
                return {"accepted": False, "duplicate": not created}
            user = issue_data.get("user")
            login = user.get("login") if isinstance(user, Mapping) else None
            labels_data = issue_data.get("labels", [])
            if not isinstance(login, str) or not login.strip() or not isinstance(labels_data, list):
                raise HTTPException(status_code=422, detail="invalid issue webhook data")
            labels = tuple(
                label.get("name")
                for label in labels_data
                if isinstance(label, Mapping) and isinstance(label.get("name"), str)
            )
            if not any(label.casefold() == github_config.admission_label.casefold() for label in labels):
                created = workflow.record_webhook_delivery(verified.delivery_id, verified.event)
                return {"accepted": False, "duplicate": not created}
            from types import SimpleNamespace

            issue_number = issue_data.get("number")
            issue_title = issue_data.get("title")
            if not isinstance(issue_number, int) or isinstance(issue_number, bool) or not isinstance(issue_title, str):
                raise HTTPException(status_code=422, detail="invalid issue webhook data")
            issue = SimpleNamespace(
                repository=repository,
                number=issue_number,
                title=issue_title,
                body=issue_data.get("body") if isinstance(issue_data.get("body"), str) else None,
                labels=labels,
            )
            result = workflow.admit_github_issue(
                issue,
                owner_principal=f"github:{login.strip().casefold()}",
                repository_allowed=True,
                admission_label=github_config.admission_label,
                idempotency_key=verified.delivery_id,
                delivery_id=verified.delivery_id,
                delivery_event=verified.event,
            )
            return {"accepted": result is not None, "duplicate": result is None, "task": result}

        if verified.event == "issue_comment":
            issue_data = payload_data.get("issue")
            comment_data = payload_data.get("comment")
            action = payload_data.get("action")
            if (
                action != "created"
                or not isinstance(repository, str)
                or not github_config.allows_repository(repository)
                or not isinstance(issue_data, Mapping)
                or "pull_request" in issue_data
                or not isinstance(comment_data, Mapping)
            ):
                created = workflow.record_webhook_delivery(verified.delivery_id, verified.event)
                return {"accepted": False, "duplicate": not created}
            author_data = comment_data.get("user")
            login = author_data.get("login") if isinstance(author_data, Mapping) else None
            number = issue_data.get("number")
            body = comment_data.get("body")
            if not isinstance(login, str) or not isinstance(number, int) or isinstance(number, bool) or not isinstance(body, str):
                raise HTTPException(status_code=422, detail="invalid issue comment webhook data")
            accepted = workflow.queue_github_comment(
                delivery_id=verified.delivery_id,
                event_name=verified.event,
                repository=repository,
                issue_number=number,
                author_principal=f"github:{login.strip().casefold()}",
                content=body,
            )
            return {"accepted": accepted, "duplicate": not accepted}

        created = workflow.record_webhook_delivery(verified.delivery_id, verified.event)
        return {"accepted": False, "duplicate": not created}

    return api
