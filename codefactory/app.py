import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, field_validator


class TaskState(str, Enum):
    TRIAGE = "TRIAGE"
    PLAN = "PLAN"
    PLAN_REVIEW = "PLAN_REVIEW"
    HUMAN_PLAN_APPROVAL = "HUMAN_PLAN_APPROVAL"
    BUILD = "BUILD"
    CODE_REVIEW = "CODE_REVIEW"
    CI = "CI"


class TaskCreate(BaseModel):
    repository: str
    issue_number: int = Field(gt=0)
    title: str
    trusted: bool = False

    @field_validator("repository", "title")
    @classmethod
    def trim_and_require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("repository")
    @classmethod
    def require_owner_and_name(cls, value: str) -> str:
        if value.count("/") != 1:
            raise ValueError("must use owner/name format")
        owner, name = value.split("/", 1)
        owner, name = owner.strip(), name.strip()
        if not owner or not name:
            raise ValueError("must use owner/name format")
        if any(character.isspace() for character in owner + name):
            raise ValueError("owner and repository name cannot contain whitespace")
        return f"{owner}/{name}"


class TaskTransition(BaseModel):
    state: TaskState
    expected_state: TaskState


TRANSITIONS = {
    TaskState.TRIAGE: {TaskState.PLAN},
    TaskState.PLAN: {TaskState.PLAN_REVIEW},
    TaskState.PLAN_REVIEW: {TaskState.PLAN, TaskState.HUMAN_PLAN_APPROVAL},
    TaskState.HUMAN_PLAN_APPROVAL: {TaskState.BUILD},
    TaskState.BUILD: {TaskState.CODE_REVIEW},
    TaskState.CODE_REVIEW: {TaskState.BUILD, TaskState.CI},
    TaskState.CI: set(),
}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _task(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["trusted"] = bool(result["trusted"])
    return result


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Create a controller app backed by a local SQLite database."""
    if db_path is None:
        db_path = os.environ.get("CODEFACTORY_DB_PATH", "codefactory.sqlite3")
    database = str(db_path) if str(db_path) == ":memory:" else str(Path(db_path).expanduser())
    if database != ":memory:":
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(
        database,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = FULL")
    db.executescript(
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

    lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(_api: FastAPI):
        try:
            yield
        finally:
            with lock:
                db.close()

    api = FastAPI(title="Codefactory", version="0.1.0", lifespan=lifespan)

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/tasks", status_code=201)
    def create_task(payload: TaskCreate, response: Response) -> dict:
        with lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    """SELECT * FROM tasks
                       WHERE repository = ? AND issue_number = ?""",
                    (payload.repository, payload.issue_number),
                ).fetchone()
                if existing is not None:
                    db.execute("COMMIT")
                    response.status_code = 200
                    return _task(existing)

                now = _now()
                cursor = db.execute(
                    """INSERT INTO tasks
                       (repository, issue_number, title, trusted,
                        state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload.repository,
                        payload.issue_number,
                        payload.title,
                        int(payload.trusted),
                        TaskState.TRIAGE.value,
                        now,
                        now,
                    ),
                )
                task_id = cursor.lastrowid
                db.execute(
                    """INSERT INTO events (task_id, event_type, from_state, to_state, created_at)
                       VALUES (?, 'created', NULL, ?, ?)""",
                    (task_id, TaskState.TRIAGE.value, now),
                )
                row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                db.execute("COMMIT")
                return _task(row)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @api.get("/tasks")
    def list_tasks() -> list[dict]:
        with lock:
            rows = db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            return [_task(row) for row in rows]

    @api.get("/tasks/{task_id}")
    def get_task(task_id: int) -> dict:
        with lock:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="task not found")
            return _task(row)

    @api.get("/tasks/{task_id}/events")
    def list_events(task_id: int) -> list[dict]:
        with lock:
            exists = db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="task not found")
            rows = db.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    @api.post("/tasks/{task_id}/transition")
    def transition_task(task_id: int, payload: TaskTransition) -> dict:
        with lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="task not found")

                current = TaskState(row["state"])
                if current == payload.state:
                    if payload.expected_state == current:
                        db.execute("COMMIT")
                        return _task(row)
                    latest = db.execute(
                        """SELECT from_state FROM events
                           WHERE task_id = ? ORDER BY id DESC LIMIT 1""",
                        (task_id,),
                    ).fetchone()
                    if latest is not None and latest["from_state"] == payload.expected_state.value:
                        db.execute("COMMIT")
                        return _task(row)
                if current != payload.expected_state:
                    raise HTTPException(
                        status_code=409,
                        detail=f"expected state {payload.expected_state.value}, current state is {current.value}",
                    )
                if not _can_transition(current, payload.state):
                    raise HTTPException(
                        status_code=409,
                        detail=f"cannot transition from {current.value} to {payload.state.value}",
                    )
                if not bool(row["trusted"]) and current == TaskState.TRIAGE:
                    raise HTTPException(status_code=409, detail="untrusted tasks must remain in TRIAGE")

                now = _now()
                db.execute(
                    "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                    (payload.state.value, now, task_id),
                )
                db.execute(
                    """INSERT INTO events (task_id, event_type, from_state, to_state, created_at)
                       VALUES (?, 'transitioned', ?, ?, ?)""",
                    (task_id, current.value, payload.state.value, now),
                )
                updated = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                db.execute("COMMIT")
                return _task(updated)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    return api


def _can_transition(current: TaskState, target: TaskState) -> bool:
    return target in TRANSITIONS.get(current, set())

