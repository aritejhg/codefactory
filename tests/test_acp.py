from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from codefactory.acp import (
    ACPConfigurationError,
    ACPCapabilityError,
    ACPEvent,
    ACPGateway,
    ACPTimeoutError,
)


FAKE_ADAPTER = r'''
import json
import sys

state_path = sys.argv[1]
fake_load = sys.argv[2]
fake_auto_finish = sys.argv[3]
fake_permission = sys.argv[4]
try:
    with open(state_path, encoding="utf-8") as state_file:
        sessions = json.load(state_file)
except FileNotFoundError:
    sessions = {}
active = {}
permissions = {}

def options(session):
    return [
        {"id": "model", "currentValue": session.get("model", "gpt-5.6-sol"),
         "options": [{"value": "gpt-5.6-sol"}, {"value": "gpt-5.6-luna"}]},
        {"id": "reasoning_effort", "currentValue": session.get("reasoning_effort", "medium"),
         "options": [{"value": "high"}, {"value": "xhigh"}]},
        {"id": "mode", "currentValue": session.get("mode", "agent"),
         "options": [{"value": "agent"}, {"value": "read-only"}, {"value": "workspace-write"}]},
    ]

def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def save():
    with open(state_path, "w", encoding="utf-8") as state_file:
        json.dump(sessions, state_file)

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    params = message.get("params", {})
    request_id = message.get("id")
    if method is None and request_id in permissions:
        session_id = permissions.pop(request_id)
        outcome = message.get("result", {}).get("outcome", {})
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "update": {
                "sessionUpdate": "permission_result", "outcome": outcome,
            }
        }})
        active_id = active.pop(session_id, None)
        if active_id is not None:
            emit({"jsonrpc": "2.0", "id": active_id, "result": {"stopReason": "end_turn"}})
    elif method == "initialize":
        load = fake_load == "1"
        caps = {"loadSession": load, "sessionCapabilities": {"close": {}}}
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": 1, "agentCapabilities": caps,
            "agentInfo": {"name": "test-adapter"},
            "authMethods": [{"id": "chatgpt", "name": "ChatGPT"}],
        }})
    elif method == "initialized":
        continue
    elif method == "session/new":
        session_id = "fake-session-" + str(len(sessions) + 1)
        sessions[session_id] = {
            "history": [], "model": "gpt-5.6-sol", "reasoning_effort": "medium", "mode": "agent"
        }
        save()
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "sessionId": session_id, "configOptions": options(sessions[session_id])
        }})
    elif method == "session/set_config_option":
        session = sessions[params["sessionId"]]
        session[params["configId"]] = params["value"]
        save()
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": options(session)}})
    elif method == "session/prompt":
        session = sessions[params["sessionId"]]
        prompt = params["prompt"]
        session["history"].append(prompt)
        save()
        active[params["sessionId"]] = request_id
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": params["sessionId"], "update": {
                "sessionUpdate": "agent_thought_chunk", "messageId": "m1",
                "content": {"type": "text", "text": "working"},
            }
        }})
        if fake_permission == "request":
            permissions[900] = params["sessionId"]
            emit({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
                "sessionId": params["sessionId"], "toolCall": {"toolCallId": "permission-tool"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            }})
        elif fake_auto_finish == "1":
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": params["sessionId"], "update": {
                    "sessionUpdate": "agent_message_chunk", "messageId": "m2",
                    "content": {"type": "text", "text": "done"},
                }
            }})
            emit({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
            active.pop(params["sessionId"], None)
    elif method == "session/cancel":
        active_id = active.pop(params["sessionId"], None)
        if active_id is not None:
            emit({"jsonrpc": "2.0", "id": active_id, "result": {"stopReason": "cancelled"}})
    elif method == "session/load":
        session = sessions.get(params["sessionId"], {"history": []})
        for index, blocks in enumerate(session.get("history", [])):
            for block in blocks:
                if block.get("type") == "text":
                    emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": params["sessionId"], "update": {
                            "sessionUpdate": "user_message_chunk", "messageId": "replayed-" + str(index),
                            "content": block,
                        }
                    }})
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": options(session)}})
    elif method == "session/close":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "authentication/status":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"type": "chat-gpt", "email": "not-printed"}})
'''


