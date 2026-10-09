"""Local operator admission and harmless native smoke; never an HTTP trust flag."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from .runtime import compose_runtime


def read_snapshot(path: Path, runtime):
    path = path.expanduser().resolve()
    if any(path.is_relative_to(repository) for repository in runtime.workspaces.repositories.values()) or path.is_relative_to(runtime.workspaces.root):
        raise ValueError("operator snapshot must be outside agent workspaces")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("operator snapshot must be user-owned with mode 0600")
        raw = os.read(fd, 256 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 256 * 1024:
        raise ValueError("operator snapshot is too large")
    value = json.loads(raw)
    repository = value["repository"]
    number = value["number"]
    if not runtime.github_config.allows_repository(repository) or type(number) is not int or number < 1:
        raise ValueError("snapshot issue is outside the operator repository allowlist")
    if value["source_url"] != f"https://github.com/{repository}/issues/{number}":
        raise ValueError("snapshot provenance must identify the exact existing GitHub issue")
    if not isinstance(value.get("labels"), list) or not all(isinstance(item, str) for item in value["labels"]):
        raise ValueError("snapshot labels must preserve the actual GitHub label list")
    return value


def admit(runtime, value, *, operator_admit=False):
    if not runtime.principal:
        raise ValueError("exactly one local authenticated operator must be configured")
    issue = SimpleNamespace(repository=value["repository"], number=value["number"], title=value["title"], body=value.get("body"), labels=tuple(value["labels"]))
    if operator_admit:
        # This privileged local command is an explicit operator action. The
        # audit source distinguishes it from genuine labeled GitHub ingestion.
        return runtime.workflow._create_task(
            repository=issue.repository, issue_number=issue.number, title=issue.title,
            owner_principal=runtime.principal, issue_body=issue.body, trusted=True,
            source="operator_bootstrap", idempotency_key=f"operator-bootstrap:{issue.repository}:{issue.number}",
        )
    result = runtime.workflow.admit_github_issue(issue, owner_principal=runtime.principal, repository_allowed=True)
    if result is None:
        raise ValueError("snapshot has no genuine admission label; explicit --operator-admit required")
    return result


async def smoke(runtime, task):
    await runtime.scheduler.start(background=False)
    try:
        await runtime.scheduler.run_once()
        current = runtime.workflow.get_task(task["id"])
        session = runtime.workflow.get_session(task["id"], "triage")
        with runtime.store._lock:
            turn = runtime.store.db.execute("SELECT status, error_code FROM agent_turns WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
        report = {"task_id": task["id"], "state_after_triage": current["state"], "native_session_persisted": session is not None,
                  "isolated_worktree": str(runtime.workspaces.path_for(task)), "turn": dict(turn) if turn else None,
                  "roles": runtime.gateway.status()}
        runtime.workflow.cancel_task(task["id"], principal=runtime.principal)
        if not turn or turn["status"] != "completed":
            raise RuntimeError("native triage smoke did not complete; inspect health/turn reason")
        return report
    finally:
        await runtime.scheduler.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["import-github", "triage-smoke"])
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--operator-admit", action="store_true", help="Explicit audited local admission of this allowlisted existing issue, independent of its labels")
    parser.add_argument("--db", type=Path, required=True, help="Use a separate smoke database for triage-smoke")
    args = parser.parse_args()
    if args.command == "triage-smoke" and args.db.expanduser().exists():
        parser.error("triage-smoke requires a new, separate database path")
    runtime, _ = compose_runtime(args.db)
    try:
        value = read_snapshot(args.snapshot, runtime)
        task = admit(runtime, value, operator_admit=args.operator_admit)
        if args.command == "triage-smoke":
            report = asyncio.run(smoke(runtime, task))
        else:
            report = {"task_id": task["id"], "state": task["state"], "source": "operator_bootstrap" if args.operator_admit else "github_labeled_snapshot"}
        print(json.dumps(report, sort_keys=True))
    finally:
        runtime.store.close()


if __name__ == "__main__":
    main()
