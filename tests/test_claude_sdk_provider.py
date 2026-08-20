"""Claude Agent SDK provider - Blueprint 4.1 and 6.2.

Per the owner's decision, this phase builds the adapter fully but verifies it
only against a mocked SDK - no real CLI subprocess, no network call, no API
credentials required. Every test monkeypatches `sdk.query` (and, where a tool
call needs to be simulated, `sdk.create_sdk_mcp_server`) rather than talking to
the real `claude` binary.

The tests exist to prove the security property the module's docstring claims:
the SDK never gets a real tool, and a "tool call" the model makes only ever
reaches an inert handler that records a proposal - never a capability, a file,
or a secret.
"""

from __future__ import annotations

import claude_agent_sdk as sdk
import pytest

from jarvis.agents.claude_sdk_provider import (
    ClaudeAgentSdkProvider,
    ProviderError,
    ProviderUnavailable,
    _build_options,
    _json_schema,
    _mcp_tool_name,
)
from jarvis.agents.factory import UnknownProvider, build_provider
from jarvis.agents.provider import AgentRequest, ProposedToolCall
from jarvis.agents.rule_provider import RuleBasedProvider
from jarvis.config import CoreConfig
from jarvis.core import JarvisCore

LIGHT_CAPABILITY = {
    "name": "home.set_light",
    "description": "Switch a light on or off.",
    "level": "P1",
    "schema": {
        "params": [
            {
                "name": "room",
                "type": "string",
                "required": True,
                "description": "Room name.",
                "choices": None,
            },
            {
                "name": "state",
                "type": "string",
                "required": True,
                "description": "Desired state.",
                "choices": ["on", "off"],
            },
        ]
    },
}


def make_request(**overrides) -> AgentRequest:
    defaults = dict(
        goal="Licht im Office an",
        correlation_id="c1",
        available_capabilities=[LIGHT_CAPABILITY],
    )
    defaults.update(overrides)
    return AgentRequest(**defaults)


class TestJsonSchema:
    def test_converts_params_to_json_schema(self):
        schema = _json_schema(LIGHT_CAPABILITY)
        assert schema["type"] == "object"
        assert schema["properties"]["room"] == {"type": "string", "description": "Room name."}
        assert schema["properties"]["state"]["enum"] == ["on", "off"]
        assert set(schema["required"]) == {"room", "state"}

    def test_optional_param_is_not_required(self):
        cap = {
            "name": "x",
            "description": "d",
            "schema": {
                "params": [
                    {
                        "name": "note",
                        "type": "string",
                        "required": False,
                        "description": "",
                        "choices": None,
                    }
                ]
            },
        }
        schema = _json_schema(cap)
        assert schema["required"] == []
        assert "description" not in schema["properties"]["note"]


class TestInertToolIsolation:
    """The core security property: a 'tool call' only ever records intent."""

    async def test_handler_only_records_a_proposal(self):
        proposals: list[ProposedToolCall] = []
        # We don't need real MCP dispatch to prove inertness: build the same
        # kind of tool the options builder builds and invoke its handler
        # directly.
        from jarvis.agents.claude_sdk_provider import _build_inert_tool

        tool = _build_inert_tool(LIGHT_CAPABILITY, proposals)
        result = await tool.handler({"room": "office", "state": "on"})

        assert proposals == [
            ProposedToolCall(capability="home.set_light", params={"room": "office", "state": "on"})
        ]
        assert "content" in result
        assert "not executed" in result["content"][0]["text"].lower()

    async def test_handler_never_imports_a_capability_handler(self):
        # The module has no reference to CapabilityRegistry or any handler at
        # all - only to capability *descriptions*. This is a structural
        # guarantee, not a runtime one, so we assert it at the import level.
        import jarvis.agents.claude_sdk_provider as mod

        assert "CapabilityRegistry" not in dir(mod)
        assert not hasattr(mod, "registry")

    async def test_repeated_calls_each_append_independently(self):
        proposals: list[ProposedToolCall] = []
        from jarvis.agents.claude_sdk_provider import _build_inert_tool

        tool = _build_inert_tool(LIGHT_CAPABILITY, proposals)
        await tool.handler({"room": "office", "state": "on"})
        await tool.handler({"room": "living", "state": "off"})
        assert len(proposals) == 2


