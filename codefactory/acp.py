"""Small ACP v1 subprocess gateway for the maintained Codex ACP adapter.

The gateway owns one adapter process, initializes it once, and keeps ACP's
native session identifiers usable through ``session/load`` after a restart.
It deliberately does not implement steering or queueing: controllers should
durably queue turns and call ``prompt`` when the session is idle.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ADAPTER_COMMAND = ("npx", "--yes", "@agentclientprotocol/codex-acp@2.1.1")
ACP_PROTOCOL_VERSION = 1
DEFAULT_TURN_TIMEOUT = 300.0
DEFAULT_CANCEL_GRACE = 5.0
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_PROMPT_BYTES = 256 * 1024
DEFAULT_MAX_TURN_EVENT_BYTES = 32 * 1024 * 1024
_TIMEOUT_UNSET = object()
_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "CODEX_PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TZ",
        "TERM",
        "USER",
        "LOGNAME",
        "SHELL",
        "XDG_RUNTIME_DIR",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
    }
)
_DEFAULT_MODEL = "gpt-5.6-luna"
_DEFAULT_REASONING_EFFORT = "xhigh"
_ROLE_MODES = {
    "planner": "read-only",
    "reviewer": "read-only",
    "triage": "read-only",
    "implementer": "workspace-write",
    "ci_babysitter": "read-only",
    "ci-babysitter": "read-only",
}
_CODEX_CONFIG = {
    "default_permissions": "workspace-only",
    "permissions": {
        "workspace-only": {
            "extends": ":workspace",
            "filesystem": {
                ":root": "deny",
                ":minimal": "read",
                ":tmpdir": "deny",
                ":slash_tmp": "deny",
            },
        },
    },
}

EventCallback = Callable[["ACPEvent"], Awaitable[None] | None]
AgentRequestHandler = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]] | Mapping[str, Any]]


class ACPError(RuntimeError):
    """Base error for gateway and adapter failures."""


class ACPProtocolError(ACPError):
    """The adapter exited or returned a malformed or incompatible ACP message."""


class ACPCapabilityError(ACPError):
    """The adapter does not advertise a capability required by the controller."""


class ACPConfigurationError(ACPError):
    """A requested session configuration is not offered by the adapter."""


class ACPBusyError(ACPError):
    """A prompt was submitted while the same session already had an active turn."""


class ACPTimeoutError(ACPError):
    """A turn exceeded its configured time or streamed-event bound."""


class ACPRemoteError(ACPError):
    """An ACP request failed remotely."""

    def __init__(self, method: str, code: int | None, message: str):
        # Keep params, prompts, and arbitrary remote data out of exception text.
        safe_message = message[:512]
        super().__init__(f"ACP request {method!r} failed" + (f" ({code})" if code is not None else "") + f": {safe_message}")
        self.method = method
        self.code = code
        self.remote_message = safe_message


@dataclass(frozen=True, slots=True)
class ACPEvent:
    """A typed, ordered ACP notification or agent-to-client request.

    ``params`` retains the adapter's typed ACP payload verbatim. For the common
    ``session/update`` event, ``update_kind`` is the ACP ``sessionUpdate`` tag.
    """

    sequence: int
    timestamp: datetime
    method: str
    params: Mapping[str, Any]
    direction: str = "notification"

    @property
    def session_id(self) -> str | None:
        value = self.params.get("sessionId")
        return value if isinstance(value, str) else None

    @property
    def update(self) -> Mapping[str, Any] | None:
        value = self.params.get("update")
        return value if isinstance(value, Mapping) else None

    @property
    def update_kind(self) -> str | None:
        update = self.update
        value = update.get("sessionUpdate") if update is not None else None
        return value if isinstance(value, str) else None


@dataclass(slots=True)
class _PendingPermission:
    session_id: str
    cancelled: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class ACPSession:
    """Controller-facing session metadata for native Codex ACP sessions."""

    session_id: str
    role: str
    task_id: str | int
    cwd: str
    state_directory: str
    capabilities: Mapping[str, Any]
    config_options: tuple[Mapping[str, Any], ...]
    config: Mapping[str, Any]


class ACPGateway:
    """Async JSON-RPC gateway to a persistent ACP adapter subprocess.

    API-key variables and adapter log destinations are removed by default so
    saved Codex ChatGPT authentication is used without an implicit API-billing
    route. Only operating-system and Codex path/locale variables are passed to
    the child. ``codex_path`` can select an installed Codex CLI binary.
    """

    def __init__(
        self,
        command: Sequence[str] = DEFAULT_ADAPTER_COMMAND,
        *,
        codex_path: str | None = None,
        env: Mapping[str, str] | None = None,
        on_event: EventCallback | None = None,
        on_agent_request: AgentRequestHandler | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        cancel_grace: float = DEFAULT_CANCEL_GRACE,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
        max_turn_event_bytes: int = DEFAULT_MAX_TURN_EVENT_BYTES,
    ) -> None:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("command must contain a non-empty executable and arguments")
        if min(request_timeout, turn_timeout, cancel_grace) <= 0:
            raise ValueError("ACP timeouts must be positive")
        if min(max_frame_bytes, max_prompt_bytes, max_turn_event_bytes) <= 0:
            raise ValueError("ACP byte limits must be positive")

        self.command = tuple(command)
        self.codex_path = codex_path
        self._source_env = dict(os.environ if env is None else env)
        self.on_event = on_event
        self.on_agent_request = on_agent_request
        self.request_timeout = request_timeout
        self.turn_timeout = turn_timeout
        self.cancel_grace = cancel_grace
        self.max_frame_bytes = max_frame_bytes
        self.max_prompt_bytes = max_prompt_bytes
        self.max_turn_event_bytes = max_turn_event_bytes

        self.process: asyncio.subprocess.Process | None = None
        self.capabilities: Mapping[str, Any] = {}
        self.agent_info: Mapping[str, Any] = {}
        self.auth_methods: tuple[Mapping[str, Any], ...] = ()
        self.state_directory = self._native_state_directory(self._source_env)

        self._next_request_id = 0
        self._pending: dict[int | str, asyncio.Future[Mapping[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._pending_permissions: dict[int | str, _PendingPermission] = {}
        self._event_sequence = 0
        self._active_prompts: set[str] = set()
        self._active_event_bytes: dict[str, int] = {}
        self._sessions: dict[str, ACPSession] = {}
        self._closing = False

    @staticmethod
    def _native_state_directory(env: Mapping[str, str]) -> str:
        configured_home = env.get("CODEX_HOME")
        if configured_home:
            home = Path(configured_home)
            if not home.is_absolute() and configured_home.startswith("~") and env.get("HOME"):
                home = Path(env["HOME"]) / configured_home.removeprefix("~/")
            else:
                home = home.expanduser()
        else:
            home = Path(env.get("HOME") or str(Path.home())).expanduser() / ".codex"
        return str(home / "sessions")

    @staticmethod
    def _cwd(cwd: str | Path) -> str:
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"ACP working directory does not exist: {path}")
        return str(path)

    def _child_environment(self) -> dict[str, str]:
        # The controller may hold GitHub, Cloudflare, webhook, Vault, operator,
        # and telemetry secrets. Pass only the small runtime environment Codex
        # needs; API keys and custom provider/config/logging routes are excluded.
        child_env = {
            key: value
            for key, value in self._source_env.items()
            if key in _SAFE_ENVIRONMENT_KEYS
        }
        child_env["NO_BROWSER"] = "1"
        # Pass an explicit vetted Codex permission profile instead of inheriting
        # unknown per-user config through an environment override. The profile
        # restricts host reads to Codex's documented minimal runtime set and the
        # session workspace roots.
        child_env["CODEX_CONFIG"] = json.dumps(_CODEX_CONFIG, separators=(",", ":"))
        if self.codex_path:
            child_env["CODEX_PATH"] = self.codex_path
        return child_env

    async def start(self) -> Mapping[str, Any]:
        """Start the adapter and complete ACP v1 initialization once."""
        async with self._lifecycle_lock:
            if self.process is not None and self.process.returncode is None:
                return self.capabilities
            executable = self.command[0]
            if shutil.which(executable, path=self._child_environment().get("PATH")) is None:
                raise ACPProtocolError(f"ACP adapter executable is unavailable: {executable}")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._child_environment(),
                    limit=self.max_frame_bytes + 1,
                )
            except OSError as exc:
                self.process = None
                raise ACPProtocolError(f"could not start ACP adapter: {exc}") from exc

            self._closing = False
            self._reader_task = asyncio.create_task(self._read_stdout(), name="acp-stdout")
            self._stderr_task = asyncio.create_task(self._drain_stderr(), name="acp-stderr")
            try:
                result = await self._rpc_request(
                    "initialize",
                    {
                        "protocolVersion": ACP_PROTOCOL_VERSION,
                        "clientInfo": {"name": "codefactory", "version": "0.1.0"},
                        "clientCapabilities": {},
                    },
                    timeout=self.request_timeout,
                )
                if result.get("protocolVersion") != ACP_PROTOCOL_VERSION:
                    raise ACPProtocolError("ACP adapter did not negotiate protocol version 1")
                capabilities = result.get("agentCapabilities")
                if not isinstance(capabilities, Mapping):
                    raise ACPProtocolError("ACP initialize response has no agentCapabilities object")
                if capabilities.get("loadSession") is not True:
                    raise ACPCapabilityError("ACP adapter must advertise loadSession for restart recovery")
                await self._send_notification("initialized", {})
                self.capabilities = dict(capabilities)
                info = result.get("agentInfo", result.get("agent_info", {}))
                self.agent_info = dict(info) if isinstance(info, Mapping) else {}
                methods = result.get("authMethods", [])
                self.auth_methods = tuple(dict(method) for method in methods if isinstance(method, Mapping))
                return self.capabilities
            except BaseException:
                await self._stop_process()
                raise

    async def close(self) -> None:
        """Stop the adapter process while leaving native Codex sessions on disk."""
        async with self._lifecycle_lock:
            await self._stop_process()

    async def _stop_process(self) -> None:
        self._closing = True
        process = self.process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        self.process = None
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
        background_tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tuple(self._server_request_tasks):
            task.cancel()
        if self._server_request_tasks:
            background_tasks.extend(self._server_request_tasks)
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._server_request_tasks.clear()
        self._pending_permissions.clear()
        self._reader_task = None
        self._stderr_task = None
        self._active_prompts.clear()
        self._active_event_bytes.clear()
        self._fail_pending(ACPProtocolError("ACP adapter process stopped"))

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                return
            # Drain without retaining or logging stderr; it can contain remote
            # errors or other sensitive adapter diagnostics.

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        failure: BaseException | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    if not self._closing:
                        failure = ACPProtocolError("ACP adapter closed its JSON-RPC output")
                    break
                if len(line) > self.max_frame_bytes:
                    failure = ACPProtocolError("ACP adapter frame exceeded the configured byte limit")
                    break
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    failure = ACPProtocolError("ACP adapter emitted invalid JSON-RPC")
                    failure.__cause__ = exc
                    break
                if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
                    failure = ACPProtocolError("ACP adapter emitted a non-JSON-RPC message")
                    break
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, asyncio.LimitOverrunError) as exc:
            failure = ACPProtocolError("ACP adapter frame exceeded the configured byte limit")
            failure.__cause__ = exc
        except BaseException as exc:
            if isinstance(exc, ACPError):
                failure = exc
            else:
                failure = ACPProtocolError("ACP adapter output reader failed")
                failure.__cause__ = exc
        finally:
            if failure is not None:
                self._fail_pending(failure)

    async def _dispatch_message(self, message: Mapping[str, Any]) -> None:
        if "method" not in message:
            request_id = message.get("id")
            future = self._pending.pop(request_id, None) if isinstance(request_id, (int, str)) else None
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, Mapping):
                raw_code = error.get("code")
                code = raw_code if isinstance(raw_code, int) else None
                remote_message = error.get("message")
                if not isinstance(remote_message, str):
                    remote_message = "unknown remote error"
                future.set_exception(ACPRemoteError("JSON-RPC", code, remote_message))
                return
            result = message.get("result", {})
            if not isinstance(result, Mapping):
                future.set_exception(ACPProtocolError("ACP response result must be an object"))
                return
            future.set_result(dict(result))
            return

        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, Mapping):
            raise ACPProtocolError("ACP adapter emitted an invalid method or params object")
        request_id = message.get("id")
        direction = "request" if isinstance(request_id, (int, str)) else "notification"
        if direction == "request" and method == "session/request_permission":
            session_id = params.get("sessionId")
            if isinstance(session_id, str):
                self._pending_permissions[request_id] = _PendingPermission(
                    session_id=session_id,
                    cancelled=asyncio.get_running_loop().create_future(),
                )
        await self._emit_event(method, params, direction)
        if direction == "request":
            task = asyncio.create_task(
                self._handle_agent_request(request_id, method, params),
                name=f"acp-agent-request-{method}",
            )
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_tasks.discard)

    async def _emit_event(self, method: str, params: Mapping[str, Any], direction: str) -> None:
        session_id = params.get("sessionId")
        if isinstance(session_id, str) and session_id in self._active_prompts:
            current_size = len(json.dumps(dict(params), ensure_ascii=False).encode("utf-8"))
            total_size = self._active_event_bytes.get(session_id, 0) + current_size
            self._active_event_bytes[session_id] = total_size
            if total_size > self.max_turn_event_bytes:
                raise ACPTimeoutError("ACP turn exceeded the configured streamed-event byte limit")
        self._event_sequence += 1
        event = ACPEvent(
            sequence=self._event_sequence,
            timestamp=datetime.now(timezone.utc),
            method=method,
            params=dict(params),
            direction=direction,
        )
        if self.on_event is not None:
            result = self.on_event(event)
            if inspect.isawaitable(result):
                await result

    async def _handle_agent_request(self, request_id: int | str, method: str, params: Mapping[str, Any]) -> None:
        if method == "session/request_permission":
            await self._handle_permission_request(request_id, params)
            return
        try:
            if self.on_agent_request is None:
                raise ACPRemoteError(method, -32601, "client request handler is not configured")
            result = self.on_agent_request(method, params)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Mapping):
                raise ACPProtocolError("ACP client request handler must return an object")
            await self._send_message({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})
        except ACPRemoteError as exc:
            await self._send_message(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code or -32601, "message": str(exc)}}
            )
        except Exception:
            await self._send_message(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "client request failed"}}
            )

    async def _handle_permission_request(self, request_id: int | str, params: Mapping[str, Any]) -> None:
        permission = self._pending_permissions.get(request_id)
        try:
            if self.on_agent_request is None:
                result = self._default_permission_result(params)
            else:
                request_task = asyncio.create_task(self._call_request_handler("session/request_permission", params))
                if permission is None:
                    result = await request_task
                else:
                    done, _ = await asyncio.wait(
                        {request_task, permission.cancelled},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if permission.cancelled in done:
                        request_task.cancel()
                        await asyncio.gather(request_task, return_exceptions=True)
                        result = {"outcome": {"outcome": "cancelled"}}
                    else:
                        result = request_task.result()
            result = self._validate_permission_result(params, result)
            await self._send_message({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})
        except Exception:
            # A permission handler failure must never turn into implicit allow.
            await self._send_message(
                {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": {"outcome": "cancelled"}}}
            )
        finally:
            self._pending_permissions.pop(request_id, None)

    async def _call_request_handler(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.on_agent_request is None:
            return {}
        result = self.on_agent_request(method, params)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise ACPProtocolError("ACP client request handler must return an object")
        return result

    @staticmethod
    def _default_permission_result(params: Mapping[str, Any]) -> Mapping[str, Any]:
        options = params.get("options")
        if isinstance(options, list):
            for preferred_kind in ("reject_once", "reject_always"):
                option = next(
                    (
                        item
                        for item in options
                        if isinstance(item, Mapping) and item.get("kind") == preferred_kind
                    ),
                    None,
                )
                if option is not None and isinstance(option.get("optionId"), str):
                    return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}

    @staticmethod
    def _validate_permission_result(
        params: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        outcome = result.get("outcome")
        if not isinstance(outcome, Mapping):
            return {"outcome": {"outcome": "cancelled"}}
        if outcome.get("outcome") == "cancelled":
            return {"outcome": {"outcome": "cancelled"}}
        if outcome.get("outcome") != "selected":
            return {"outcome": {"outcome": "cancelled"}}
        option_id = outcome.get("optionId")
        options = params.get("options")
        if isinstance(option_id, str) and isinstance(options, list) and any(
            isinstance(option, Mapping) and option.get("optionId") == option_id
            for option in options
        ):
            return {"outcome": {"outcome": "selected", "optionId": option_id}}
        return {"outcome": {"outcome": "cancelled"}}

    def _ensure_running(self) -> asyncio.subprocess.Process:
        process = self.process
        if process is None or process.returncode is not None:
            raise ACPProtocolError("ACP adapter is not running; call start() first")
        return process

    async def _send_message(self, message: Mapping[str, Any]) -> None:
        process = self._ensure_running()
        if process.stdin is None:
            raise ACPProtocolError("ACP adapter stdin is unavailable")
        try:
            payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ValueError("ACP messages must contain JSON-compatible values") from exc
        if len(payload) > self.max_frame_bytes:
            raise ValueError("ACP request exceeded the configured frame byte limit")
        async with self._write_lock:
            process.stdin.write(payload)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ACPProtocolError("ACP adapter closed its JSON-RPC input") from exc

    async def _send_notification(self, method: str, params: Mapping[str, Any]) -> None:
        await self._send_message({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def _rpc_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None | object = _TIMEOUT_UNSET,
    ) -> Mapping[str, Any]:
        self._ensure_running()
        self._next_request_id += 1
        request_id = self._next_request_id
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_message(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            effective_timeout = self.request_timeout if timeout is _TIMEOUT_UNSET else timeout
            if effective_timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ACPTimeoutError(f"ACP request {method!r} timed out") from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        except BaseException:
            self._pending.pop(request_id, None)
            raise

    def _fail_pending(self, error: BaseException) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    async def new_session(
        self,
        role: str,
        task_id: str | int,
        cwd: str | Path,
        config: Mapping[str, Any] | None = None,
    ) -> ACPSession:
        """Create a distinct persistent native ACP session for a task role."""
        self._ensure_running()
        if not role.strip():
            raise ValueError("role must not be blank")
        normalized_cwd = self._cwd(cwd)
        normalized_config = self._role_config(role, config)
        result = await self._rpc_request(
            "session/new",
            self._session_params(normalized_cwd),
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ACPProtocolError("ACP session/new response has no sessionId")
        config_options = result.get("configOptions", [])
        if not isinstance(config_options, list):
            config_options = []
        try:
            config_options = await self._apply_config(session_id, config_options, normalized_config)
        except BaseException:
            if self.capabilities.get("sessionCapabilities", {}).get("close") is not None:
                try:
                    await self._rpc_request("session/close", {"sessionId": session_id})
                except ACPError:
                    pass
            raise
        session = ACPSession(
            session_id=session_id,
            role=role.strip(),
            task_id=task_id,
            cwd=normalized_cwd,
            state_directory=self.state_directory,
            capabilities=dict(self.capabilities),
            config_options=tuple(dict(item) for item in config_options if isinstance(item, Mapping)),
            config=normalized_config,
        )
        self._sessions[session_id] = session
        return session

    async def load_session(
        self,
        session_id: str,
        role: str,
        task_id: str | int,
        cwd: str | Path,
        config: Mapping[str, Any] | None = None,
    ) -> ACPSession:
        """Load a native session after gateway/adapter restart, replaying ACP history."""
        self._ensure_running()
        if self.capabilities.get("loadSession") is not True:
            raise ACPCapabilityError("ACP adapter does not support session/load")
        if not session_id:
            raise ValueError("session_id must not be blank")
        if not role.strip():
            raise ValueError("role must not be blank")
        normalized_cwd = self._cwd(cwd)
        normalized_config = self._role_config(role, config)
        result = await self._rpc_request(
            "session/load",
            {"sessionId": session_id, **self._session_params(normalized_cwd)},
        )
        config_options = result.get("configOptions", [])
        if not isinstance(config_options, list):
            config_options = []
        config_options = await self._apply_config(session_id, config_options, normalized_config)
        session = ACPSession(
            session_id=session_id,
            role=role.strip(),
            task_id=task_id,
            cwd=normalized_cwd,
            state_directory=self.state_directory,
            capabilities=dict(self.capabilities),
            config_options=tuple(dict(item) for item in config_options if isinstance(item, Mapping)),
            config=normalized_config,
        )
        self._sessions[session_id] = session
        return session

    @staticmethod
    def _session_params(cwd: str) -> dict[str, Any]:
        return {"cwd": cwd, "mcpServers": []}

    @staticmethod
    def _validate_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
        if config is None:
            return {}
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping")
        unknown = set(config) - {"model", "reasoning_effort", "mode"}
        if unknown:
            raise ACPConfigurationError(f"unsupported ACP session config key(s): {', '.join(sorted(unknown))}")
        normalized: dict[str, Any] = {}
        for key, value in config.items():
            if not isinstance(value, str) or not value.strip():
                raise ACPConfigurationError(f"ACP session config {key!r} must be a non-empty string")
            normalized[key] = value.strip()
        return normalized

    @classmethod
    def _role_config(cls, role: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
        role_key = role.strip().lower().replace(" ", "_")
        expected_mode = _ROLE_MODES.get(role_key, "read-only")
        normalized = cls._validate_config(config)
        expected_values = {
            "model": _DEFAULT_MODEL,
            "reasoning_effort": _DEFAULT_REASONING_EFFORT,
            "mode": expected_mode,
        }
        requested_mode = normalized.get("mode", expected_mode)
        if requested_mode != expected_mode:
            raise ACPConfigurationError(f"role {role!r} requires ACP mode={expected_mode!r}")
        for key, expected in expected_values.items():
            normalized.setdefault(key, expected)
        return normalized

    async def _apply_config(
        self,
        session_id: str,
        config_options: Sequence[Any],
        config: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        options = [dict(option) for option in config_options if isinstance(option, Mapping)]
        for config_key, config_id in (
            ("model", "model"),
            ("reasoning_effort", "reasoning_effort"),
            ("mode", "mode"),
        ):
            desired = config.get(config_key)
            if desired is None:
                continue
            option = next((item for item in options if item.get("id") == config_id), None)
            if option is None:
                raise ACPConfigurationError(f"ACP adapter did not advertise {config_id!r} configuration")
            values = option.get("options")
            supported = {
                item.get("value")
                for item in values
                if isinstance(values, list) and isinstance(item, Mapping)
            } if isinstance(values, list) else set()
            if desired not in supported:
                raise ACPConfigurationError(f"ACP adapter does not offer requested {config_id} value {desired!r}")
            if option.get("currentValue") == desired:
                continue
            result = await self._rpc_request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": desired},
            )
            updated_options = result.get("configOptions")
            if isinstance(updated_options, list):
                options = [dict(item) for item in updated_options if isinstance(item, Mapping)]
        return options

    async def prompt(
        self,
        session_id: str,
        content: str | Sequence[Mapping[str, Any]],
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Send one foreground turn and return its ACP stop reason/result."""
        self._ensure_running()
        if session_id not in self._sessions:
            raise ACPError("session must be created or loaded by this gateway before prompting")
        if session_id in self._active_prompts:
            raise ACPBusyError(f"session {session_id!r} already has an active prompt")
        prompt_blocks = self._normalize_prompt(content)
        prompt_size = len(json.dumps(prompt_blocks, ensure_ascii=False).encode("utf-8"))
        if prompt_size > self.max_prompt_bytes:
            raise ValueError("ACP prompt exceeded the configured byte limit")
        self._active_prompts.add(session_id)
        self._active_event_bytes[session_id] = 0
        turn_timeout = self.turn_timeout if timeout is None else timeout
        if turn_timeout <= 0:
            self._active_prompts.discard(session_id)
            self._active_event_bytes.pop(session_id, None)
            raise ValueError("turn timeout must be positive")
        request_task = asyncio.create_task(
            self._rpc_request(
                "session/prompt",
                {"sessionId": session_id, "prompt": prompt_blocks},
                timeout=None,
            ),
            name=f"acp-prompt-{session_id}",
        )
        try:
            return await asyncio.wait_for(asyncio.shield(request_task), timeout=turn_timeout)
        except TimeoutError as exc:
            await self._cancel_and_settle(session_id, request_task)
            raise ACPTimeoutError(f"ACP turn exceeded its {turn_timeout:g}s timeout") from exc
        except ACPTimeoutError:
            await self._cancel_and_settle(session_id, request_task)
            raise
        except asyncio.CancelledError:
            await self._cancel_and_settle(session_id, request_task)
            raise
        finally:
            self._active_prompts.discard(session_id)
            self._active_event_bytes.pop(session_id, None)

    @staticmethod
    def _normalize_prompt(content: str | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("ACP prompt must not be blank")
            return [{"type": "text", "text": content}]
        if not isinstance(content, Sequence) or not content:
            raise ValueError("ACP prompt must contain at least one content block")
        blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping) or not isinstance(block.get("type"), str):
                raise ValueError("ACP prompt blocks must be typed mappings")
            blocks.append(dict(block))
        return blocks

    async def _cancel_and_settle(
        self,
        session_id: str,
        request_task: asyncio.Task[Mapping[str, Any]],
    ) -> None:
        try:
            await self.cancel(session_id)
        except ACPError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(request_task), timeout=self.cancel_grace)
        except (TimeoutError, ACPError):
            await self._stop_process()

    async def cancel(self, session_id: str) -> None:
        """Interrupt the current foreground turn via ACP ``session/cancel``."""
        if session_id not in self._sessions:
            raise ACPError("cannot cancel a session that is not loaded by this gateway")
        await self._send_notification("session/cancel", {"sessionId": session_id})
        for permission in self._pending_permissions.values():
            if permission.session_id == session_id and not permission.cancelled.done():
                permission.cancelled.set_result(None)

    @property
    def active_sessions(self) -> tuple[ACPSession, ...]:
        return tuple(self._sessions.values())
