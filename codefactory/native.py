"""Native ACP routing with explicit unavailable roles and no paid API route."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .acp import ACPConfigurationError, ACPGateway, ACPProtocolError
from .roles import RoleRoute


class RoleUnavailable(RuntimeError):
    pass


class ClaudeReviewGateway(ACPGateway):
    """Claude subscription review, with every native tool removed."""

    @staticmethod
    def _session_params(cwd: str) -> dict[str, Any]:
        return {
            "cwd": cwd, "mcpServers": [],
            "_meta": {"claudeCode": {"options": {
                "tools": [], "settingSources": [], "plugins": [], "hooks": {},
                "mcpServers": {}, "additionalDirectories": [],
            }}},
        }

    @classmethod
    def _role_config(cls, role: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
        normalized = cls._validate_config(config)
        if role != "reviewer" or normalized.get("mode") != "read-only" or not normalized.get("model"):
            raise ACPConfigurationError("Claude native adapter accepts explicit tool-free reviewer sessions only")
        if "reasoning_effort" in normalized:
            raise ACPConfigurationError("Claude native adapter has no Codex reasoning effort setting")
        return normalized

    async def _apply_config(self, session_id, config_options, config):
        # Native tools: [] is stricter than Claude's named permission modes.
        return await super()._apply_config(session_id, config_options, {"model": config["model"]})


class NativeRoleGateway:
    def __init__(self, routes: Mapping[str, RoleRoute], adapters: Mapping[str, Any] | None = None):
        self.routes = dict(routes)
        self.on_event = None
        self.on_agent_request = None
        self.blocked: dict[str, str] = {}
        self.verified_roles: set[str] = set()
        self._sessions: dict[str, str] = {}
        adapters = adapters or {}
        codex_command = adapters.get("codex", ["npx", "--yes", "@agentclientprotocol/codex-acp@2.1.1"])
        claude_default = str(Path.home() / ".local/share/codefactory/adapters/node_modules/.bin/claude-agent-acp")
        self.backends = {
            "codex": ACPGateway(command=codex_command, on_event=self._event, on_agent_request=self._request),
            "claude": ClaudeReviewGateway(command=adapters.get("claude", [claude_default]), on_event=self._event, on_agent_request=self._request),
        }

    async def _event(self, event):
        if self.on_event is not None:
            return await self.on_event(event)

    async def _request(self, method, params):
        if self.on_agent_request is not None:
            return await self.on_agent_request(method, params)
        return {"outcome": {"outcome": "cancelled"}}

    async def start(self):
        # A missing reviewer or integration must not prevent the local API.
        for name, gateway in self.backends.items():
            try:
                await gateway.start()
            except Exception:
                self.blocked[name] = "native_adapter_unavailable"
        return {}

    def available(self, role: str) -> bool:
        route = self.routes[role]
        return role not in self.blocked and route.backend not in self.blocked

    def status(self) -> dict[str, Any]:
        return {
            role: {"backend": route.backend, "model": route.model, "reasoning_effort": route.reasoning_effort or None,
                   "status": "blocked" if not self.available(role) else ("ready" if role in self.verified_roles else "configured"),
                   "reason": self.blocked.get(role) or self.blocked.get(route.backend)}
            for role, route in self.routes.items()
        }

    async def _session(self, role, task_id, cwd, config, session_id=None):
        if not self.available(role):
            raise RoleUnavailable("required native role is unavailable")
        route = self.routes[role]
        if dict(config) != route.session_config():
            raise ACPConfigurationError("persisted role config does not match configured routing")
        gateway = self.backends[route.backend]
        try:
            if session_id is None:
                result = await gateway.new_session(role, task_id, cwd, config)
            else:
                result = await gateway.load_session(session_id, role, task_id, cwd, config)
        except ACPConfigurationError:
            self.blocked[role] = "requested_native_model_or_configuration_unavailable"
            raise RoleUnavailable("requested native role configuration is unavailable") from None
        except ACPProtocolError:
            self.blocked[route.backend] = "native_adapter_unavailable"
            raise RoleUnavailable("native role adapter is unavailable") from None
        self._sessions[result.session_id] = route.backend
        self.verified_roles.add(role)
        return result

    async def new_session(self, role, task_id, cwd, config):
        return await self._session(role, task_id, cwd, config)

    async def load_session(self, session_id, role, task_id, cwd, config):
        return await self._session(role, task_id, cwd, config, session_id)

    async def prompt(self, session_id, content):
        return await self.backends[self._sessions[session_id]].prompt(session_id, content)

    async def cancel(self, session_id):
        return await self.backends[self._sessions[session_id]].cancel(session_id)

    @property
    def active_sessions(self):
        return tuple(session for gateway in self.backends.values() for session in gateway.active_sessions)

    async def close(self):
        for gateway in self.backends.values():
            await gateway.close()