def gateway(
    tmp_path: Path,
    *,
    events: list[ACPEvent] | None = None,
    env_overrides: dict[str, str] | None = None,
    **kwargs: Any,
) -> ACPGateway:
    tmp_path.mkdir(parents=True, exist_ok=True)
    adapter_path = tmp_path / "fake_acp_adapter.py"
    adapter_path.write_text(FAKE_ADAPTER, encoding="utf-8")
    load = "1"
    auto_finish = "1"
    fake_permission = "none"
    safe_overrides = dict(env_overrides or {})
    load = safe_overrides.pop("FAKE_LOAD", load)
    auto_finish = safe_overrides.pop("FAKE_AUTO_FINISH", auto_finish)
    fake_permission = safe_overrides.pop("FAKE_PERMISSION", fake_permission)
    env = {"PATH": str(Path(sys.executable).parent), "HOME": str(tmp_path)}
    env.update(safe_overrides)
    callback = kwargs.pop("on_event", None)
    if callback is None and events is not None:
        callback = events.append
    return ACPGateway(
        (
            sys.executable,
            str(adapter_path),
            str(tmp_path / "native-state.json"),
            load,
            auto_finish,
            fake_permission,
        ),
        env=env,
        on_event=callback,
        **kwargs,
    )


def test_gateway_initializes_creates_role_sessions_configures_and_streams_events(tmp_path: Path):
    async def run() -> None:
        events: list[ACPEvent] = []
        client = gateway(tmp_path, events=events)
        caps = await client.start()
        assert caps["loadSession"] is True
        planner = await client.new_session(
            "planner", 41, tmp_path, {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}
        )
        implementer = await client.new_session("implementer", 41, tmp_path)
        assert planner.session_id != implementer.session_id
        assert planner.role == "planner"
        assert planner.state_directory == str(tmp_path / ".codex" / "sessions")
        assert {option["id"]: option["currentValue"] for option in planner.config_options} == {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "xhigh",
            "mode": "read-only",
        }

        result = await client.prompt(planner.session_id, "A short smoke turn")
        assert result["stopReason"] == "end_turn"
        assert [event.update_kind for event in events if event.method == "session/update"] == [
            "agent_thought_chunk",
            "agent_message_chunk",
        ]
        assert all(event.timestamp.tzinfo is not None for event in events)
        assert [event.sequence for event in events] == sorted(event.sequence for event in events)
        await client.close()

    asyncio.run(run())


def test_gateway_loads_existing_session_and_replays_typed_history_after_restart(tmp_path: Path):
    async def run() -> None:
        first = gateway(tmp_path)
        await first.start()
        session = await first.new_session("planner", "task-42", tmp_path)
        await first.prompt(session.session_id, "Remember marker ACP-REPLAY-42")
        await first.close()

        replayed: list[ACPEvent] = []
        restarted = gateway(tmp_path, events=replayed)
        await restarted.start()
        loaded = await restarted.load_session(
            session.session_id, "planner", "task-42", tmp_path
        )
        replay = [event for event in replayed if event.method == "session/update"]
        assert loaded.session_id == session.session_id
        assert any(
            event.update_kind == "user_message_chunk"
            and "ACP-REPLAY-42" in json.dumps(event.update)
            for event in replay
        )
        await restarted.close()

    asyncio.run(run())


def test_cancel_sends_acp_interrupt_and_waits_for_turn_completion(tmp_path: Path):
    async def run() -> None:
        started = asyncio.Event()

        async def on_event(event: ACPEvent) -> None:
            if event.update_kind == "agent_thought_chunk":
                started.set()

        client = gateway(
            tmp_path,
            on_event=on_event,
            env_overrides={"FAKE_AUTO_FINISH": "0"},
        )
        await client.start()
        session = await client.new_session("implementer", 9, tmp_path)
        prompt_task = asyncio.create_task(client.prompt(session.session_id, "Long running turn"))
        await asyncio.wait_for(started.wait(), timeout=2)
        await client.cancel(session.session_id)
        result = await asyncio.wait_for(prompt_task, timeout=2)
        assert result["stopReason"] == "cancelled"
        await client.close()

    asyncio.run(run())


def test_gateway_fails_closed_for_missing_load_or_unadvertised_model(tmp_path: Path):
    async def run() -> None:
        unsupported = gateway(tmp_path, env_overrides={"FAKE_LOAD": "0"})
        with pytest.raises(ACPCapabilityError):
            await unsupported.start()

        configured = gateway(tmp_path / "configured")
        await configured.start()
        with pytest.raises(ACPConfigurationError):
            await configured.new_session(
                "planner", 1, tmp_path, {"model": "unavailable-model"}
            )
        await configured.close()

    asyncio.run(run())