class TestOptionsLockdown:
    """The three-layer restriction described in the module docstring."""

    def test_no_builtin_tools_are_available_at_all(self):
        options = _build_options(
            make_request(), model="m", effort="high", max_turns=4, proposals=[]
        )
        assert options.tools == []

    def test_only_inert_wrappers_are_auto_allowed(self):
        options = _build_options(
            make_request(), model="m", effort="high", max_turns=4, proposals=[]
        )
        assert options.allowed_tools == [_mcp_tool_name("home.set_light")]

    def test_ambient_host_configuration_is_excluded(self):
        options = _build_options(
            make_request(), model="m", effort="high", max_turns=4, proposals=[]
        )
        assert options.strict_mcp_config is True
        assert options.setting_sources == []

    def test_model_effort_and_turn_budget_are_threaded_through(self):
        options = _build_options(
            make_request(), model="claude-opus-5", effort="max", max_turns=2, proposals=[]
        )
        assert options.model == "claude-opus-5"
        assert options.effort == "max"
        assert options.max_turns == 2

    def test_mcp_server_is_named_jarvis(self):
        options = _build_options(
            make_request(), model="m", effort="high", max_turns=4, proposals=[]
        )
        assert set(options.mcp_servers) == {"jarvis"}

    def test_no_capabilities_means_no_allowed_tools(self):
        options = _build_options(
            make_request(available_capabilities=[]),
            model="m",
            effort="high",
            max_turns=4,
            proposals=[],
        )
        assert options.allowed_tools == []


class FakeQuery:
    """Replaces `sdk.query`. Never touches a CLI, a subprocess, or a network."""

    def __init__(self, messages=None, error: Exception | None = None):
        self.messages = messages or []
        self.error = error
        self.received_options: sdk.ClaudeAgentOptions | None = None
        self.received_prompt: str | None = None

    async def __call__(self, *, prompt, options, **_):
        self.received_prompt = prompt
        self.received_options = options
        for message in self.messages:
            yield message
        if self.error is not None:
            raise self.error


