"""Deterministic task workflow rules and durable state changes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from .store import Store, json_dumps, utc_now


_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.IGNORECASE)
_ROLE_NAMES = {"triage", "planner", "reviewer", "implementer", "ci_babysitter"}


class TaskState(str, Enum):
    TRIAGE = "TRIAGE"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    PLAN = "PLAN"
    PLAN_REVIEW = "PLAN_REVIEW"
    HUMAN_PLAN_APPROVAL = "HUMAN_PLAN_APPROVAL"
    BUILD = "BUILD"
    CODE_REVIEW = "CODE_REVIEW"
    CI = "CI"
    PREVIEW_READY = "PREVIEW_READY"
    HUMAN_PR_APPROVAL = "HUMAN_PR_APPROVAL"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class WorkflowError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _task(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["trusted"] = bool(result["trusted"])
    result["preview_healthy"] = bool(result["preview_healthy"])
    result["role_failures"] = json.loads(result.pop("role_failures_json", "{}"))
    return result


def _need_task(db: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise WorkflowError("task not found", 404)
    return row


def _append_event(
    db: sqlite3.Connection,
    *,
    task_id: int,
    event_type: str,
    from_state: str | None,
    to_state: str,
    actor: str | None,
    run_id: str | None,
    plan_version: int | None,
    head_sha: str | None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO events
           (task_id, event_type, from_state, to_state, created_at,
            actor_principal, run_id, idempotency_key, plan_version, head_sha,
            payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            event_type,
            from_state,
            to_state,
            utc_now(),
            actor,
            run_id,
            idempotency_key,
            plan_version,
            head_sha,
            json_dumps(dict(payload or {})),
        ),
    )


def _set_state(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    state: TaskState,
    event_type: str,
    actor: str | None,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> sqlite3.Row:
    if idempotency_key:
        prior = db.execute(
            "SELECT task_id FROM events WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if prior:
            if prior["task_id"] != row["id"]:
                raise WorkflowError("idempotency key belongs to another task")
            return _need_task(db, row["id"])
    old_state = row["state"]
    db.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (state.value, utc_now(), row["id"]),
    )
    _append_event(
        db,
        task_id=row["id"],
        event_type=event_type,
        from_state=old_state,
        to_state=state.value,
        actor=actor,
        run_id=row["run_id"],
        plan_version=row["plan_version"],
        head_sha=row["head_sha"],
        payload=payload,
        idempotency_key=idempotency_key,
    )
    return _need_task(db, row["id"])


class WorkflowService:
    """The only application-level authority for task transitions."""

    def __init__(
        self,
        store: Store,
        *,
        max_plan_revisions: int = 2,
        max_review_repairs: int = 3,
        max_ci_fixes: int = 2,
        max_turn_failures: int = 3,
    ):
        if min(max_plan_revisions, max_review_repairs, max_ci_fixes, max_turn_failures) < 0:
            raise ValueError("workflow retry limits must be non-negative")
        self.store = store
        self.max_plan_revisions = max_plan_revisions
        self.max_review_repairs = max_review_repairs
        self.max_ci_fixes = max_ci_fixes
        self.max_turn_failures = max_turn_failures

    def _create_task(
        self,
        *,
        repository: str,
        issue_number: int,
        title: str,
        owner_principal: str,
        trusted: bool,
        issue_body: str | None = None,
        source: str = "manual",
        idempotency_key: str | None = None,
        delivery_id: str | None = None,
        delivery_event: str | None = None,
    ) -> dict[str, Any] | None:
        repository = repository.strip()
        owner_principal = owner_principal.strip()
        title = title.strip()
        if repository.count("/") != 1 or any(not part for part in repository.split("/")):
            raise WorkflowError("repository must use owner/name format", 422)
        if issue_number <= 0 or not title or not owner_principal:
            raise WorkflowError("issue number, title, and owner are required", 422)
        if issue_body is not None and not isinstance(issue_body, str):
            raise WorkflowError("issue body must be text", 422)

        with self.store.transaction() as db:
            if delivery_id is not None:
                delivery = db.execute(
                    "SELECT task_id FROM webhook_deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if delivery is not None:
                    return None
                db.execute(
                    "INSERT INTO webhook_deliveries(delivery_id, event_name, received_at) VALUES (?, ?, ?)",
                    (delivery_id, delivery_event or "issues", utc_now()),
                )
            existing = db.execute(
                "SELECT * FROM tasks WHERE repository = ? AND issue_number = ?",
                (repository, issue_number),
            ).fetchone()
            if existing is not None:
                if existing["owner_principal"] != owner_principal:
                    raise WorkflowError("issue is already owned by another principal", 403)
                # A redelivery cannot upgrade trust or replace the original owner.
                if trusted and not bool(existing["trusted"]):
                    db.execute(
                        "UPDATE tasks SET trusted = 1, updated_at = ? WHERE id = ?",
                        (utc_now(), existing["id"]),
                    )
                    _append_event(
                        db,
                        task_id=existing["id"],
                        event_type="trusted_admission",
                        from_state=existing["state"],
                        to_state=existing["state"],
                        actor=owner_principal,
                        run_id=existing["run_id"],
                        plan_version=existing["plan_version"],
                        head_sha=existing["head_sha"],
                        payload={"source": source},
                        idempotency_key=idempotency_key,
                    )
                if delivery_id is not None:
                    db.execute(
                        "UPDATE webhook_deliveries SET task_id = ? WHERE delivery_id = ?",
                        (existing["id"], delivery_id),
                    )
                return _task(_need_task(db, existing["id"]))

            run_id = str(uuid.uuid4())
            now = utc_now()
            cursor = db.execute(
                """INSERT INTO tasks
                   (repository, issue_number, title, issue_body, trusted, state,
                    owner_principal, run_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    repository,
                    issue_number,
                    title,
                    issue_body,
                    int(trusted),
                    TaskState.TRIAGE.value,
                    owner_principal,
                    run_id,
                    now,
                    now,
                ),
            )
            task_id = cursor.lastrowid
            if delivery_id is not None:
                db.execute(
                    "UPDATE webhook_deliveries SET task_id = ? WHERE delivery_id = ?",
                    (task_id, delivery_id),
                )
            _append_event(
                db,
                task_id=task_id,
                event_type="task_admitted",
                from_state=None,
                to_state=TaskState.TRIAGE.value,
                actor=owner_principal,
                run_id=run_id,
                plan_version=0,
                head_sha=None,
                payload={"source": source, "trusted": bool(trusted)},
                idempotency_key=idempotency_key,
            )
            return _task(_need_task(db, task_id))

    def create_manual_task(
        self,
        *,
        repository: str,
        issue_number: int,
        title: str,
        owner_principal: str,
        issue_body: str | None = None,
    ) -> dict[str, Any]:
        """Create a user-owned task, always untrusted until GitHub admits it."""
        return self._create_task(
            repository=repository,
            issue_number=issue_number,
            title=title,
            owner_principal=owner_principal,
            issue_body=issue_body,
            trusted=False,
        )

    def admit_github_issue(
        self,
        issue: Any,
        *,
        owner_principal: str,
        repository_allowed: bool,
        admission_label: str = "factory:ready",
        idempotency_key: str | None = None,
        delivery_id: str | None = None,
        delivery_event: str = "issues",
    ) -> dict[str, Any] | None:
        """Admit only a signed, explicitly allowlisted, labeled issue."""
        if not repository_allowed:
            raise WorkflowError("repository is not allowlisted for trusted work", 403)
        labels = getattr(issue, "labels", ())
        if not any(
            isinstance(label, str) and label.casefold() == admission_label.casefold()
            for label in labels
        ):
            raise WorkflowError("issue is missing the admission label", 409)
        return self._create_task(
            repository=issue.repository,
            issue_number=issue.number,
            title=issue.title,
            owner_principal=owner_principal,
            issue_body=issue.body,
            trusted=True,
            source="github_webhook",
            idempotency_key=idempotency_key,
            delivery_id=delivery_id,
            delivery_event=delivery_event,
        )

    def get_task_by_issue(self, repository: str, issue_number: int) -> dict[str, Any] | None:
        """Internal lookup used to route verified GitHub comment webhooks."""
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM tasks WHERE repository = ? AND issue_number = ?",
                (repository, issue_number),
            ).fetchone()
        return _task(row) if row is not None else None

    def queue_github_comment(
        self,
        *,
        delivery_id: str,
        event_name: str,
        repository: str,
        issue_number: int,
        author_principal: str,
        content: str,
    ) -> bool:
        """Deduplicate a verified issue-comment delivery and queue owner replies."""
        if not content.strip() or len(content.encode("utf-8")) > 64 * 1024:
            raise WorkflowError("GitHub comment is empty or too large", 422)
        with self.store.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO webhook_deliveries(delivery_id, event_name, received_at) VALUES (?, ?, ?)",
                (delivery_id, event_name, utc_now()),
            )
            if cursor.rowcount != 1:
                return False
            row = db.execute(
                "SELECT * FROM tasks WHERE repository = ? AND issue_number = ?",
                (repository, issue_number),
            ).fetchone()
            if row is None or row["owner_principal"] != author_principal:
                return True
            if row["state"] in {TaskState.MERGED.value, TaskState.BLOCKED.value}:
                return True
            role = row["waiting_role"] or role_for_state(row)
            if role is None:
                return True
            message_id = self._queue_message(
                db, row["id"], role, "user", content.strip(), author_principal
            )
            _append_event(
                db,
                task_id=row["id"],
                event_type="message_queued",
                from_state=row["state"],
                to_state=row["state"],
                actor=author_principal,
                run_id=row["run_id"],
                plan_version=row["plan_version"],
                head_sha=row["head_sha"],
                payload={"message_id": message_id, "role": role, "source": "github_comment"},
            )
            db.execute(
                "UPDATE webhook_deliveries SET task_id = ? WHERE delivery_id = ?",
                (row["id"], delivery_id),
            )
            if row["state"] == TaskState.WAITING_FOR_USER.value:
                target_name = row["waiting_state"] or TaskState.PLAN.value
                try:
                    target = TaskState(target_name)
                except ValueError:
                    raise WorkflowError("stored waiting state is invalid") from None
                db.execute(
                    "UPDATE tasks SET waiting_role = NULL, waiting_state = NULL, updated_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )
                refreshed = _need_task(db, row["id"])
                _set_state(
                    db, refreshed, target, "user_answered", author_principal,
                    payload={"message_id": message_id, "source": "github_comment"},
                )
            return True

    def get_task(self, task_id: int, *, principal: str | None = None) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise WorkflowError("task not found", 404)
        if principal is not None and row["owner_principal"] != principal:
            raise WorkflowError("task not found", 404)
        return _task(row)

    def list_tasks(self, *, principal: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT * FROM tasks WHERE owner_principal = ? ORDER BY priority DESC, id",
                (principal,),
            ).fetchall()
        return [_task(row) for row in rows]

    def list_events(self, task_id: int, *, principal: str) -> list[dict[str, Any]]:
        self.get_task(task_id, principal=principal)
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json", "{}"))
            result.append(item)
        return result

    def list_messages(self, task_id: int, *, principal: str) -> list[dict[str, Any]]:
        self.get_task(task_id, principal=principal)
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT id, task_id, role, direction, content, status, actor_principal, created_at, updated_at FROM messages WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_webhook_delivery(
        self, delivery_id: str, event_name: str, *, task_id: int | None = None
    ) -> bool:
        """Return false for a replayed GitHub delivery ID."""
        if not delivery_id or not event_name:
            raise WorkflowError("invalid webhook delivery", 422)
        with self.store.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO webhook_deliveries
                   (delivery_id, event_name, received_at, task_id)
                   VALUES (?, ?, ?, ?)""",
                (delivery_id, event_name, utc_now(), task_id),
            )
            return cursor.rowcount == 1

    def set_triage(
        self,
        task_id: int,
        *,
        summary: str,
        priority: int,
        risk: str,
        questions: list[str] | None = None,
    ) -> dict[str, Any]:
        questions = self._questions(questions)
        if not isinstance(summary, str) or not summary.strip():
            raise WorkflowError("triage summary is required", 422)
        if not isinstance(priority, int) or isinstance(priority, bool) or not -100 <= priority <= 100:
            raise WorkflowError("priority must be between -100 and 100", 422)
        if risk not in {"low", "medium", "high"}:
            raise WorkflowError("risk must be low, medium, or high", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.TRIAGE)
            self._require_trusted(row)
            target = TaskState.WAITING_FOR_USER if questions else TaskState.PLAN
            self._audit_questions(db, row, "triage", questions)
            db.execute("UPDATE tasks SET priority = ?, updated_at = ? WHERE id = ?", (priority, utc_now(), task_id))
            return _task(
                _set_state(
                    db,
                    row,
                    target,
                    "triage_complete" if not questions else "user_question",
                    "agent:triage",
                    payload={"priority": priority, "risk": risk, "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(), "questions": questions},
                )
            )

    def submit_plan(
        self,
        task_id: int,
        *,
        plan: str,
        questions: list[str] | None = None,
    ) -> dict[str, Any]:
        questions = self._questions(questions)
        if not isinstance(plan, str) or not plan.strip():
            raise WorkflowError("plan must not be blank", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.PLAN)
            self._require_trusted(row)
            plan_version = int(row["plan_version"]) + 1
            if int(row["plan_revisions"]) > self.max_plan_revisions:
                return _task(_set_state(db, row, TaskState.BLOCKED, "plan_revision_limit", "agent:planner"))
            target = TaskState.WAITING_FOR_USER if questions else TaskState.PLAN_REVIEW
            self._audit_questions(db, row, "planner", questions)
            db.execute(
                """UPDATE tasks SET plan_version = ?, plan_text = ?,
                   reviewed_plan_version = NULL, approved_plan_version = NULL,
                   updated_at = ? WHERE id = ?""",
                (plan_version, plan, utc_now(), task_id),
            )
            refreshed = _need_task(db, task_id)
            return _task(
                _set_state(
                    db,
                    refreshed,
                    target,
                    "plan_submitted" if not questions else "user_question",
                    "agent:planner",
                    payload={"plan_version": plan_version, "plan_sha256": hashlib.sha256(plan.encode()).hexdigest(), "questions": questions},
                )
            )

    def review_plan(
        self,
        task_id: int,
        *,
        plan_version: int,
        decision: str,
        findings: list[str] | None = None,
        questions: list[str] | None = None,
    ) -> dict[str, Any]:
        findings = self._findings(findings)
        questions = self._questions(questions)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.PLAN_REVIEW)
            if plan_version != row["plan_version"]:
                raise WorkflowError("plan review is for a stale plan version")
            if decision not in {"approve", "revise", "needs_user"}:
                raise WorkflowError("invalid plan review decision", 422)
            if decision == "needs_user":
                if not questions:
                    raise WorkflowError("reviewer question is required", 422)
                self._audit_questions(db, row, "reviewer", questions)
                return _task(_set_state(db, row, TaskState.WAITING_FOR_USER, "user_question", "agent:reviewer", payload={"questions": questions, "plan_version": plan_version}))
            if decision == "revise":
                revisions = int(row["plan_revisions"]) + 1
                db.execute("UPDATE tasks SET plan_revisions = ?, updated_at = ? WHERE id = ?", (revisions, utc_now(), task_id))
                if revisions > self.max_plan_revisions:
                    return _task(_set_state(db, row, TaskState.BLOCKED, "plan_revision_limit", "agent:reviewer", payload={"findings": findings}))
                self._queue_message(db, task_id, "planner", "agent", "\n".join(findings), "agent:reviewer")
                return _task(_set_state(db, row, TaskState.PLAN, "plan_revision_requested", "agent:reviewer", payload={"findings": findings, "plan_version": plan_version}))
            db.execute("UPDATE tasks SET reviewed_plan_version = ?, updated_at = ? WHERE id = ?", (plan_version, utc_now(), task_id))
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, TaskState.HUMAN_PLAN_APPROVAL, "plan_review_approved", "agent:reviewer", payload={"plan_version": plan_version, "findings": findings}))

    def approve_plan(
        self,
        task_id: int,
        *,
        principal: str,
        plan_version: int,
        approved: bool,
        comment: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            if plan_version != row["plan_version"]:
                raise WorkflowError("approval is for a stale or unreviewed plan version")
            if not bool(row["trusted"]):
                raise WorkflowError("untrusted tasks cannot receive plan approval")
            if not isinstance(approved, bool):
                raise WorkflowError("approved must be a boolean", 422)
            prior = db.execute(
                "SELECT principal, approved FROM approvals WHERE task_id = ? AND approval_type = 'plan' AND plan_version = ?",
                (task_id, plan_version),
            ).fetchone()
            if prior is not None:
                if prior["principal"] != principal or bool(prior["approved"]) != approved:
                    raise WorkflowError("a different decision is already recorded for this plan version")
                if approved and row["approved_plan_version"] != plan_version:
                    raise WorkflowError("plan approval is no longer active")
                # Replays are idempotent because the exact explicit approval
                # record is durable; state text alone is never treated as approval.
                return _task(row)
            self._require_state(row, TaskState.HUMAN_PLAN_APPROVAL)
            if row["reviewed_plan_version"] != plan_version:
                raise WorkflowError("approval is for a stale or unreviewed plan version")
            db.execute(
                """INSERT INTO approvals(task_id, approval_type, principal,
                   plan_version, head_sha, approved, created_at)
                   VALUES (?, 'plan', ?, ?, NULL, ?, ?)""",
                (task_id, principal, plan_version, int(approved), utc_now()),
            )
            if approved:
                db.execute("UPDATE tasks SET approved_plan_version = ?, updated_at = ? WHERE id = ?", (plan_version, utc_now(), task_id))
                refreshed = _need_task(db, task_id)
                return _task(_set_state(db, refreshed, TaskState.BUILD, "plan_human_approved", principal, payload={"plan_version": plan_version, "comment_sha256": self._optional_hash(comment)}))
            revisions = int(row["plan_revisions"]) + 1
            db.execute("UPDATE tasks SET plan_revisions = ?, approved_plan_version = NULL, updated_at = ? WHERE id = ?", (revisions, utc_now(), task_id))
            target = TaskState.PLAN if revisions <= self.max_plan_revisions else TaskState.BLOCKED
            self._queue_message(db, task_id, "planner", "agent", comment or "Human requested a plan revision.", principal)
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, target, "plan_human_rejected", principal, payload={"plan_version": plan_version, "comment_sha256": self._optional_hash(comment)}))

    def record_pull_request(self, task_id: int, *, number: int, url: str, head_sha: str) -> dict[str, Any]:
        self._require_sha(head_sha)
        if number <= 0 or not url.startswith("https://github.com/"):
            raise WorkflowError("invalid pull request reference", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            if row["state"] not in {TaskState.BUILD.value, TaskState.CODE_REVIEW.value, TaskState.CI.value}:
                raise WorkflowError("pull request cannot be attached in the current state")
            if row["head_sha"] is not None and row["head_sha"] != head_sha:
                raise WorkflowError("pull request head does not match the current commit")
            db.execute("UPDATE tasks SET pull_number = ?, pull_url = ?, updated_at = ? WHERE id = ?", (number, url, utc_now(), task_id))
            refreshed = _need_task(db, task_id)
            _append_event(db, task_id=task_id, event_type="pull_request_created", from_state=row["state"], to_state=row["state"], actor="controller", run_id=row["run_id"], plan_version=row["plan_version"], head_sha=head_sha, payload={"pull_number": number})
            return _task(refreshed)

    def record_commit(self, task_id: int, *, head_sha: str, actor: str = "agent:implementer") -> dict[str, Any]:
        self._require_sha(head_sha)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_trusted(row)
            if row["state"] not in {TaskState.BUILD.value, TaskState.CODE_REVIEW.value, TaskState.CI.value, TaskState.PREVIEW_READY.value, TaskState.HUMAN_PR_APPROVAL.value}:
                raise WorkflowError("commit cannot be recorded in the current state")
            if row["approved_plan_version"] != row["plan_version"]:
                raise WorkflowError("implementation does not have current human plan approval")
            if row["head_sha"] == head_sha:
                return _task(row)
            db.execute(
                """UPDATE tasks SET head_sha = ?, reviewed_sha = NULL, ci_sha = NULL,
                   ci_status = NULL, preview_sha = NULL, preview_healthy = 0,
                   preview_id = NULL, preview_url = NULL, updated_at = ? WHERE id = ?""",
                (head_sha, utc_now(), task_id),
            )
            db.execute("DELETE FROM approvals WHERE task_id = ? AND approval_type = 'pull_request'", (task_id,))
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, TaskState.CODE_REVIEW, "commit_recorded", actor, payload={"head_sha": head_sha}))

    def review_code(
        self,
        task_id: int,
        *,
        head_sha: str,
        decision: str,
        findings: list[str] | None = None,
        questions: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_sha(head_sha)
        findings = self._findings(findings)
        questions = self._questions(questions)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.CODE_REVIEW)
            if row["head_sha"] != head_sha:
                raise WorkflowError("code review is for a stale commit")
            if decision not in {"approve", "changes_requested", "replan", "needs_user"}:
                raise WorkflowError("invalid code review decision", 422)
            if decision == "needs_user":
                if not questions:
                    raise WorkflowError("reviewer question is required", 422)
                self._audit_questions(db, row, "reviewer", questions)
                return _task(_set_state(db, row, TaskState.WAITING_FOR_USER, "user_question", "agent:reviewer", payload={"questions": questions, "head_sha": head_sha}))
            if decision == "replan":
                db.execute("UPDATE tasks SET approved_plan_version = NULL, updated_at = ? WHERE id = ?", (utc_now(), task_id))
                self._queue_message(db, task_id, "planner", "agent", "\n".join(findings) or "Material scope change requested by reviewer.", "agent:reviewer")
                refreshed = _need_task(db, task_id)
                return _task(_set_state(db, refreshed, TaskState.PLAN, "replan_requested", "agent:reviewer", payload={"findings": findings, "head_sha": head_sha}))
            if decision == "changes_requested":
                repairs = int(row["review_repairs"]) + 1
                db.execute("UPDATE tasks SET review_repairs = ?, updated_at = ? WHERE id = ?", (repairs, utc_now(), task_id))
                target = TaskState.BUILD if repairs <= self.max_review_repairs else TaskState.BLOCKED
                if target == TaskState.BUILD:
                    self._queue_message(db, task_id, "implementer", "agent", "\n".join(findings), "agent:reviewer")
                refreshed = _need_task(db, task_id)
                return _task(_set_state(db, refreshed, target, "code_changes_requested", "agent:reviewer", payload={"findings": findings, "head_sha": head_sha, "repairs": repairs}))
            db.execute("UPDATE tasks SET reviewed_sha = ?, ci_status = 'pending', ci_sha = NULL, updated_at = ? WHERE id = ?", (head_sha, utc_now(), task_id))
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, TaskState.CI, "code_review_approved", "agent:reviewer", payload={"head_sha": head_sha, "findings": findings}))

    def accept_ci_evidence(self, task_id: int, evidence: Any) -> dict[str, Any]:
        head_sha = getattr(evidence, "head_sha", None)
        successful = getattr(evidence, "successful", None)
        self._require_sha(head_sha)
        if not isinstance(successful, bool):
            raise WorkflowError("CI evidence is not a verified GitHub result", 422)
        combined = getattr(evidence, "combined_status", None)
        combined_state = getattr(combined, "state", None)
        contexts = getattr(combined, "contexts", ())
        check_runs = getattr(evidence, "check_runs", ())
        has_evidence = bool(contexts or check_runs)
        pending = not successful and (
            any(getattr(run, "status", None) != "completed" for run in check_runs)
            or combined_state == "pending"
            or not has_evidence
        )
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.CI)
            if row["head_sha"] != head_sha or row["reviewed_sha"] != head_sha:
                raise WorkflowError("CI evidence is for a stale or unreviewed commit")
            if pending:
                db.execute("UPDATE tasks SET ci_status = 'pending', updated_at = ? WHERE id = ?", (utc_now(), task_id))
                refreshed = _need_task(db, task_id)
                _append_event(db, task_id=task_id, event_type="github_ci_pending", from_state=refreshed["state"], to_state=refreshed["state"], actor="github", run_id=refreshed["run_id"], plan_version=refreshed["plan_version"], head_sha=head_sha, payload={"source": "github_check_runs"})
                return _task(refreshed)
            if successful:
                db.execute("UPDATE tasks SET ci_sha = ?, ci_status = 'success', updated_at = ? WHERE id = ?", (head_sha, utc_now(), task_id))
                refreshed = _need_task(db, task_id)
                _append_event(db, task_id=task_id, event_type="github_ci_passed", from_state=refreshed["state"], to_state=refreshed["state"], actor="github", run_id=refreshed["run_id"], plan_version=refreshed["plan_version"], head_sha=head_sha, payload={"source": "github_check_runs"})
                return _task(refreshed)
            failures = int(row["ci_failures"]) + 1
            target = TaskState.CI if failures <= self.max_ci_fixes else TaskState.BLOCKED
            db.execute("UPDATE tasks SET ci_failures = ?, ci_status = 'failure', ci_sha = ?, updated_at = ? WHERE id = ?", (failures, head_sha, utc_now(), task_id))
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, target, "github_ci_failed", "github", payload={"head_sha": head_sha, "failures": failures}))

    def record_ci_babysitter_result(self, task_id: int, *, action: str, summary: str) -> dict[str, Any]:
        if action not in {"narrow_fix", "delegate_to_implementer", "no_safe_fix"}:
            raise WorkflowError("invalid CI babysitter action", 422)
        reported_action = action
        if action == "narrow_fix":
            action = "delegate_to_implementer"
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.CI)
            if row["ci_status"] != "failure":
                raise WorkflowError("CI babysitter requires a failed current check")
            if action == "no_safe_fix":
                return _task(_set_state(db, row, TaskState.BLOCKED, "ci_babysitter_stopped", "agent:ci_babysitter", payload={"summary_sha256": hashlib.sha256(summary.encode()).hexdigest()}))
            # Both repair paths return to the same writer session. The next
            # measured commit invalidates review, CI, and preview evidence.
            self._queue_message(db, task_id, "implementer", "agent", summary, "agent:ci_babysitter")
            db.execute("UPDATE tasks SET ci_status = 'repair_requested', updated_at = ? WHERE id = ?", (utc_now(), task_id))
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, TaskState.BUILD, "ci_repair_requested", "agent:ci_babysitter", payload={"action": action, "reported_action": reported_action, "policy": "ci_read_only", "head_sha": row["head_sha"]}))

    def record_preview(
        self,
        task_id: int,
        *,
        preview_id: str,
        preview_url: str,
        head_sha: str,
        healthy: bool,
    ) -> dict[str, Any]:
        self._require_sha(head_sha)
        if not preview_id or not preview_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise WorkflowError("invalid preview reference", 422)
        if not isinstance(healthy, bool):
            raise WorkflowError("health result must be boolean", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.CI)
            if row["head_sha"] != head_sha or row["ci_sha"] != head_sha or row["ci_status"] != "success":
                raise WorkflowError("preview must match the current CI-passing commit")
            db.execute("UPDATE tasks SET preview_id = ?, preview_url = ?, preview_sha = ?, preview_healthy = ?, updated_at = ? WHERE id = ?", (preview_id, preview_url, head_sha, int(healthy), utc_now(), task_id))
            refreshed = _need_task(db, task_id)
            target = TaskState.PREVIEW_READY if healthy else TaskState.CI
            return _task(_set_state(db, refreshed, target, "preview_healthy" if healthy else "preview_unhealthy", "workspace", payload={"preview_id": preview_id, "head_sha": head_sha, "healthy": healthy}))

    def request_pr_approval(self, task_id: int) -> dict[str, Any]:
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.PREVIEW_READY)
            if not row["pull_number"] or row["head_sha"] != row["preview_sha"] or not bool(row["preview_healthy"]):
                raise WorkflowError("healthy current-commit preview and GitHub PR are required")
            return _task(_set_state(db, row, TaskState.HUMAN_PR_APPROVAL, "human_pr_approval_requested", "controller", payload={"pull_number": row["pull_number"], "head_sha": row["head_sha"]}))

    def approve_pull_request(
        self,
        task_id: int,
        *,
        principal: str,
        head_sha: str,
        approved: bool,
        comment: str | None = None,
    ) -> dict[str, Any]:
        self._require_sha(head_sha)
        if not isinstance(approved, bool):
            raise WorkflowError("approved must be a boolean", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            prior = db.execute(
                "SELECT principal, approved FROM approvals WHERE task_id = ? AND approval_type = 'pull_request' AND head_sha = ?",
                (task_id, head_sha),
            ).fetchone()
            if prior is not None:
                if prior["principal"] != principal or bool(prior["approved"]) != approved:
                    raise WorkflowError("a different decision is already recorded for this commit")
                if row["head_sha"] != head_sha:
                    raise WorkflowError("PR approval is for a stale commit")
                return _task(row)
            self._require_state(row, TaskState.HUMAN_PR_APPROVAL)
            if row["head_sha"] != head_sha or row["reviewed_sha"] != head_sha or row["ci_sha"] != head_sha or row["preview_sha"] != head_sha or not bool(row["preview_healthy"]):
                raise WorkflowError("PR approval is for a stale or ungated commit")
            if row["pull_number"] is None:
                raise WorkflowError("GitHub pull request is not recorded")
            db.execute(
                """INSERT INTO approvals(task_id, approval_type, principal,
                   plan_version, head_sha, approved, created_at)
                   VALUES (?, 'pull_request', ?, NULL, ?, ?, ?)""",
                (task_id, principal, head_sha, int(approved), utc_now()),
            )
            if approved:
                _append_event(db, task_id=task_id, event_type="pull_request_approved", from_state=row["state"], to_state=row["state"], actor=principal, run_id=row["run_id"], plan_version=row["plan_version"], head_sha=head_sha, payload={"comment_sha256": self._optional_hash(comment)})
                return _task(_need_task(db, task_id))
            db.execute(
                """UPDATE tasks SET reviewed_sha = NULL, ci_sha = NULL, ci_status = NULL,
                   preview_sha = NULL, preview_healthy = 0, preview_id = NULL,
                   preview_url = NULL, updated_at = ? WHERE id = ?""",
                (utc_now(), task_id),
            )
            self._queue_message(
                db,
                task_id,
                "implementer",
                "user",
                comment.strip() if isinstance(comment, str) and comment.strip() else "The owner did not approve this pull request. Please ask for clarification if needed.",
                principal,
            )
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, TaskState.BUILD, "pull_request_rejected", principal, payload={"head_sha": head_sha, "comment_sha256": self._optional_hash(comment)}))

    def merge_pull_request(self, task_id: int, *, principal: str, github: Any) -> dict[str, Any]:
        """Merge using the expected head SHA, then confirm merged state on GitHub."""
        task = self.get_task(task_id, principal=principal)
        if task["state"] == TaskState.MERGED.value:
            return task
        if task["state"] != TaskState.HUMAN_PR_APPROVAL.value:
            raise WorkflowError("task is not waiting for PR approval")
        if not task["pull_number"] or not task["head_sha"]:
            raise WorkflowError("GitHub pull request is incomplete")
        with self.store._lock:
            approval = self.store.db.execute(
                """SELECT 1 FROM approvals WHERE task_id = ? AND approval_type = 'pull_request'
                   AND principal = ? AND head_sha = ? AND approved = 1""",
                (task_id, principal, task["head_sha"]),
            ).fetchone()
        if approval is None:
            raise WorkflowError("current commit has no human PR approval")
        repository = task["repository"]
        pull_number = task["pull_number"]
        head_sha = task["head_sha"]
        result = github.merge_pull_request(repository, pull_number, expected_head_sha=head_sha)
        if not getattr(result, "merged", False):
            raise WorkflowError("GitHub did not merge the pull request")
        pull = github.get_pull_request(repository, pull_number)
        if not getattr(pull, "merged", False) or getattr(pull, "head_sha", None) != head_sha:
            raise WorkflowError("GitHub merge could not be confirmed for the approved commit")
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            self._require_state(row, TaskState.HUMAN_PR_APPROVAL)
            if row["head_sha"] != head_sha:
                raise WorkflowError("commit changed while merge confirmation was in flight")
            return _task(_set_state(db, row, TaskState.MERGED, "github_merge_confirmed", principal, payload={"pull_number": pull_number, "merge_commit_sha": getattr(result, "merge_commit_sha", None)}))

    def submit_user_message(self, task_id: int, *, principal: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 64 * 1024:
            raise WorkflowError("message must be non-empty and at most 64 KiB", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            if row["owner_principal"] != principal:
                raise WorkflowError("task not found", 404)
            role = row["waiting_role"] or role_for_state(row)
            if role is None:
                raise WorkflowError("task has no active agent session", 409)
            message_id = self._queue_message(db, task_id, role, "user", content.strip(), principal)
            if row["state"] == TaskState.WAITING_FOR_USER.value:
                target_name = row["waiting_state"] or TaskState.PLAN.value
                try:
                    target = TaskState(target_name)
                except ValueError:
                    raise WorkflowError("stored waiting state is invalid") from None
                db.execute("UPDATE tasks SET waiting_role = NULL, waiting_state = NULL, updated_at = ? WHERE id = ?", (utc_now(), task_id))
                refreshed = _need_task(db, task_id)
                _set_state(db, refreshed, target, "user_answered", principal, payload={"message_id": message_id})
            else:
                _append_event(db, task_id=task_id, event_type="message_queued", from_state=row["state"], to_state=row["state"], actor=principal, run_id=row["run_id"], plan_version=row["plan_version"], head_sha=row["head_sha"], payload={"message_id": message_id, "role": role})
            return {"id": message_id, "task_id": task_id, "role": role, "status": "queued"}

    def wait_for_user(self, task_id: int, *, role: str, question: str) -> dict[str, Any]:
        if role not in _ROLE_NAMES or not isinstance(question, str) or not question.strip():
            raise WorkflowError("a valid role and user question are required", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            if role_for_state(row) != role:
                raise WorkflowError("question does not match the task's active role")
            db.execute("UPDATE tasks SET waiting_role = ?, waiting_state = ?, updated_at = ? WHERE id = ?", (role, row["state"], utc_now(), task_id))
            self._queue_message(db, task_id, role, "agent", question.strip()[:4096], f"agent:{role}")
            refreshed = _need_task(db, task_id)
            return _task(_set_state(db, refreshed, TaskState.WAITING_FOR_USER, "user_question", f"agent:{role}", payload={"role": role}))

    def block_task(self, task_id: int, *, actor: str, reason: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            if row["state"] in {TaskState.MERGED.value, TaskState.BLOCKED.value, TaskState.CANCELLED.value}:
                return _task(row)
            return _task(_set_state(db, row, TaskState.BLOCKED, "task_blocked", actor, payload={"reason": reason[:512]}))

    def cancel_task(self, task_id: int, *, principal: str) -> dict[str, Any]:
        """Cancel owner-controlled work and interrupt its durable lease."""
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            if row["owner_principal"] != principal:
                raise WorkflowError("task not found", 404)
            if row["state"] == TaskState.MERGED.value:
                raise WorkflowError("merged task cannot be cancelled")
            if row["state"] == TaskState.CANCELLED.value:
                return _task(row)
            now = utc_now()
            db.execute(
                "UPDATE agent_turns SET status = 'interrupted', finished_at = ?, error_code = 'owner_cancelled' WHERE task_id = ? AND status = 'claimed'",
                (now, task_id),
            )
            db.execute(
                "UPDATE tasks SET waiting_role = NULL, waiting_state = NULL, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            refreshed = _need_task(db, task_id)
            return _task(
                _set_state(
                    db,
                    refreshed,
                    TaskState.CANCELLED,
                    "task_cancelled",
                    principal,
                )
            )

    def session_ids_for_task(self, task_id: int) -> list[str]:
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT session_id FROM sessions WHERE task_id = ? ORDER BY role",
                (task_id,),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def get_session(self, task_id: int, role: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.db.execute("SELECT * FROM sessions WHERE task_id = ? AND role = ?", (task_id, role)).fetchone()
        return dict(row) if row is not None else None

    def get_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

    def save_session(self, task_id: int, session: Any) -> None:
        with self.store.transaction() as db:
            db.execute(
                """INSERT INTO sessions
                   (task_id, role, session_id, cwd, state_directory, config_json,
                    capabilities_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id, role) DO UPDATE SET
                     session_id=excluded.session_id, cwd=excluded.cwd,
                     state_directory=excluded.state_directory,
                     config_json=excluded.config_json,
                     capabilities_json=excluded.capabilities_json,
                     updated_at=excluded.updated_at""",
                (task_id, session.role, session.session_id, session.cwd, session.state_directory, json_dumps(dict(session.config)), json_dumps(dict(session.capabilities)), utc_now(), utc_now()),
            )

    def queue_for_role(self, task_id: int, role: str, *, actor: str, direction: str, content: str) -> int:
        with self.store.transaction() as db:
            return self._queue_message(db, task_id, role, direction, content, actor)

    def claim_turn(self, task_id: int, role: str, *, worker_id: str, lease_seconds: int, max_active: int) -> str | None:
        now = utc_now()
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="microseconds")
        with self.store.transaction() as db:
            db.execute("UPDATE agent_turns SET status = 'interrupted', finished_at = ?, error_code = 'lease_expired' WHERE status = 'claimed' AND lease_expires_at <= ?", (now, now))
            active = db.execute("SELECT COUNT(*) AS count FROM agent_turns WHERE status = 'claimed'").fetchone()["count"]
            if active >= max_active:
                return None
            current = _need_task(db, task_id)
            if not bool(current["trusted"]) or role_for_state(current) != role:
                return None
            if db.execute("SELECT 1 FROM agent_turns WHERE task_id = ? AND status = 'claimed' AND lease_expires_at > ?", (task_id, now)).fetchone():
                return None
            session = db.execute("SELECT session_id FROM sessions WHERE task_id = ? AND role = ?", (task_id, role)).fetchone()
            session_id = session["session_id"] if session else None
            sequence = db.execute("SELECT COUNT(*) AS count FROM agent_turns WHERE task_id = ? AND role = ?", (task_id, role)).fetchone()["count"] + 1
            idempotency_key = f"{current['run_id']}:{role}:{sequence}"
            db.execute(
                """INSERT INTO agent_turns
                   (task_id, role, session_id, idempotency_key, status, lease_owner,
                    lease_expires_at, started_at)
                   VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?)""",
                (task_id, role, session_id, idempotency_key, worker_id, lease_expires_at, now),
            )
            return idempotency_key

    def attach_session_to_turn(self, idempotency_key: str, session_id: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE agent_turns SET session_id = ? WHERE idempotency_key = ? AND status = 'claimed'",
                (session_id, idempotency_key),
            )

    def finish_turn(self, idempotency_key: str, *, status: str, error_code: str | None = None) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid agent turn status")
        with self.store.transaction() as db:
            db.execute("UPDATE agent_turns SET status = ?, finished_at = ?, error_code = ? WHERE idempotency_key = ? AND status = 'claimed'", (status, utc_now(), error_code, idempotency_key))

    def recover_expired_turns(self) -> int:
        with self.store.transaction() as db:
            cursor = db.execute("UPDATE agent_turns SET status = 'interrupted', finished_at = ?, error_code = 'lease_expired' WHERE status = 'claimed' AND lease_expires_at <= ?", (utc_now(), utc_now()))
            return cursor.rowcount

    def next_task_for_role(self, role: str) -> dict[str, Any] | None:
        if role not in _ROLE_NAMES:
            raise WorkflowError("unknown agent role", 422)
        now = utc_now()
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT * FROM tasks WHERE trusted = 1
                   AND state IN ('TRIAGE', 'PLAN', 'PLAN_REVIEW', 'BUILD', 'CODE_REVIEW', 'CI')
                   AND NOT EXISTS (
                       SELECT 1 FROM role_pauses pause
                       WHERE pause.task_id = tasks.id AND pause.role = ? AND pause.retry_after > ?
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM agent_turns active
                       WHERE active.task_id = tasks.id AND active.status = 'claimed'
                         AND active.lease_expires_at > ?
                   )
                   ORDER BY priority DESC, created_at, id""",
                (role, now, now),
            ).fetchall()
        for row in rows:
            task = _task(row)
            if role_for_state(task) == role:
                return task
        return None

    def defer_role(self, task_id: int, role: str, *, seconds: int, reason: str) -> None:
        if role not in _ROLE_NAMES or seconds < 1 or reason not in {"native_quota_wait"}:
            raise ValueError("invalid native role pause")
        retry_after = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            db.execute(
                "INSERT INTO role_pauses(task_id, role, retry_after, reason) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id, role) DO UPDATE SET retry_after=excluded.retry_after, reason=excluded.reason",
                (task_id, role, retry_after, reason),
            )
            _append_event(db, task_id=task_id, event_type="role_deferred", from_state=row["state"], to_state=row["state"],
                          actor="controller", run_id=row["run_id"], plan_version=row["plan_version"], head_sha=row["head_sha"],
                          payload={"role": role, "reason": reason, "retry_after": retry_after})

    def queued_messages(self, task_id: int, role: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT * FROM messages WHERE task_id = ? AND role = ? AND status = 'queued' ORDER BY id",
                (task_id, role),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_messages_sent(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self.store.transaction() as db:
            db.execute(
                f"UPDATE messages SET status = 'sent', updated_at = ? WHERE id IN ({placeholders}) AND status = 'queued'",
                (utc_now(), *message_ids),
            )

    def record_session_event(self, task_id: int, role: str, event: Any) -> None:
        # Keep event metadata for restart/debugging without duplicating prompts,
        # source text, or possible secret material into the controller database.
        with self.store.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO session_events
                   (task_id, role, session_id, sequence, method, update_kind, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, role, getattr(event, "session_id", None) or "", getattr(event, "sequence", 0), getattr(event, "method", "event"), getattr(event, "update_kind", None), None, getattr(event, "timestamp", None).isoformat() if getattr(event, "timestamp", None) else utc_now()),
            )

    def increment_role_failure(self, task_id: int, role: str) -> int:
        if role not in _ROLE_NAMES:
            raise WorkflowError("unknown agent role", 422)
        with self.store.transaction() as db:
            row = _need_task(db, task_id)
            failures = json.loads(row["role_failures_json"] or "{}")
            if row["state"] in {
                TaskState.MERGED.value,
                TaskState.BLOCKED.value,
                TaskState.CANCELLED.value,
            }:
                return int(failures.get(role, 0))
            failures[role] = int(failures.get(role, 0)) + 1
            db.execute("UPDATE tasks SET role_failures_json = ?, updated_at = ? WHERE id = ?", (json_dumps(failures), utc_now(), task_id))
            if failures[role] > self.max_turn_failures:
                refreshed = _need_task(db, task_id)
                _set_state(db, refreshed, TaskState.BLOCKED, "agent_turn_retry_limit", f"agent:{role}", payload={"role": role, "attempts": failures[role]})
            return failures[role]

    def _queue_message(
        self,
        db: sqlite3.Connection,
        task_id: int,
        role: str,
        direction: str,
        content: str,
        actor: str | None,
        *,
        status: str = "queued",
    ) -> int:
        if role not in _ROLE_NAMES:
            raise WorkflowError("unknown agent role", 422)
        if direction not in {"user", "agent", "system"}:
            raise WorkflowError("invalid message direction", 422)
        cursor = db.execute("INSERT INTO messages(task_id, role, direction, content, status, actor_principal, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (task_id, role, direction, content, status, actor, utc_now(), utc_now()))
        return int(cursor.lastrowid)

    def _audit_questions(self, db: sqlite3.Connection, row: sqlite3.Row, role: str, questions: list[str]) -> None:
        if not questions:
            return
        db.execute("UPDATE tasks SET waiting_role = ?, waiting_state = ?, updated_at = ? WHERE id = ?", (role, row["state"], utc_now(), row["id"]))
        for question in questions:
            self._queue_message(db, row["id"], role, "agent", question, f"agent:{role}", status="sent")

    @staticmethod
    def _require_state(row: sqlite3.Row, state: TaskState) -> None:
        if row["state"] != state.value:
            raise WorkflowError(f"expected state {state.value}, current state is {row['state']}")

    @staticmethod
    def _require_trusted(row: sqlite3.Row) -> None:
        if not bool(row["trusted"]):
            raise WorkflowError("untrusted task remains queued", 403)

    @staticmethod
    def _require_sha(sha: Any) -> None:
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise WorkflowError("commit SHA is invalid", 422)

    @staticmethod
    def _questions(values: list[str] | None) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 10 or any(not isinstance(value, str) or not value.strip() for value in values):
            raise WorkflowError("questions must be a list of at most ten non-empty strings", 422)
        return [value.strip()[:4096] for value in values]

    @staticmethod
    def _findings(values: list[str] | None) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(value, str) for value in values):
            raise WorkflowError("findings must be a list of at most 100 strings", 422)
        return [value.strip()[:4096] for value in values if value.strip()]

    @staticmethod
    def _optional_hash(value: str | None) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def role_for_state(task: Mapping[str, Any] | sqlite3.Row) -> str | None:
    state = task["state"]
    if state == TaskState.TRIAGE.value:
        return "triage"
    if state == TaskState.PLAN.value:
        return "planner"
    if state == TaskState.PLAN_REVIEW.value:
        return "reviewer"
    if state == TaskState.BUILD.value:
        return "implementer"
    if state == TaskState.CODE_REVIEW.value:
        return "reviewer"
    if state == TaskState.CI.value and task["ci_status"] == "failure":
        return "ci_babysitter"
    return None