def test_gateway_strips_api_keys_by_default_and_bounds_prompt_size(tmp_path: Path):
    client = gateway(
        tmp_path,
        env_overrides={
            "OPENAI_API_KEY": "not-a-real-key",
            "CODEX_API_KEY": "not-a-real-key",
            "GITHUB_TOKEN": "sentinel",
            "CLOUDFLARE_API_TOKEN": "sentinel",
            "VAULT_TOKEN": "sentinel",
            "CODEFACTORY_WEBHOOK_SECRET": "sentinel",
            "OTEL_EXPORTER_OTLP_HEADERS": "sentinel",
            "INITIAL_AGENT_MODE": "read-only",
        },
        max_prompt_bytes=32,
    )
    child_env = client._child_environment()
    assert "OPENAI_API_KEY" not in child_env
    assert "CODEX_API_KEY" not in child_env
    assert "GITHUB_TOKEN" not in child_env
    assert "CLOUDFLARE_API_TOKEN" not in child_env
    assert "VAULT_TOKEN" not in child_env
    assert "CODEFACTORY_WEBHOOK_SECRET" not in child_env
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in child_env
    assert "CLOUDFLARE_API_TOKEN" not in child_env
    assert "VAULT_TOKEN" not in child_env
    assert "INITIAL_AGENT_MODE" not in child_env
    assert json.loads(child_env["CODEX_CONFIG"])["default_permissions"] == "workspace-only"
    assert child_env["NO_BROWSER"] == "1"

    async def run() -> None:
        await client.start()
        session = await client.new_session("reviewer", 22, tmp_path)
        with pytest.raises(ValueError, match="byte limit"):
            await client.prompt(session.session_id, "x" * 100)
        await client.close()

    asyncio.run(run())


def test_permission_requests_default_to_reject_and_emit_typed_request(tmp_path: Path):
    async def run() -> None:
        events: list[ACPEvent] = []
        client = gateway(
            tmp_path,
            events=events,
            env_overrides={"FAKE_PERMISSION": "request"},
        )
        await client.start()
        session = await client.new_session("implementer", "task-17", tmp_path)

        result = await client.prompt(session.session_id, "Run a gated operation")

        permission_request = next(event for event in events if event.method == "session/request_permission")
        permission_result = next(event for event in events if event.update_kind == "permission_result")
        assert permission_request.direction == "request"
        assert permission_request.session_id == session.session_id
        assert permission_result.update == {
            "sessionUpdate": "permission_result",
            "outcome": {"outcome": "selected", "optionId": "reject-once"},
        }
        assert result["stopReason"] == "end_turn"
        await client.close()

    asyncio.run(run())


def test_permission_handler_cannot_select_an_unoffered_option(tmp_path: Path):
    async def run() -> None:
        async def handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "session/request_permission"
            return {"outcome": {"outcome": "selected", "optionId": "not-offered"}}

        events: list[ACPEvent] = []
        client = gateway(
            tmp_path,
            events=events,
            env_overrides={"FAKE_PERMISSION": "request"},
            on_agent_request=handler,
        )
        await client.start()
        session = await client.new_session("implementer", "task-18", tmp_path)
        await client.prompt(session.session_id, "Ask for permission")
        permission_result = next(event for event in events if event.update_kind == "permission_result")
        assert permission_result.update == {
            "sessionUpdate": "permission_result",
            "outcome": {"outcome": "cancelled"},
        }
        await client.close()

    asyncio.run(run())


def test_cancel_cancels_outstanding_permission_handler(tmp_path: Path):
    async def run() -> None:
        permission_seen = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
            permission_seen.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                handler_cancelled.set()
                raise

        events: list[ACPEvent] = []
        client = gateway(
            tmp_path,
            events=events,
            env_overrides={"FAKE_PERMISSION": "request", "FAKE_AUTO_FINISH": "0"},
            on_agent_request=handler,
        )
        await client.start()
        session = await client.new_session("implementer", "task-19", tmp_path)
        prompt_task = asyncio.create_task(client.prompt(session.session_id, "Ask then cancel"))
        await asyncio.wait_for(permission_seen.wait(), timeout=2)
        await client.cancel(session.session_id)
        result = await asyncio.wait_for(prompt_task, timeout=2)
        assert result["stopReason"] == "cancelled"
        assert handler_cancelled.is_set()
        permission_result = next(event for event in events if event.update_kind == "permission_result")
        assert permission_result.update["outcome"] == {"outcome": "cancelled"}
        await client.close()

    asyncio.run(run())
