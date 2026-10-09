"""SQLite persistence for workflow state, sessions, messages, and leases.

The store exposes transactions to the deterministic workflow service. HTTP
handlers must use that service rather than mutating task rows directly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Store:
    """A durable SQLite store with serialized write transactions."""

    def __init__(self, db_path: str | Path):
        database = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path).expanduser())
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            database,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = FULL")
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository TEXT NOT NULL COLLATE NOCASE,
                    issue_number INTEGER NOT NULL CHECK (issue_number > 0),
                    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                    trusted INTEGER NOT NULL CHECK (trusted IN (0, 1)),
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (repository, issue_number)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_task_id_id ON events(task_id, id);
                """
            )
            self._add_columns(
                "tasks",
                {
                    "owner_principal": "TEXT NOT NULL DEFAULT ''",
                    "issue_body": "TEXT",
                    "run_id": "TEXT",
                    "priority": "INTEGER NOT NULL DEFAULT 0",
                    "plan_version": "INTEGER NOT NULL DEFAULT 0",
                    "plan_text": "TEXT",
                    "reviewed_plan_version": "INTEGER",
                    "approved_plan_version": "INTEGER",
                    "head_sha": "TEXT",
                    "reviewed_sha": "TEXT",
                    "ci_sha": "TEXT",
                    "ci_status": "TEXT",
                    "preview_id": "TEXT",
                    "preview_url": "TEXT",
                    "preview_sha": "TEXT",
                    "preview_healthy": "INTEGER NOT NULL DEFAULT 0",
                    "pull_number": "INTEGER",
                    "pull_url": "TEXT",
                    "ci_failures": "INTEGER NOT NULL DEFAULT 0",
                    "review_repairs": "INTEGER NOT NULL DEFAULT 0",
                    "plan_revisions": "INTEGER NOT NULL DEFAULT 0",
                    "waiting_role": "TEXT",
                    "waiting_state": "TEXT",
                    "role_failures_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            self._add_columns(
                "events",
                {
                    "actor_principal": "TEXT",
                    "run_id": "TEXT",
                    "idempotency_key": "TEXT",
                    "plan_version": "INTEGER",
                    "head_sha": "TEXT",
                    "payload_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            self.db.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency_key
                    ON events(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS sessions (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    cwd TEXT NOT NULL,
                    state_directory TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, role)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('user', 'agent', 'system')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    actor_principal TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_queue
                    ON messages(task_id, role, status, id);
                CREATE TABLE IF NOT EXISTS role_pauses (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    retry_after TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (task_id, role)
                );
                CREATE TABLE IF NOT EXISTS task_workspaces (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE,
                    branch TEXT NOT NULL UNIQUE,
                    base_sha TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    update_kind TEXT,
                    summary TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS session_events_task_id_id
                    ON session_events(task_id, id);
                CREATE TABLE IF NOT EXISTS agent_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    session_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed', 'failed', 'interrupted')),
                    lease_owner TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS agent_turns_active
                    ON agent_turns(status, lease_expires_at);
                CREATE TABLE IF NOT EXISTS pr_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL COLLATE NOCASE,
                    head TEXT NOT NULL,
                    base TEXT NOT NULL,
                    pull_number INTEGER,
                    task_id INTEGER REFERENCES tasks(id),
                    UNIQUE(repository, head, base)
                );
                CREATE TABLE IF NOT EXISTS feature_slots (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pr_inventory (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    active_count INTEGER NOT NULL,
                    verified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    approval_type TEXT NOT NULL CHECK (approval_type IN ('plan', 'pull_request')),
                    principal TEXT NOT NULL,
                    plan_version INTEGER,
                    head_sha TEXT,
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, approval_type, plan_version, head_sha)
                );
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_name TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL
                );
                """
            )
            legacy_turns = "native_started" not in {row["name"] for row in self.db.execute("PRAGMA table_info(agent_turns)")}
            self._add_columns("agent_turns", {"native_started": "INTEGER NOT NULL DEFAULT 0"})
            if legacy_turns:
                self.db.execute("UPDATE agent_turns SET native_started = 1 WHERE status = 'claimed'")
            self._add_columns("pr_inventory", {"active_json": "TEXT NOT NULL DEFAULT '[]'"})
            self._add_columns("pr_reservations", {"task_id": "INTEGER REFERENCES tasks(id)"})
            # Older local tasks were created by the single-operator baseline.
            # They remain owned by the explicit local principal, but retain
            # their existing trusted bit only for that operator's migration.
            self.db.execute(
                "UPDATE tasks SET owner_principal = 'local-operator' WHERE owner_principal = ''"
            )

    def _add_columns(self, table: str, columns: Mapping[str, str]) -> None:
        existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self.db.close()
