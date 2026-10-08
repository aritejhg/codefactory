from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

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
) -> Any:
    return client.post(
        f"/tasks/{task_id}/transition",
        json={"state": state, "expected_state": expected_state},
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
        {"title": " \n "},
    ],
)
def test_create_rejects_invalid_task_fields(client: TestClient, overrides: dict[str, Any]):
    response = client.post("/tasks", json=task_payload(**overrides))

    assert response.status_code == 422


def test_admission_is_idempotent_and_survives_app_recreation(tmp_path: Path):
    db_path = tmp_path / "factory.sqlite3"
    with TestClient(create_app(db_path=db_path)) as client:
        created = admit(client, trusted=True)
        assert created["trusted"] is True
        assert created["state"] == "TRIAGE"

        advanced = transition(client, created["id"], "PLAN", "TRIAGE")
        assert advanced.status_code == 200

        duplicate = client.post("/tasks", json=task_payload(trusted=True))
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == created["id"]

        first_events = client.get(f"/tasks/{created['id']}/events")
        assert first_events.status_code == 200
        assert [event["event_type"] for event in first_events.json()] == [
            "created",
            "transitioned",
        ]
        assert [row["id"] for row in client.get("/tasks").json()] == [created["id"]]

    with TestClient(create_app(db_path=db_path)) as restarted_client:
        fetched = restarted_client.get(f"/tasks/{created['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["state"] == "PLAN"

        duplicate_after_restart = restarted_client.post(
            "/tasks", json=task_payload(trusted=True)
        )
        assert duplicate_after_restart.status_code == 200
        assert duplicate_after_restart.json()["id"] == created["id"]
        assert len(restarted_client.get(f"/tasks/{created['id']}/events").json()) == 2


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

    first = transition(client, task_id, "PLAN", "TRIAGE")
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "PLAN"
    event_count = len(client.get(f"/tasks/{task_id}/events").json())
    assert event_count == 2

    # Replaying the committed request and requesting the current state are both no-ops.
    replay = transition(client, task_id, "PLAN", "TRIAGE")
    assert replay.status_code == 200, replay.text
    same_state = transition(client, task_id, "PLAN", "PLAN")
    assert same_state.status_code == 200, same_state.text
    assert len(client.get(f"/tasks/{task_id}/events").json()) == event_count

    stale = transition(client, task_id, "BUILD", "TRIAGE")
    invalid_edge = transition(client, task_id, "CI", "PLAN")
    assert stale.status_code == 409
    assert invalid_edge.status_code == 409
    assert client.get(f"/tasks/{task_id}").json()["state"] == "PLAN"
    assert len(client.get(f"/tasks/{task_id}/events").json()) == event_count


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
            executor.submit(transition, client, trusted_task["id"], "PLAN", "TRIAGE")
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
