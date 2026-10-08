from __future__ import annotations

import asyncio
from functools import wraps
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codefactory.acp import ACPEvent, ACPSession, ACPRemoteError
from codefactory.scheduler import Scheduler
from codefactory.store import Store
from codefactory.workflow import WorkflowService, role_for_state


def admitted(workflow: WorkflowService, issue_number: int = 17) -> dict:
    issue = SimpleNamespace(
        repository="acme/widget",
        number=issue_number,
        title="Make a change",
        body="Issue details",
        labels=("factory:ready",),
    )
    return workflow.admit_github_issue(
        issue, owner_principal="github:alice", repository_allowed=True
    )


class FakeGateway:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.on_event = None
        self.on_agent_request = None
        self.active_sessions: list[ACPSession] = []
        self.sessions: dict[str, ACPSession] = {}
        self.loaded_ids: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.closed = False
        self._sequence = 0

    async def start(self):
        return None

    async def close(self):
        self.closed = True

    def _session(self, session_id: str, role: str, task_id: int, cwd: Path, config: dict) -> ACPSession:
        session = ACPSession(
            session_id=session_id,
            role=role,
            task_id=task_id,
            cwd=str(cwd),
            state_directory="/tmp/fake-codex/sessions",
            capabilities={},
            config_options=(),
            config=config,
        )
        self.sessions[session_id] = session
        self.active_sessions.append(session)
        return session

    async def new_session(self, role: str, task_id: int, cwd: Path, config: dict):
        return self._session(str(uuid4()), role, task_id, Path(cwd), config)

    async def load_session(self, session_id: str, role: str, task_id: int, cwd: Path, config: dict):
        self.loaded_ids.append(session_id)
        return self._session(session_id, role, task_id, Path(cwd), config)

    async def prompt(self, session_id: str, prompt: str):
        self.prompts.append((session_id, prompt))
        value = self.responses.pop(0)
        self._sequence += 1
        event = ACPEvent(
            sequence=self._sequence,
            timestamp=datetime.now(timezone.utc),
            method="session/update",
            params={
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": value},
                },
            },
        )
        result = self.on_event(event)
        if hasattr(result, "__await__"):
            await result
        return {"stopReason": "end_turn"}

    async def cancel(self, session_id: str):
        return None


def run_async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        asyncio.run(function(*args, **kwargs))

    return wrapper


@run_async_test
async def test_quota_wait_survives_restart_and_normal_quota_discussion_completes(tmp_path: Path):
    database = tmp_path / "quota.sqlite3"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    store = Store(database)
    workflow = WorkflowService(store)
    task = admitted(workflow)

    class QuotaGateway(FakeGateway):
        async def prompt(self, session_id, prompt):
            raise ACPRemoteError("session/prompt", 429, "Too many requests")

    first = Scheduler(workflow, gateway=QuotaGateway([]), workspace_resolver=lambda _task: cwd,
                      access_policy=lambda *_args: True)
    await first.run_once()
    saved_id = workflow.get_session(task["id"], "triage")["session_id"]
    assert workflow.get_task(task["id"])["role_failures"] == {}
    assert await first.run_once() is False
    await first.stop()
    store.close()

    store = Store(database)
    workflow = WorkflowService(store)
    with store.transaction() as db:
        db.execute("UPDATE role_pauses SET retry_after = '2000-01-01T00:00:00+00:00'")
    gateway = FakeGateway(['```json\n{"decision":"ready","summary":"Implement quota and rate limit handling","risk":"low","priority":0,"questions":[]}\n```'])
    second = Scheduler(workflow, gateway=gateway, workspace_resolver=lambda _task: cwd,
                       access_policy=lambda *_args: True)
    try:
        assert await second.run_once() is True
        assert gateway.loaded_ids == [saved_id]
        assert workflow.get_task(task["id"])["state"] == "PLAN"
    finally:
        await second.stop()
        store.close()


@pytest.fixture
def workflow(tmp_path: Path):
    store = Store(tmp_path / "scheduler.sqlite3")
    service = WorkflowService(store)
    try:
        yield service
    finally:
        store.close()


