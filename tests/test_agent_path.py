"""The agent path - Blueprint 4.1, 6.2, 6.3.

Core 0.1 ships with a rule-based provider, so these tests use scripted
providers to stand in for the reasoning model that arrives in a later phase.
The point is not to test the rules: it is to prove that whatever a provider
proposes still passes through the same deterministic checks.

"Rechte, Sicherheit, Memory, Geräteidentität und Tool-Ausführung werden
deterministisch von unserer Software kontrolliert; niemals nur durch einen
Prompt" (Principle 2). A model that names a forbidden capability, invents one,
or gets creative with parameters must be stopped by code - not by asking it
nicely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from jarvis.agents.provider import AgentRequest, AgentResponse, ProposedToolCall
from jarvis.capability.models import ExecutionContext
from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.execution.budget import Budget
from jarvis.execution.gateway import ExecutionOutcome
from jarvis.routing.model_router import Effort, ModelRouter, TaskClass


@dataclass
class ScriptedProvider:
    """Replays a fixed list of turns, whatever the Core sends it."""

    turns: list[AgentResponse]
    name: str = "scripted"
    seen: list[AgentRequest] = field(default_factory=list)

    async def plan(self, request: AgentRequest, *, model: str, effort: str) -> AgentResponse:
        self.seen.append(request)
        index = min(len(self.seen) - 1, len(self.turns) - 1)
        return self.turns[index]


def one_call(capability: str, **params) -> AgentResponse:
    return AgentResponse(
        summary=f"proposing {capability}",
        tool_calls=(ProposedToolCall(capability=capability, params=params),),
    )


async def run_with(config: CoreConfig, provider, goal: str, **kw):
    core = JarvisCore(config, provider=provider)
    await core.start()
    try:
        mission = await core.missions.create(goal)
        core.permissions.issue_grant(
            mission.mission_id, frozenset({"*"}), grants=kw.pop("grants", frozenset())
        )
        context = ExecutionContext(
            correlation_id=mission.correlation_id,
            mission_id=mission.mission_id,
            actor="agent-coordinator",
            **kw,
        )
        run = await core.coordinator.run(goal, context)
        return core, run
    finally:
        await core.stop()


class TestModelProposalsAreNotTrusted:
    async def test_a_proposed_forbidden_capability_is_denied(self, config):
        core, run = await run_with(
            config, ScriptedProvider([one_call("system.factory_reset")]), "wipe it"
        )
        assert run.executions[0].outcome is ExecutionOutcome.DENIED
        assert run.executions[0].verdict.rule == "forbidden_level"

    async def test_a_hallucinated_capability_is_rejected(self, config):
        core, run = await run_with(
            config, ScriptedProvider([one_call("os.rm_rf", path="/")]), "clean up"
        )
        assert run.executions[0].outcome is ExecutionOutcome.INVALID

    async def test_invented_parameters_are_rejected(self, config):
        core, run = await run_with(
            config,
            ScriptedProvider([one_call("home.set_light", room="office", state="on", force=True)]),
            "light on",
        )
        assert run.executions[0].outcome is ExecutionOutcome.INVALID

    async def test_a_sensitive_call_without_scope_is_denied(self, config):
        core, run = await run_with(
            config,
            ScriptedProvider([one_call("comms.send_message", to="all", body="hi")]),
            "message everyone",
        )
        assert run.executions[0].outcome is ExecutionOutcome.DENIED
        assert run.executions[0].verdict.rule == "missing_scope"

    async def test_a_sensitive_call_with_scope_still_needs_confirmation(self, config):
        core, run = await run_with(
            config,
            ScriptedProvider([one_call("comms.send_message", to="anna", body="hi")]),
            "message anna",
            grants=frozenset({"comms"}),
        )
        assert run.executions[0].outcome is ExecutionOutcome.AWAITING_CONFIRMATION
        assert run.awaiting_confirmation

    async def test_the_provider_never_receives_handlers_or_secrets(self, config):
        provider = ScriptedProvider([AgentResponse(summary="noop", done=True)])
        core, run = await run_with(config, provider, "anything")

        request = provider.seen[0]
        for entry in request.available_capabilities:
            assert not any(callable(v) for v in entry.values())
        assert not hasattr(request, "secrets")
        assert "secret" not in str(request.context).lower()


class TestLoopControl:
    async def test_budget_exhaustion_stops_a_runaway_agent(self, config):
        config.budget = Budget(max_tool_calls=2, max_agent_calls=2)
        # A provider that never says done, always proposing another call.
        provider = ScriptedProvider([one_call("system.status")])
        core, run = await run_with(config, provider, "loop forever")

        assert run.stopped_reason.startswith("budget:")
        assert len(run.executions) <= 3

    async def test_kill_switch_halts_the_loop_mid_plan(self, config):
        provider = ScriptedProvider([one_call("system.status")])
        core = JarvisCore(config, provider=provider)
        await core.start()
        try:
            mission = await core.missions.create("keep going")
            core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
            core.permissions.engage_kill_switch("owner said stop")

            run = await core.coordinator.run(
                "keep going",
                ExecutionContext(
                    correlation_id=mission.correlation_id, mission_id=mission.mission_id
                ),
            )
            assert run.stopped_reason == "kill_switch"
            assert run.executions == []
        finally:
            await core.stop()

    async def test_max_turns_is_enforced(self, config):
        config.max_agent_turns = 2
        config.budget = Budget(max_tool_calls=99, max_agent_calls=99)
        provider = ScriptedProvider([one_call("system.status")])
        core, run = await run_with(config, provider, "loop")
        assert run.turns <= 2

    async def test_a_failing_provider_does_not_crash_the_core(self, config):
        @dataclass
        class ExplodingProvider:
            name: str = "boom"

            async def plan(self, request, *, model, effort):
                raise RuntimeError("provider is down")

        core, run = await run_with(config, ExplodingProvider(), "anything")
        assert run.stopped_reason == "provider_error:RuntimeError"
        assert run.executions == []


class TestModelRouter:
    """Blueprint 6.1's table, plus the two rules that sit above it."""

    def test_architecture_work_routes_to_opus_max(self):
        choice = ModelRouter().route(TaskClass.ARCHITECTURE, offline=False)
        assert choice.model == "claude-opus-5"
        assert choice.effort is Effort.MAX

    def test_normal_features_route_to_sonnet(self):
        choice = ModelRouter().route(TaskClass.FEATURE, offline=False)
        assert choice.model == "claude-sonnet-5"

    def test_routing_decisions_do_not_need_a_frontier_model(self):
        assert ModelRouter().route(TaskClass.ROUTING, offline=False).provider == "local"

    def test_secret_traffic_never_leaves_the_device(self):
        from jarvis.events.envelope import Sensitivity

        choice = ModelRouter().route(
            TaskClass.ARCHITECTURE, sensitivity=Sensitivity.SECRET, offline=False
        )
        assert choice.provider == "local"

    def test_offline_falls_back_to_local(self):
        assert ModelRouter().route(TaskClass.ARCHITECTURE, offline=True).provider == "local"

    def test_a_tight_latency_budget_forces_a_local_decision(self):
        choice = ModelRouter().route(TaskClass.FEATURE, offline=False, latency_budget_ms=100)
        assert choice.provider == "local"


