from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import yaml

from codefactory.capacity import CapacityBlocked, PullRequestCapacity
from codefactory.roles import load_config
from codefactory.store import Store
from codefactory.workflow import WorkflowService

REPO = "acme/widget"


def task(workflow, number):
    return workflow.admit_github_issue(SimpleNamespace(repository=REPO, number=number, title="Feature", body="Scope", labels=("factory:ready",)), owner_principal="github:alice", repository_allowed=True)


class Inventory:
    def __init__(self, count=0):
        self.pulls = [self.pull(number) for number in range(1, count + 1)]
        self.closed = {}
        self.fail = False

    @staticmethod
    def pull(number, **values):
        return SimpleNamespace(repository=REPO, number=number, head_ref=f"feature-{number}", base_ref="main", state=values.get("state", "open"), merged=values.get("merged", False), draft=number % 2 == 1)

    def list_active_pull_requests(self, _repository):
        if self.fail:
            raise RuntimeError("unavailable")
        return list(self.pulls)

    def get_pull_request(self, _repository, number):
        return self.closed.get(number) or next(pull for pull in self.pulls if pull.number == number)


@pytest.mark.parametrize("value", [0, -1, 101, True, 2.5, "5", None])
def test_yaml_rejects_non_bounded_integer_limits(tmp_path, value):
    config = yaml.safe_load(Path("config/runtime.yaml").read_text())
    config["repositories"] = {REPO: str(tmp_path)}
    for name in ("max_concurrent_agents", "max_open_prs"):
        invalid = {**config, name: value}
        path = tmp_path / "invalid.yaml"
        path.write_text(yaml.safe_dump(invalid))
        with pytest.raises(ValueError, match="integers from 1 to 100"):
            load_config(path)
    for name in ("max_concurrent_agents", "max_open_prs"):
        config.pop(name)
    path.write_text(yaml.safe_dump(config))
    valid, _ = load_config(path)
    assert (valid["max_concurrent_agents"], valid["max_open_prs"]) == (5, 5)


