from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from codefactory.app import create_app


def task_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repository": "acme/widget",
        "issue_number": 17,
        "title": "Add a useful feature",
    }
    payload.update(overrides)
    return payload


def admit(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/tasks", json=task_payload(**overrides))
    assert response.status_code in (200, 201), response.text
    return response.json()


def transition(
    client: TestClient,
    task_id: Any,
    state: str,
    expected_state: str,
    *,
    expected_revision: int | None = None,
    request_id: str | None = None,
) -> Any:
    if expected_revision is None:
        current = client.get(f"/tasks/{task_id}")
        expected_revision = current.json().get("revision", 0)
    return client.post(
        f"/tasks/{task_id}/transition",
        json={"state": state, "expected_state": expected_state,
              "expected_revision": expected_revision, "request_id": request_id or str(uuid.uuid4())},
    )


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(db_path=tmp_path / "factory.sqlite3")) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": ""},
        {"repository": "acme"},
        {"repository": "/widget"},
        {"repository": "acme/"},
        {"issue_number": 0},
        {"issue_number": -1},
        {"issue_number": 2**63},
        {"title": " \n "},
        {"title": "\x00title"},
    ],
)
def test_create_rejects_invalid_task_fields(client: TestClient, overrides: dict[str, Any]):
    response = client.post("/tasks", json=task_payload(**overrides))

    assert response.status_code == 422
    assert client.get("/tasks").json() == []


def test_admission_is_idempotent_and_survives_app_recreation(tmp_path: Path):
    db_path = tmp_path / "factory.sqlite3"
    with TestClient(create_app(db_path=db_path)) as client:
        created = admit(client, trusted=True)
        assert created["trusted"] is True
        assert created["state"] == "TRIAGE"

        advanced = transition(client, created["id"], "PLAN", "TRIAGE", expected_revision=0, request_id="advance")
        assert advanced.status_code == 200
        saved = advanced.json()

        duplicate = client.post("/tasks", json=task_payload(title="Conflicting title", trusted=False))
        assert duplicate.status_code == 200
        assert duplicate.json() == saved
        untrusted = admit(client, issue_number=18)

        first_events = client.get(f"/tasks/{created['id']}/events")
        assert first_events.status_code == 200
        assert [event["event_type"] for event in first_events.json()] == [
            "created",
            "transitioned",
        ]
        assert [row["id"] for row in client.get("/tasks").json()] == [created["id"], untrusted["id"]]

    with TestClient(create_app(db_path=db_path)) as restarted_client:
        fetched = restarted_client.get(f"/tasks/{created['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == saved

        duplicate_after_restart = restarted_client.post(
            "/tasks", json=task_payload(title="Changed after restart", trusted=False)
        )
        assert duplicate_after_restart.status_code == 200
        assert duplicate_after_restart.json() == saved
        replay = transition(restarted_client, created["id"], "PLAN", "TRIAGE", expected_revision=0, request_id="advance")
        assert replay.json() == saved
        assert len(restarted_client.get(f"/tasks/{created['id']}/events").json()) == 2
        duplicate_untrusted = restarted_client.post("/tasks", json=task_payload(issue_number=18, title="Upgrade trust", trusted=True))
        assert duplicate_untrusted.json() == untrusted
        assert transition(restarted_client, untrusted["id"], "PLAN", "TRIAGE").status_code == 409


def test_untrusted_task_cannot_advance_and_rejections_do_not_add_events(client: TestClient):
    task = admit(client)
    assert task["trusted"] is False
    task_id = task["id"]

    blocked = transition(client, task_id, "PLAN", "TRIAGE")
    assert blocked.status_code == 409
    assert client.get(f"/tasks/{task_id}").json()["state"] == "TRIAGE"
    events = client.get(f"/tasks/{task_id}/events").json()
    assert [event["event_type"] for event in events] == ["created"]


def test_transition_replays_are_idempotent_and_stale_or_invalid_moves_are_atomic(
    client: TestClient,
):
    task = admit(client, trusted=True)
    task_id = task["id"]

    first = transition(client, task_id, "PLAN", "TRIAGE", expected_revision=0, request_id="first")
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "PLAN"
    event_count = len(client.get(f"/tasks/{task_id}/events").json())
    assert event_count == 2

    replay = transition(client, task_id, "PLAN", "TRIAGE", expected_revision=0, request_id="first")
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    same_state = transition(client, task_id, "PLAN", "PLAN")
    assert same_state.status_code == 409, same_state.text
    assert len(client.get(f"/tasks/{task_id}/events").json()) == event_count
    stale = transition(client, task_id, "BUILD", "TRIAGE")
    invalid_edge = transition(client, task_id, "CI", "PLAN")
    assert stale.status_code == 409
    assert invalid_edge.status_code == 409
    assert client.get(f"/tasks/{task_id}").json()["state"] == "PLAN"
    assert len(client.get(f"/tasks/{task_id}/events").json()) == event_count

    review = transition(client, task_id, "PLAN_REVIEW", "PLAN", expected_revision=1, request_id="review")
    assert review.status_code == 200
    revised = transition(client, task_id, "PLAN", "PLAN_REVIEW").json()
    assert revised["revision"] == 3
    delayed = transition(client, task_id, "PLAN_REVIEW", "PLAN", expected_revision=1, request_id="review")
    assert delayed.json() == review.json()
    assert transition(client, task_id, "PLAN_REVIEW", "PLAN", expected_revision=1).status_code == 409
    assert transition(client, task_id, "CI", "PLAN", expected_revision=1, request_id="review").status_code == 409
    assert client.get(f"/tasks/{task_id}").json() == revised
    assert len(client.get(f"/tasks/{task_id}/events").json()) == 4


@pytest.mark.parametrize("change", [{"expected_state": None}, {"state": "UNKNOWN"}, {"expected_state": "UNKNOWN"}, {"expected_revision": None}, {"request_id": None}])
def test_transition_rejects_missing_or_invalid_preconditions(client: TestClient, change):
    task = admit(client, trusted=True)
    payload = {"state": "PLAN", "expected_state": "TRIAGE", "expected_revision": 0, "request_id": "invalid"}
    for key, value in change.items():
        if value is None:
            payload.pop(key)
        else:
            payload[key] = value
    assert client.post(f"/tasks/{task['id']}/transition", json=payload).status_code == 422
    assert client.get(f"/tasks/{task['id']}").json() == task
    assert len(client.get(f"/tasks/{task['id']}/events").json()) == 1


def test_oversized_task_ids_are_rejected_before_sqlite(client: TestClient):
    task_id = 2**63
    assert client.get(f"/tasks/{task_id}").status_code == 422
    assert client.get(f"/tasks/{task_id}/events").status_code == 422
    assert transition(client, task_id, "PLAN", "TRIAGE", expected_revision=0).status_code == 422


@pytest.mark.parametrize("operation", ["creation", "transition"])
def test_failed_event_write_rolls_back_and_same_request_can_retry(tmp_path: Path, operation):
    db_path = tmp_path / "rollback.sqlite3"
    with TestClient(create_app(db_path=db_path), raise_server_exceptions=False) as client:
        task = admit(client, trusted=True) if operation == "transition" else None
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'forced event failure'); END")

        def request():
            if task:
                return transition(client, task["id"], "PLAN", "TRIAGE", expected_revision=0, request_id="retry")
            return client.post("/tasks", json=task_payload(trusted=True))

        assert request().status_code == 500
        assert client.get("/tasks").json() == ([task] if task else [])
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == (1 if task else 0)
            assert db.execute("SELECT COUNT(*) FROM transition_requests").fetchone()[0] == 0
            db.execute("DROP TRIGGER reject_event")
        assert request().status_code == (200 if task else 201)
        saved = client.get("/tasks").json()[0]
        assert len(client.get(f"/tasks/{saved['id']}/events").json()) == (2 if task else 1)

