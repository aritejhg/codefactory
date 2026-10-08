from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codefactory.github import CIEvidence, CommitStatus, MergeResult
from codefactory.store import Store
from codefactory.workflow import TaskState, WorkflowError, WorkflowService, role_for_state


SHA1 = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
def workflow(tmp_path: Path):
    store = Store(tmp_path / "workflow.sqlite3")
    service = WorkflowService(store)
    try:
        yield service
    finally:
        store.close()


def admitted(workflow: WorkflowService, issue_number: int = 17) -> dict:
    issue = SimpleNamespace(
        repository="acme/widget",
        number=issue_number,
        title="Implement a feature",
        body="Acceptance criteria",
        labels=("factory:ready",),
    )
    return workflow.admit_github_issue(
        issue,
        owner_principal="github:alice",
        repository_allowed=True,
    )


def plan_approved(workflow: WorkflowService, task_id: int) -> dict:
    workflow.set_triage(task_id, summary="Ready", priority=10, risk="low")
    workflow.submit_plan(task_id, plan="1. Implement; 2. test")
    task = workflow.get_task(task_id)
    workflow.review_plan(task_id, plan_version=task["plan_version"], decision="approve")
    return workflow.approve_plan(
        task_id,
        principal="github:alice",
        plan_version=task["plan_version"],
        approved=True,
    )


def test_manual_admission_is_untrusted_and_github_admission_requires_allowlist_and_label(workflow: WorkflowService):
    manual = workflow.create_manual_task(
        repository="acme/widget",
        issue_number=20,
        title="Manual task",
        owner_principal="github:alice",
    )
    assert manual["trusted"] is False
    assert role_for_state(manual) == "triage"
    with pytest.raises(WorkflowError, match="allowlisted"):
        workflow.admit_github_issue(
            SimpleNamespace(repository="acme/widget", number=21, title="x", body=None, labels=("factory:ready",)),
            owner_principal="github:alice",
            repository_allowed=False,
        )
    with pytest.raises(WorkflowError, match="admission label"):
        workflow.admit_github_issue(
            SimpleNamespace(repository="acme/widget", number=21, title="x", body=None, labels=()),
            owner_principal="github:alice",
            repository_allowed=True,
        )


def test_github_admission_and_delivery_dedup_are_durable_and_atomic(workflow: WorkflowService):
    issue = SimpleNamespace(
        repository="acme/widget", number=21, title="Issue", body=None, labels=("factory:ready",)
    )
    first = workflow.admit_github_issue(
        issue,
        owner_principal="github:alice",
        repository_allowed=True,
        idempotency_key="delivery-1",
        delivery_id="delivery-1",
    )
    duplicate = workflow.admit_github_issue(
        issue,
        owner_principal="github:alice",
        repository_allowed=True,
        idempotency_key="delivery-1",
        delivery_id="delivery-1",
    )
    assert first["trusted"] is True
    assert duplicate is None
    assert len(workflow.list_events(first["id"], principal="github:alice")) == 1


def test_plan_approval_is_exact_version_and_code_replan_invalidates_it(workflow: WorkflowService):
    task = plan_approved(workflow, admitted(workflow)["id"])
    assert task["state"] == TaskState.BUILD.value
    assert task["approved_plan_version"] == 1
    workflow.record_commit(task["id"], head_sha=SHA1)
    workflow.review_code(
        task["id"], head_sha=SHA1, decision="replan", findings=["Scope needs a decision"]
    )
    task = workflow.get_task(task["id"])
    assert task["state"] == TaskState.PLAN.value
    assert task["approved_plan_version"] is None

    workflow.submit_plan(task["id"], plan="Revised plan")
    task = workflow.get_task(task["id"])
    assert task["plan_version"] == 2
    workflow.review_plan(task["id"], plan_version=2, decision="approve")
    with pytest.raises(WorkflowError, match="stale or unreviewed"):
        workflow.approve_plan(
            task["id"], principal="github:alice", plan_version=1, approved=True
        )
    current = workflow.approve_plan(
        task["id"], principal="github:alice", plan_version=2, approved=True
    )
    assert current["approved_plan_version"] == 2


def test_new_commit_invalidates_review_ci_preview_and_pr_approval(workflow: WorkflowService):
    task = plan_approved(workflow, admitted(workflow)["id"])
    task_id = task["id"]
    workflow.record_commit(task_id, head_sha=SHA1)
    workflow.record_pull_request(task_id, number=101, url="https://github.com/acme/widget/pull/101", head_sha=SHA1)
    workflow.review_code(task_id, head_sha=SHA1, decision="approve")
    evidence = CIEvidence(
        "acme/widget", 101, SHA1, CommitStatus("success", (("ci", "success"),)), ()
    )
    workflow.accept_ci_evidence(task_id, evidence)
    workflow.record_preview(
        task_id,
        preview_id="preview-1",
        preview_url="https://preview.example.test",
        head_sha=SHA1,
        healthy=True,
    )
    workflow.request_pr_approval(task_id)
    workflow.approve_pull_request(
        task_id, principal="github:alice", head_sha=SHA1, approved=True
    )

    changed = workflow.record_commit(task_id, head_sha=SHA2)
    assert changed["state"] == TaskState.CODE_REVIEW.value
    assert changed["reviewed_sha"] is None
    assert changed["ci_sha"] is None
    assert changed["preview_sha"] is None
    assert changed["preview_healthy"] is False
    with workflow.store._lock:
        approvals = workflow.store.db.execute(
            "SELECT COUNT(*) FROM approvals WHERE task_id = ? AND approval_type = 'pull_request'",
            (task_id,),
        ).fetchone()[0]
    assert approvals == 0