def test_global_native_ceiling_and_live_claim_survives_expiry_cancel_restart(tmp_path):
    path = tmp_path / "controller.sqlite3"
    stores = [Store(path), Store(path)]
    services = [WorkflowService(store) for store in stores]
    tasks = [task(services[0], number) for number in range(1, 7)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        keys = list(pool.map(lambda item: services[item[0] % 2].claim_turn(item[1]["id"], "triage", worker_id=f"worker-{item[0]}", lease_seconds=60, max_active=5), enumerate(tasks)))
    assert sum(key is not None for key in keys) == 5
    index = next(index for index, key in enumerate(keys) if key is not None)
    key = keys[index]
    gate = PullRequestCapacity(stores[0], [REPO], limit=1)
    gate.refresh(Inventory())
    assert gate.admit(tasks[index])
    assert services[0].start_native_turn(key)
    services[0].cancel_task(tasks[index]["id"], principal="github:alice")
    assert gate.status()["unpublished_features"] == 1
    with stores[0].transaction() as db:
        db.execute("UPDATE agent_turns SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
    assert services[1].recover_expired_turns() == 4
    assert services[1].claim_turn(tasks[index]["id"], "triage", worker_id="restart", lease_seconds=60, max_active=5) is None
    other = tasks[next(index for index, key in enumerate(keys) if key is None)]
    assert services[1].claim_turn(other["id"], "triage", worker_id="lowered", lease_seconds=60, max_active=1) is None
    services[0].release_worker_turns(f"worker-{index}")
    assert gate.status()["unpublished_features"] == 0
    assert services[1].claim_turn(other["id"], "triage", worker_id="restart", lease_seconds=60, max_active=1)
    for store in stores:
        store.close()


def test_feature_slots_atomic_reused_waiting_cancelled_and_legacy_pr_gate(tmp_path):
    path = tmp_path / "controller.sqlite3"
    stores = [Store(path), Store(path)]
    workflow = WorkflowService(stores[0])
    tasks = [task(workflow, number) for number in range(11, 14)]
    gates = [PullRequestCapacity(store, [REPO]) for store in stores]
    inventory = Inventory(4)
    assert not gates[0].admit(tasks[0])
    for gate in gates:
        gate.refresh(inventory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(pool.map(lambda index: gates[index].admit(tasks[index]), range(2)))
    assert sum(admitted) == 1
    owner = tasks[admitted.index(True)]
    workflow.wait_for_user(owner["id"], role="triage", question="Which target?")
    assert gates[0].status()["used"] == 5
    workflow.submit_user_message(owner["id"], principal="github:alice", content="Target one")
    assert gates[0].admit(owner) and gates[0].status()["unpublished_features"] == 1
    workflow.cancel_task(owner["id"], principal="github:alice")
    assert gates[0].status()["used"] == 4
    inventory.pulls.append(inventory.pull(5))
    gates[0].refresh(inventory)
    assert not gates[0].admit(tasks[2])
    # Only controller-persisted, inventory-verified PR references reuse a slot.
    assert not gates[0].admit({**tasks[2], "pull_number": 1})
    with stores[0].transaction() as db:
        db.execute("UPDATE tasks SET pull_number = 1 WHERE id = ?", (tasks[2]["id"],))
    assert gates[0].admit(tasks[2])
    inventory.pulls = [pull for pull in inventory.pulls if pull.number != 1]
    gates[0].refresh(inventory)
    assert not gates[0].admit(tasks[2])  # closed links cannot become new-feature bypasses
    assert gates[0].status()["used"] == 4
    inventory.fail = True
    with pytest.raises(CapacityBlocked):
        gates[0].refresh(inventory)
    assert gates[0].status()["used"] is None and not gates[0].admit(tasks[1])
    for store in stores:
        store.close()


def test_publication_reservation_prevents_sixth_and_unknown_result_holds(tmp_path):
    stores = [Store(tmp_path / "controller.sqlite3"), Store(tmp_path / "controller.sqlite3")]
    gates = [PullRequestCapacity(store, [REPO], writes_enabled=True) for store in stores]
    inventory = Inventory(4)
    entered, release = Event(), Event()
    def publish():
        entered.set()
        assert release.wait(5)
        raise RuntimeError("response lost after remote publication may have succeeded")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(gates[0], inventory, REPO, "new-feature", "main", publish)
        assert entered.wait(5)
        with pytest.raises(CapacityBlocked, match="capacity_full"):
            gates[1](inventory, REPO, "other-feature", "main", lambda: pytest.fail("sixth PR published"))
        release.set()
        with pytest.raises(RuntimeError, match="response lost"):
            first.result()
    gates[1].refresh(inventory)
    assert gates[1].status()["used"] == 5 and gates[1].status()["reserved"] == 1
    for store in stores:
        store.close()


def test_publication_converts_feature_slot_and_confirmed_closure_releases(tmp_path):
    store = Store(tmp_path / "controller.sqlite3")
    workflow = WorkflowService(store)
    feature = task(workflow, 42)
    inventory = Inventory()
    gate = PullRequestCapacity(store, [REPO], limit=1, writes_enabled=True)
    gate.refresh(inventory)
    assert gate.admit(feature)
    with store.transaction() as db:
        db.execute("INSERT INTO task_workspaces VALUES (?, ?, ?, ?)", (feature["id"], str(tmp_path), "feature-1", "a" * 40))
    pull = gate(inventory, REPO, "feature-1", "main", lambda: inventory.pull(1), task_id=feature["id"])
    assert pull.number == 1 and gate.status()["used"] == 1
    inventory.pulls.append(pull)
    gate.refresh(inventory)
    assert gate.status()["used"] == 1 and gate.status()["unpublished_features"] == 0
    inventory.pulls.clear()
    gate.refresh(inventory)
    assert gate.status()["used"] == 0
    with pytest.raises(CapacityBlocked, match="gates_not_enabled"):
        PullRequestCapacity(store, [REPO])(inventory, REPO, "x", "main", lambda: pytest.fail("bootstrap write"))
    store.close()