class TestPlanMessageHandling:
    async def test_text_blocks_become_the_summary(self, monkeypatch):
        fake = FakeQuery(
            messages=[
                sdk.AssistantMessage(
                    content=[sdk.TextBlock(text="I will switch the light on.")],
                    model="claude-opus-5",
                ),
                sdk.ResultMessage(
                    subtype="success",
                    duration_ms=10,
                    duration_api_ms=8,
                    is_error=False,
                    num_turns=1,
                    session_id="s1",
                    total_cost_usd=0.02,
                ),
            ]
        )
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        provider = ClaudeAgentSdkProvider()
        response = await provider.plan(make_request(), model="claude-opus-5", effort="high")

        assert response.summary == "I will switch the light on."
        assert response.cost_units == 0.02
        assert response.model == "claude-opus-5"
        assert response.tool_calls == ()
        assert response.done is True

    async def test_thinking_blocks_are_discarded_not_forwarded(self, monkeypatch):
        fake = FakeQuery(
            messages=[
                sdk.AssistantMessage(
                    content=[
                        sdk.ThinkingBlock(thinking="secret chain of thought", signature="sig"),
                        sdk.TextBlock(text="Done."),
                    ],
                    model="m",
                ),
            ]
        )
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        response = await ClaudeAgentSdkProvider().plan(make_request(), model="m", effort="low")
        assert "secret chain of thought" not in response.summary
        assert response.summary == "Done."

    async def test_a_proposed_tool_call_survives_into_the_response(self, monkeypatch):
        """Simulates the model invoking our inert wrapper via the real
        registered handler, the way the SDK's own transport would - proving
        `plan()` picks up whatever the handler recorded."""
        captured_tools = {}

        def fake_create_server(*, name, tools):
            for t in tools:
                captured_tools[t.name] = t
            return {"type": "sdk", "name": name, "tools": tools}

        async def fake_query(*, prompt, options, **_):
            # The "model" calls the one tool it was given.
            tool = captured_tools["home.set_light"]
            await tool.handler({"room": "office", "state": "on"})
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Proposed.")], model="m")

        monkeypatch.setattr(
            "jarvis.agents.claude_sdk_provider.sdk.create_sdk_mcp_server", fake_create_server
        )
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake_query)

        response = await ClaudeAgentSdkProvider().plan(make_request(), model="m", effort="high")
        assert response.tool_calls == (
            ProposedToolCall(capability="home.set_light", params={"room": "office", "state": "on"}),
        )
        assert response.done is False

    async def test_cli_not_found_becomes_provider_unavailable(self, monkeypatch):
        fake = FakeQuery(error=sdk.CLINotFoundError("claude CLI not found"))
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        with pytest.raises(ProviderUnavailable):
            await ClaudeAgentSdkProvider().plan(make_request(), model="m", effort="low")

    async def test_connection_failure_becomes_provider_unavailable(self, monkeypatch):
        fake = FakeQuery(error=sdk.CLIConnectionError("transport closed"))
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        with pytest.raises(ProviderUnavailable):
            await ClaudeAgentSdkProvider().plan(make_request(), model="m", effort="low")

    async def test_an_sdk_error_result_becomes_provider_error(self, monkeypatch):
        fake = FakeQuery(
            messages=[
                sdk.ResultMessage(
                    subtype="error_during_execution",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=True,
                    num_turns=1,
                    session_id="s1",
                    result="something went wrong",
                )
            ]
        )
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        with pytest.raises(ProviderError):
            await ClaudeAgentSdkProvider().plan(make_request(), model="m", effort="low")

    async def test_no_text_and_no_tools_still_returns_a_response(self, monkeypatch):
        fake = FakeQuery(messages=[])
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        response = await ClaudeAgentSdkProvider().plan(make_request(), model="m", effort="low")
        assert response.summary == "No response text."
        assert response.done is True


class TestProviderFactory:
    def test_rules_is_the_default_and_needs_no_sdk(self):
        assert isinstance(build_provider("rules"), RuleBasedProvider)

    def test_claude_agent_sdk_is_selectable(self):
        assert isinstance(build_provider("claude-agent-sdk"), ClaudeAgentSdkProvider)

    def test_unknown_provider_name_is_rejected(self):
        with pytest.raises(UnknownProvider):
            build_provider("gpt-whatever")

    def test_core_config_defaults_to_rules(self):
        assert CoreConfig().provider == "rules"

    def test_core_wires_the_configured_provider_by_default(self, tmp_path):
        config = CoreConfig(db_path=str(tmp_path / "j.db"), provider="claude-agent-sdk")
        core = JarvisCore(config)
        assert isinstance(core.coordinator._provider, ClaudeAgentSdkProvider)

    def test_an_explicit_provider_argument_overrides_the_config(self, tmp_path):
        config = CoreConfig(db_path=str(tmp_path / "j.db"), provider="claude-agent-sdk")
        core = JarvisCore(config, provider=RuleBasedProvider())
        assert isinstance(core.coordinator._provider, RuleBasedProvider)


class TestCoordinatorIntegration:
    """Proves the new provider composes with the existing security machinery
    without needing to re-test the machinery itself (already covered by
    tests/test_agent_path.py)."""

    async def test_provider_unavailable_stops_the_run_gracefully(self, config, monkeypatch):
        fake = FakeQuery(error=sdk.CLIConnectionError("no CLI on this box"))
        monkeypatch.setattr("jarvis.agents.claude_sdk_provider.sdk.query", fake)

        core = JarvisCore(config, provider=ClaudeAgentSdkProvider())
        await core.start()
        try:
            from jarvis.capability.models import ExecutionContext

            mission = await core.missions.create("do something")
            core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
            run = await core.coordinator.run(
                "do something",
                ExecutionContext(
                    correlation_id=mission.correlation_id, mission_id=mission.mission_id
                ),
            )
            assert run.stopped_reason == "provider_error:ProviderUnavailable"
            assert run.executions == []
        finally:
            await core.stop()