def test_ci_pending_does_not_dispatch_repair_and_failure_has_bounded_babysitter(workflow: WorkflowService):
    task = plan_approved(workflow, admitted(workflow)["id"])
    task_id = task["id"]
    workflow.record_commit(task_id, head_sha=SHA1)
    workflow.review_code(task_id, head_sha=SHA1, decision="approve")
    pending = CIEvidence("acme/widget", 102, SHA1, CommitStatus("pending", ()), ())
    result = workflow.accept_ci_evidence(task_id, pending)
    assert result["state"] == TaskState.CI.value
    assert result["ci_status"] == "pending"
    assert role_for_state(result) is None

    failed = CIEvidence("acme/widget", 102, SHA1, CommitStatus("failure", (("ci", "failure"),)), ())
    result = workflow.accept_ci_evidence(task_id, failed)
    assert result["state"] == TaskState.CI.value
    assert result["ci_status"] == "failure"
    assert role_for_state(result) == "ci_babysitter"
    result = workflow.record_ci_babysitter_result(task_id, action="no_safe_fix", summary="Do not guess")
    assert result["state"] == TaskState.BLOCKED.value


def test_pr_merge_requires_exact_owner_approval_and_github_confirmation(workflow: WorkflowService):
    task = plan_approved(workflow, admitted(workflow)["id"])
    task_id = task["id"]
    workflow.record_commit(task_id, head_sha=SHA1)
    workflow.record_pull_request(task_id, number=33, url="https://github.com/acme/widget/pull/33", head_sha=SHA1)
    workflow.review_code(task_id, head_sha=SHA1, decision="approve")
    workflow.accept_ci_evidence(
        task_id,
        CIEvidence("acme/widget", 33, SHA1, CommitStatus("success", (("ci", "success"),)), ()),
    )
    workflow.record_preview(
        task_id, preview_id="preview", preview_url="https://preview.example", head_sha=SHA1, healthy=True
    )
    workflow.request_pr_approval(task_id)
    with pytest.raises(WorkflowError, match="no human PR approval"):
        workflow.merge_pull_request(task_id, principal="github:alice", github=SimpleNamespace())
    workflow.approve_pull_request(task_id, principal="github:alice", head_sha=SHA1, approved=True)

    class GitHub:
        def __init__(self):
            self.expected = None

        def merge_pull_request(self, repository: str, number: int, *, expected_head_sha: str):
            self.expected = expected_head_sha
            return MergeResult(True, "merged", "c" * 40)

        def get_pull_request(self, repository: str, number: int):
            return SimpleNamespace(merged=True, head_sha=SHA1)

    github = GitHub()
    merged = workflow.merge_pull_request(task_id, principal="github:alice", github=github)
    assert github.expected == SHA1
    assert merged["state"] == TaskState.MERGED.value


def test_waiting_for_user_restores_state_and_releases_role(workflow: WorkflowService):
    task = admitted(workflow)
    task_id = task["id"]
    key = workflow.claim_turn(task_id, "triage", worker_id="worker-1", lease_seconds=60, max_active=2)
    assert key is not None
    waiting = workflow.wait_for_user(task_id, role="triage", question="Which target?")
    assert waiting["state"] == TaskState.WAITING_FOR_USER.value
    workflow.finish_turn(key, status="completed")
    assert workflow.claim_turn(task_id, "triage", worker_id="worker-1", lease_seconds=60, max_active=2) is None
    queued = workflow.submit_user_message(task_id, principal="github:alice", content="Target one")
    assert queued["status"] == "queued"
    resumed = workflow.get_task(task_id)
    assert resumed["state"] == TaskState.TRIAGE.value
    assert resumed["waiting_role"] is None
    assert workflow.queued_messages(task_id, "triage")[-1]["content"] == "Target one"


def test_expired_turn_lease_recovers_and_allows_a_new_claim(workflow: WorkflowService):
    task_id = admitted(workflow)["id"]
    first = workflow.claim_turn(task_id, "triage", worker_id="worker-1", lease_seconds=60, max_active=2)
    assert first is not None
    with workflow.store.transaction() as db:
        db.execute("UPDATE agent_turns SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE idempotency_key = ?", (first,))
    assert workflow.recover_expired_turns() == 1
    second = workflow.claim_turn(task_id, "triage", worker_id="worker-2", lease_seconds=60, max_active=2)
    assert second is not None and second != first
