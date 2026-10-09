"""Controller-owned draft admission; the HTTP client has no database authority."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from .roles import resource_limit
from .store import utc_now


class CapacityBlocked(RuntimeError):
    pass


class PullRequestCapacity:
    def __init__(self, store, repositories, limit=5, *, writes_enabled=False):
        self.store, self.repositories = store, tuple(repositories)
        self.limit = resource_limit(limit)
        self.writes_enabled = writes_enabled
        self.inventory_reason = "active_pr_inventory_unverified"

    def _refresh(self, db, github):
        try:
            active = {(pull.repository.casefold(), pull.number): pull for repository in self.repositories for pull in github.list_active_pull_requests(repository)}
            for reservation in db.execute("SELECT * FROM pr_reservations").fetchall():
                key = (reservation["repository"].casefold(), reservation["pull_number"])
                matches = any(pull.repository.casefold() == key[0] and pull.head_ref == reservation["head"] and pull.base_ref == reservation["base"] for pull in active.values())
                if key in active or matches:
                    db.execute("DELETE FROM pr_reservations WHERE reservation_id = ?", (reservation["reservation_id"],))
                    if reservation["task_id"] is not None:
                        db.execute("DELETE FROM feature_slots WHERE task_id = ?", (reservation["task_id"],))
                elif reservation["pull_number"]:
                    pull = github.get_pull_request(reservation["repository"], reservation["pull_number"])
                    if pull.state == "closed" or pull.merged:
                        db.execute("DELETE FROM pr_reservations WHERE reservation_id = ?", (reservation["reservation_id"],))
                        if reservation["task_id"] is not None:
                            db.execute("DELETE FROM feature_slots WHERE task_id = ?", (reservation["task_id"],))
                    elif pull.state == "open":
                        active[key] = pull
                        db.execute("DELETE FROM pr_reservations WHERE reservation_id = ?", (reservation["reservation_id"],))
                        if reservation["task_id"] is not None:
                            db.execute("DELETE FROM feature_slots WHERE task_id = ?", (reservation["task_id"],))
                    else:
                        raise CapacityBlocked("active_pr_inventory_unverified")
            for feature in db.execute("SELECT tasks.id, repository, pull_number FROM feature_slots JOIN tasks ON tasks.id = feature_slots.task_id").fetchall():
                if (feature["repository"].casefold(), feature["pull_number"]) in active:
                    db.execute("DELETE FROM feature_slots WHERE task_id = ?", (feature["id"],))
                elif feature["pull_number"] is not None:
                    pull = github.get_pull_request(feature["repository"], feature["pull_number"])
                    if pull.state == "closed" or pull.merged:
                        db.execute("DELETE FROM feature_slots WHERE task_id = ?", (feature["id"],))
                    else:
                        raise CapacityBlocked("active_pr_inventory_unverified")
            db.execute("INSERT OR REPLACE INTO pr_inventory(singleton, active_count, verified_at, active_json) VALUES (1, ?, ?, ?)", (len(active), utc_now(), json.dumps(list(active))))
            self.inventory_reason = None
            return len(active)
        except Exception:
            self.inventory_reason = "active_pr_inventory_unverified"
            raise CapacityBlocked(self.inventory_reason) from None

    def refresh(self, github):
        with self.store.transaction() as db:
            self._refresh(db, github)

    def status(self):
        with self.store._lock:
            inventory = self.store.db.execute("SELECT * FROM pr_inventory").fetchone()
            reserved = self._reserved(self.store.db)
            features = self.store.db.execute("SELECT COUNT(*) FROM feature_slots").fetchone()[0]
        fresh = inventory is not None and (datetime.now(timezone.utc) - datetime.fromisoformat(inventory["verified_at"])).total_seconds() < 120
        used = inventory["active_count"] + reserved if fresh and not self.inventory_reason else None
        reason = self.inventory_reason or ("active_pr_inventory_unverified" if not fresh else ("active_pr_capacity_full" if used >= self.limit else None))
        return {"limit": self.limit, "used": used, "reserved": reserved, "unpublished_features": features, "blocked": reason is not None, "reason": reason}

    @staticmethod
    def _reserved(db):
        return db.execute("SELECT (SELECT COUNT(*) FROM feature_slots) + (SELECT COUNT(*) FROM pr_reservations WHERE task_id IS NULL OR task_id NOT IN (SELECT task_id FROM feature_slots))").fetchone()[0]

    def admit(self, task, *, reserve=True):
        """One slot per canonical admitted issue, reused through waits and roles."""
        with self.store.transaction() as db:
            current = db.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
            inventory = db.execute("SELECT * FROM pr_inventory").fetchone()
            if current is None or not current["trusted"] or self.inventory_reason or inventory is None:
                return False
            if (datetime.now(timezone.utc) - datetime.fromisoformat(inventory["verified_at"])).total_seconds() >= 120:
                return False
            active = {tuple(key) for key in json.loads(inventory["active_json"])}
            if current["pull_number"] is not None:
                return (current["repository"].casefold(), current["pull_number"]) in active
            if inventory["active_count"] >= self.limit:
                return False
            if inventory["active_count"] + self._reserved(db) > self.limit:
                return False
            if db.execute("SELECT 1 FROM feature_slots WHERE task_id = ?", (current["id"],)).fetchone():
                return True
            if inventory["active_count"] + self._reserved(db) >= self.limit:
                return False
            if reserve:
                db.execute("INSERT INTO feature_slots VALUES (?, ?)", (current["id"], utc_now()))
            return True

    def __call__(self, github, repository, head, base, publish, *, task_id=None):
        if not self.writes_enabled:
            raise CapacityBlocked("draft_review_and_publication_gates_not_enabled")
        if repository not in self.repositories:
            raise CapacityBlocked("repository_not_configured")
        reservation_id = str(uuid4())
        # Serialize inventory+reservation across controllers sharing this DB.
        # Keep the reservation through the remote call and any uncertain result.
        with self.store.transaction() as db:
            used = self._refresh(db, github)
            reserved = self._reserved(db)
            feature = False
            if task_id is not None:
                task = db.execute("SELECT tasks.*, task_workspaces.branch FROM tasks JOIN task_workspaces ON task_workspaces.task_id = tasks.id WHERE tasks.id = ?", (task_id,)).fetchone()
                feature = db.execute("SELECT 1 FROM feature_slots WHERE task_id = ?", (task_id,)).fetchone() is not None
                if task is None or not task["trusted"] or task["repository"].casefold() != repository.casefold() or task["branch"] != head or not feature:
                    raise CapacityBlocked("feature_publication_identity_unverified")
            if used >= self.limit or used + reserved - int(feature) >= self.limit:
                raise CapacityBlocked("active_pr_capacity_full")
            if db.execute("SELECT 1 FROM pr_reservations WHERE repository = ? AND head = ? AND base = ?", (repository, head, base)).fetchone():
                raise CapacityBlocked("publication_outcome_unverified")
            db.execute("INSERT INTO pr_reservations(reservation_id, repository, head, base, task_id) VALUES (?, ?, ?, ?, ?)", (reservation_id, repository, head, base, task_id))
        pull = publish()
        with self.store.transaction() as db:
            db.execute("UPDATE pr_reservations SET pull_number = ? WHERE reservation_id = ?", (pull.number, reservation_id))
        return pull
