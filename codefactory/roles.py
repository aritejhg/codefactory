"""Explicit native model routing; absent models never select a fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


ROLES = frozenset({"triage", "planner", "reviewer", "implementer", "ci_babysitter"})


@dataclass(frozen=True)
class RoleRoute:
    backend: str
    model: str
    reasoning_effort: str
    mode: str

    def session_config(self) -> dict[str, str]:
        result = {"model": self.model, "mode": self.mode}
        if self.reasoning_effort:
            result["reasoning_effort"] = self.reasoning_effort
        return result


def load_config(path: Path) -> tuple[dict[str, Any], dict[str, RoleRoute]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) - {"roles", "repositories", "worktree_root", "github_token_file", "poll_seconds", "quota_retry_seconds", "adapters", "base_prompt_file"}:
        raise ValueError("runtime YAML has unsupported configuration")
    raw_roles = value.get("roles")
    if not isinstance(raw_roles, Mapping) or set(raw_roles) != ROLES:
        raise ValueError("runtime YAML must configure every supported role")
    routes = {}
    for role, raw in raw_roles.items():
        if not isinstance(raw, dict) or set(raw) - {"backend", "model", "reasoning_effort"}:
            raise ValueError("invalid role route")
        backend, model = raw.get("backend"), raw.get("model")
        effort = raw.get("reasoning_effort", "")
        if backend not in {"codex", "claude"} or not isinstance(model, str) or not model.strip() or not isinstance(effort, str):
            raise ValueError("role backend/model must be explicitly configured")
        if backend == "claude" and (role != "reviewer" or effort):
            raise ValueError("Claude routing supports tool-free review only")
        mode = "workspace-write" if role == "implementer" else "read-only"
        routes[role] = RoleRoute(backend, model.strip(), effort, mode)
    repositories = value.get("repositories", {})
    if not isinstance(repositories, dict) or not repositories:
        raise ValueError("runtime must have an explicit repository allowlist")
    for name, path_value in repositories.items():
        if not isinstance(name, str) or name.count("/") != 1 or not isinstance(path_value, str):
            raise ValueError("invalid repository mapping")
        path_value = Path(path_value).expanduser()
        if not path_value.is_absolute() or not path_value.is_dir():
            raise ValueError("repository checkout must be an existing absolute directory")
    return value, routes