def test_trusted_workflow_and_review_loops_are_recorded_in_order(client: TestClient):
    task = admit(client, trusted=True)
    task_id = task["id"]
    states = [
        "PLAN",
        "PLAN_REVIEW",
        "PLAN",  # planner revises after review
        "PLAN_REVIEW",
        "HUMAN_PLAN_APPROVAL",
        "BUILD",
        "CODE_REVIEW",
        "BUILD",  # implementer fixes review findings
        "CODE_REVIEW",
        "CI",
    ]
    current = "TRIAGE"
    for next_state in states:
        response = transition(client, task_id, next_state, current)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == next_state
        current = next_state

    assert client.get(f"/tasks/{task_id}").json()["state"] == "CI"
    events = client.get(f"/tasks/{task_id}/events").json()
    assert [(event["from_state"], event["to_state"]) for event in events] == [
        (None, "TRIAGE"),
        *list(zip(["TRIAGE", *states[:-1]], states)),
    ]
    assert [event["event_type"] for event in events] == ["created"] + [
        "transitioned"
    ] * len(states)


def test_missing_task_reads_and_transitions_return_404(client: TestClient):
    task = admit(client)
    missing_id = task["id"] + 1_000_000 if isinstance(task["id"], int) else "missing-task"

    assert client.get(f"/tasks/{missing_id}").status_code == 404
    assert client.get(f"/tasks/{missing_id}/events").status_code == 404
    assert transition(client, missing_id, "PLAN", "TRIAGE").status_code == 404


def test_concurrent_admission_and_transition_across_app_instances_are_idempotent(
    tmp_path: Path,
):
    db_path = tmp_path / "shared.sqlite3"
    with (
        TestClient(create_app(db_path=db_path)) as first_client,
        TestClient(create_app(db_path=db_path)) as second_client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        clients = [first_client, second_client]
        create_futures = [
            executor.submit(client.post, "/tasks", json=task_payload()) for client in clients
        ]
        create_responses = [future.result() for future in create_futures]
        assert sorted(response.status_code for response in create_responses) == [200, 201]
        created_ids = {response.json()["id"] for response in create_responses}
        assert len(created_ids) == 1
        task_id = created_ids.pop()
        assert len(first_client.get(f"/tasks/{task_id}/events").json()) == 1

        trusted_payload = task_payload(issue_number=18, trusted=True)
        trusted_task = first_client.post("/tasks", json=trusted_payload).json()
        transition_futures = [
            executor.submit(transition, client, trusted_task["id"], "PLAN", "TRIAGE", expected_revision=0, request_id="concurrent")
            for client in clients
        ]
        transition_responses = [future.result() for future in transition_futures]
        assert [response.status_code for response in transition_responses] == [200, 200]
        assert all(response.json()["state"] == "PLAN" for response in transition_responses)
        events = first_client.get(f"/tasks/{trusted_task['id']}/events").json()
        assert [(event["from_state"], event["to_state"]) for event in events] == [
            (None, "TRIAGE"),
            ("TRIAGE", "PLAN"),
        ]