@run_async_test
async def test_scheduler_persists_turn_and_resumes_same_native_role_session(tmp_path: Path, workflow: WorkflowService):
    task = admitted(workflow)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    authorization_checks: list[tuple[str, str, str]] = []

    async def policy(current: dict, role: str, session_id: str, workspace: Path) -> bool:
        authorization_checks.append((current["state"], role, str(workspace)))
        return current["trusted"] and role_for_state(current) == role and workspace == cwd

    first_gateway = FakeGateway(
        ['{"decision":"needs_user","summary":"Need a target","risk":"medium","priority":0,"questions":["Which target?"]}']
    )
    first = Scheduler(
        workflow,
        gateway=first_gateway,
        workspace_resolver=lambda _task: cwd,
        access_policy=policy,
        poll_seconds=0.01,
    )
    assert await first.run_once() is True
    task = workflow.get_task(task["id"])
    assert task["state"] == "WAITING_FOR_USER"
    session = workflow.get_session(task["id"], "triage")
    assert session is not None
    saved_session_id = session["session_id"]
    assert json.loads(session["config_json"]) == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
        "mode": "read-only",
    }
    await first.stop()

    workflow.submit_user_message(task["id"], principal="github:alice", content="Use target one")
    second_gateway = FakeGateway(
        ['{"decision":"ready","summary":"Ready","risk":"low","priority":5,"questions":[]}']
    )
    second = Scheduler(
        workflow,
        gateway=second_gateway,
        workspace_resolver=lambda _task: cwd,
        access_policy=policy,
        poll_seconds=0.01,
    )
    assert await second.run_once() is True
    assert second_gateway.loaded_ids == [saved_session_id]
    assert second_gateway.prompts[0][0] == saved_session_id
    assert '"content": "Use target one"' in second_gateway.prompts[0][1]
    assert workflow.get_task(task["id"])["state"] == "PLAN"
    assert len(authorization_checks) == 4
    events = workflow.list_events(task["id"], principal="github:alice")
    assert any(event["event_type"] == "user_answered" for event in events)
    await second.stop()


@run_async_test
async def test_scheduler_does_not_prompt_without_separate_access_policy(tmp_path: Path, workflow: WorkflowService):
    task = admitted(workflow)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    gateway = FakeGateway(['{"decision":"ready"}'])
    scheduler = Scheduler(
        workflow,
        gateway=gateway,
        workspace_resolver=lambda _task: cwd,
    )
    assert await scheduler.run_once() is False
    assert gateway.prompts == []
    assert workflow.get_session(task["id"], "triage") is None
    await scheduler.stop()


@run_async_test
async def test_async_commit_reader_is_authoritative_over_agent_claim(tmp_path: Path, workflow: WorkflowService):
    task = admitted(workflow)
    workflow.set_triage(task["id"], summary="Ready", priority=1, risk="low")
    workflow.submit_plan(task["id"], plan="Change and test")
    current = workflow.get_task(task["id"])
    workflow.review_plan(task["id"], plan_version=current["plan_version"], decision="approve")
    workflow.approve_plan(
        task["id"], principal="github:alice", plan_version=current["plan_version"], approved=True
    )
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    measured = "a" * 40
    reader_calls: list[int] = []

    async def commit_reader(current_task: dict, workspace: Path) -> dict:
        reader_calls.append(current_task["id"])
        assert workspace == cwd
        return {"head_sha": measured}

    gateway = FakeGateway(
        ['{"decision":"done","summary":"implemented","head_sha":"' + "b" * 40 + '"}']
    )
    scheduler = Scheduler(
        workflow,
        gateway=gateway,
        workspace_resolver=lambda _task: cwd,
        commit_reader=commit_reader,
        access_policy=lambda _task, role, _session, _cwd: role == "implementer",
    )
    assert await scheduler.run_once() is True
    updated = workflow.get_task(task["id"])
    assert updated["head_sha"] == measured
    assert updated["state"] == "CODE_REVIEW"
    assert reader_calls == [task["id"]]
    await scheduler.stop()