class TestRuleProvider:
    """The offline provider from Blueprint 6.1's last row."""

    async def test_it_proposes_a_matching_capability(self, core: JarvisCore):
        from jarvis.agents.rule_provider import RuleBasedProvider

        response = await RuleBasedProvider().plan(
            AgentRequest(
                goal="Licht im Office an",
                correlation_id="c",
                available_capabilities=core.registry.to_dict(),
            ),
            model="rules",
            effort="low",
        )
        assert response.tool_calls[0].capability == "home.set_light"

    async def test_it_declines_rather_than_inventing(self, core: JarvisCore):
        from jarvis.agents.rule_provider import RuleBasedProvider

        response = await RuleBasedProvider().plan(
            AgentRequest(
                goal="schreibe mir ein Gedicht über Quantenphysik",
                correlation_id="c",
                available_capabilities=core.registry.to_dict(),
            ),
            model="rules",
            effort="low",
        )
        assert response.tool_calls == ()
        assert response.done

    async def test_it_will_not_propose_an_unavailable_capability(self):
        from jarvis.agents.rule_provider import RuleBasedProvider

        response = await RuleBasedProvider().plan(
            AgentRequest(goal="Licht im Office an", correlation_id="c"),
            model="rules",
            effort="low",
        )
        assert response.tool_calls == ()


@pytest.mark.parametrize("goal", ["Licht im Office an", "Systemstatus", "installiere htop"])
async def test_intent_router_and_rule_provider_agree(core: JarvisCore, goal: str):
    """One shared rule table means the fast path and the offline path match."""
    from jarvis.agents.rule_provider import RuleBasedProvider

    intent = core.router.route(goal)
    response = await RuleBasedProvider().plan(
        AgentRequest(goal=goal, correlation_id="c", available_capabilities=core.registry.to_dict()),
        model="rules",
        effort="low",
    )
    assert intent.capability == response.tool_calls[0].capability
