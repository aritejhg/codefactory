from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from codefactory.app import create_app
from codefactory.github import GitHubConfig


TOKEN = "test-bearer-token-0001"
OTHER_TOKEN = "test-bearer-token-0002"


def task_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repository": "acme/widget",
        "issue_number": 17,
        "title": "Add a useful feature",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(
        create_app(
            db_path=tmp_path / "factory.sqlite3",
            auth_tokens={TOKEN: "github:alice", OTHER_TOKEN: "github:bob"},
        )
    ) as test_client:
        yield test_client


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/tasks", json=task_payload(**overrides), headers=auth())
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": ""},
        {"repository": "acme"},
        {"repository": "/widget"},
        {"repository": "acme/"},
        {"repository": "acme /widget"},
        {"issue_number": 0},
        {"issue_number": -1},
        {"title": " \n "},
        {"trusted": True},
    ],
)
def test_create_rejects_invalid_or_trust_bypassing_fields(client: TestClient, overrides: dict[str, Any]):
    response = client.post("/tasks", json=task_payload(**overrides), headers=auth())
    assert response.status_code == 422


def test_health_is_public_but_task_routes_require_auth_and_fail_closed_when_unconfigured(tmp_path: Path):
    with TestClient(create_app(db_path=tmp_path / "no-auth.sqlite3", auth_tokens={})) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/tasks").status_code == 503
        assert client.post("/tasks", json=task_payload()).status_code == 503

    with TestClient(create_app(db_path=tmp_path / "auth.sqlite3", auth_tokens={TOKEN: "github:alice"})) as client:
        assert client.get("/tasks").status_code == 401
        assert client.get("/tasks", headers=auth("invalid-token-000000")).status_code == 401


def test_auth_token_file_must_be_user_owned_and_private(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({TOKEN: "github:alice"}), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        create_app(db_path=tmp_path / "factory.sqlite3", auth_tokens_file=path)


def test_manual_tasks_are_untrusted_idempotent_and_owned(client: TestClient):
    first = create(client)
    assert first["trusted"] is False
    assert first["owner_principal"] == "github:alice"
    duplicate = client.post("/tasks", json=task_payload(), headers=auth())
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == first["id"]
    assert client.get("/tasks", headers=auth()).json()[0]["id"] == first["id"]
    assert client.get(f"/tasks/{first['id']}", headers=auth(OTHER_TOKEN)).status_code == 404
    assert client.get(f"/tasks/{first['id']}/events", headers=auth(OTHER_TOKEN)).status_code == 404
    assert client.get(f"/tasks/{first['id']}/messages", headers=auth(OTHER_TOKEN)).status_code == 404


def test_no_generic_transition_endpoint_and_approvals_are_explicit(client: TestClient):
    task = create(client)
    assert client.post(
        f"/tasks/{task['id']}/transition",
        json={"state": "BUILD", "expected_state": "TRIAGE"},
        headers=auth(),
    ).status_code == 404
    assert client.post(
        f"/tasks/{task['id']}/approve-plan",
        json={"plan_version": 1, "approved": True},
        headers=auth(),
    ).status_code == 409


def test_signed_github_issue_admission_is_allowlisted_labeled_owned_and_deduplicated(tmp_path: Path):
    secret = "webhook-test-secret"
    config = GitHubConfig(
        allowed_repositories=frozenset({"acme/widget"}),
        admission_label="factory:ready",
        webhook_secret=secret,
    )
    with TestClient(
        create_app(
            db_path=tmp_path / "github.sqlite3",
            auth_tokens={TOKEN: "github:alice"},
            github_config=config,
        )
    ) as client:
        delivery_id = "6ef96ed8-0c6a-4b09-9532-fba1c3a9449b"
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/widget"},
            "issue": {
                "number": 44,
                "title": "A labeled issue",
                "body": "Issue body",
                "labels": [{"name": "factory:ready"}],
                "user": {"login": "Alice"},
            },
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers = {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery_id,
        }
        first = client.post("/webhooks/github", content=raw, headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["accepted"] is True
        task = first.json()["task"]
        assert task["trusted"] is True
        assert task["owner_principal"] == "github:alice"
        replay = client.post("/webhooks/github", content=raw, headers=headers)
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True
        assert [item["id"] for item in client.get("/tasks", headers=auth()).json()] == [task["id"]]
        owner_client_task = client.get(f"/tasks/{task['id']}", headers=auth())
        assert owner_client_task.status_code == 200
        assert len(client.get(f"/tasks/{task['id']}/events", headers=auth()).json()) == 1


def test_webhook_signature_is_verified_and_invalid_delivery_is_not_admitted(tmp_path: Path):
    config = GitHubConfig(
        allowed_repositories=frozenset({"acme/widget"}),
        webhook_secret="secret",
    )
    with TestClient(create_app(db_path=tmp_path / "github.sqlite3", auth_tokens={TOKEN: "github:alice"}, github_config=config)) as client:
        raw = b'{"action":"opened"}'
        response = client.post(
            "/webhooks/github",
            content=raw,
            headers={
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": str(UUID(int=1)),
            },
        )
        assert response.status_code == 401
        assert client.get("/tasks", headers=auth()).json() == []


def test_user_message_requires_owner_and_is_persisted(client: TestClient):
    task = create(client)
    response = client.post(
        f"/tasks/{task['id']}/messages",
        json={"content": "Please proceed"},
        headers=auth(OTHER_TOKEN),
    )
    assert response.status_code == 404
    response = client.post(
        f"/tasks/{task['id']}/messages",
        json={"content": "Please proceed"},
        headers=auth(),
    )
    assert response.status_code == 202  # queued, but trusted admission is still required to run it


def test_cancel_is_owner_scoped_and_persisted(client: TestClient):
    task = create(client)
    assert client.post(f"/tasks/{task['id']}/cancel", headers=auth(OTHER_TOKEN)).status_code == 404
    cancelled = client.post(f"/tasks/{task['id']}/cancel", headers=auth())
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    events = client.get(f"/tasks/{task['id']}/events", headers=auth()).json()
    assert events[-1]["event_type"] == "task_cancelled"
