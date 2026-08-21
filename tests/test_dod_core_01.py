"""Exit criteria for Core 0.1 - Blueprint 5.4, "Definition of Done".

One test class per checklist item, in the blueprint's own order:

    1. Textkommando erreicht Core über lokale API/WebSocket.
    2. Intent Router wählt zwischen lokalem Mock-Tool und Claude-Agent.
    3. Permission Engine blockiert/erlaubt/fordert Bestätigung.
    4. Tool Registry führt mindestens drei Mock-Tools aus.
    5. Jede Aktion erzeugt Events und Audit-Logs.
    6. Mission bleibt nach Prozessneustart erhalten.
    7. Verifier unterscheidet "Tool wurde aufgerufen" von "Ziel erreicht".
    8. Keine UI außer minimalem Debug-Dashboard ist für diesen Meilenstein nötig.

These are acceptance tests: they drive the Core the way the owner would, and
assert on observable outcomes rather than internals.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis.api.server import create_app
from jarvis.core import JarvisCore
from jarvis.events import types as ev
from jarvis.execution.gateway import ExecutionOutcome
from jarvis.intent.router import Route
from jarvis.mission.model import MissionState, TaskState


class TestCriterion1LocalApi:
    """Textkommando erreicht Core über lokale API/WebSocket."""

    def test_command_over_http(self, config):
        with TestClient(create_app(config=config)) as client:
            response = client.post(
                "/command", json={"text": "Licht im Office an", "device_id": "desk-01"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["mission_state"] == str(MissionState.COMPLETED)

    def test_events_stream_over_websocket(self, config):
        with TestClient(create_app(config=config)) as client, client.websocket_connect("/ws") as ws:
            client.post("/command", json={"text": "Licht im Office an"})
            seen = [json.loads(ws.receive_text())["type"] for _ in range(4)]
            assert ev.COMMAND_RECEIVED in seen

    def test_api_binds_to_loopback_by_default(self, config):
        # Blueprint 7.2: no admin port openly exposed to the internet.
        assert config.host == "127.0.0.1"


class TestCriterion2IntentRouter:
    """Intent Router wählt zwischen lokalem Mock-Tool und Claude-Agent."""

    def test_known_phrase_routes_to_a_local_tool(self, core: JarvisCore):
        intent = core.router.route("Licht im Office an")
        assert intent.route is Route.LOCAL_TOOL
        assert intent.capability == "home.set_light"
        assert intent.needs_model is False

    def test_open_ended_request_routes_to_the_agent(self, core: JarvisCore):
        intent = core.router.route("Plane bitte meinen Umzug nach Lissabon")
        assert intent.route is Route.AGENT
        assert intent.needs_model is True

    def test_stop_phrase_routes_to_control_without_a_model(self, core: JarvisCore):
        intent = core.router.route("Jarvis, stopp alles")
        assert intent.route is Route.CONTROL
        assert intent.needs_model is False

    async def test_agent_route_actually_reaches_the_coordinator(self, core: JarvisCore):
        result = await core.handle_command("Plane bitte meinen Umzug nach Lissabon")
        assert result.agent_run is not None
        assert result.agent_run.turns >= 1

    async def test_a_local_rule_wins_over_the_agent(self, core: JarvisCore):
        # "installiere htop" is a known phrase, so it dispatches locally and is
        # gated on its scope - it never costs a model call.
        result = await core.handle_command("Bitte installiere htop")
        assert result.agent_run is None
        assert result.execution is not None
        assert result.execution.capability == "system.install_software"


class TestCriterion3PermissionEngine:
    """Permission Engine blockiert/erlaubt/fordert Bestätigung."""

    async def test_allows_a_safe_action(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        assert result.execution is not None
        assert result.execution.outcome is ExecutionOutcome.EXECUTED

    async def test_requires_confirmation_for_a_sensitive_action(self, core: JarvisCore):
        result = await core.handle_command(
            "Nachricht an anna: bin unterwegs", grants=frozenset({"comms"})
        )
        assert result.execution is not None
        assert result.execution.outcome is ExecutionOutcome.AWAITING_CONFIRMATION
        assert result.mission_state == str(MissionState.WAITING_FOR_APPROVAL)
        assert result.pending_approval is not None

    async def test_blocks_a_forbidden_action(self, core: JarvisCore):
        result = await core.handle_command("Mach einen factory reset")
        assert result.execution is not None
        assert result.execution.outcome is ExecutionOutcome.DENIED
        assert result.mission_state == str(MissionState.BLOCKED)

    async def test_forbidden_handler_is_never_reached(self, core: JarvisCore):
        # The P6 mock handler raises if dispatched; a denial means it was not.
        result = await core.handle_command("Mach einen factory reset")
        assert result.execution is not None
        assert result.execution.outcome is not ExecutionOutcome.FAILED

    async def test_sensitive_action_without_its_scope_is_denied(self, core: JarvisCore):
        result = await core.handle_command("Nachricht an anna: hallo")
        assert result.execution is not None
        assert result.execution.outcome is ExecutionOutcome.DENIED
        assert "grant" in result.execution.detail

    async def test_approval_completes_the_pending_action(self, core: JarvisCore):
        first = await core.handle_command(
            "Nachricht an anna: bin unterwegs", grants=frozenset({"comms"})
        )
        assert first.pending_approval is not None

        approved = await core.approve(first.pending_approval["fingerprint"])
        assert approved.mission_state == str(MissionState.COMPLETED)
        assert any(m["to"] == "anna" for m in core.world.outbox)

        # The task itself must reach DONE, not just the mission as a whole -
        # a stale PENDING task under a COMPLETED mission is exactly the kind
        # of inconsistency the HUD's progress bar would otherwise report as
        # "0/1 done" on a finished mission.
        mission = await core.missions.load(approved.mission_id)
        assert [t.state for t in mission.tasks] == [TaskState.DONE]

        progress = core.runner.progress(mission)
        assert progress["tasks_done"] == 1
        assert progress["fraction_done"] == 1.0

    async def test_denial_cancels_the_mission_and_sends_nothing(self, core: JarvisCore):
        first = await core.handle_command(
            "Nachricht an anna: bin unterwegs", grants=frozenset({"comms"})
        )
        denied = await core.deny(first.pending_approval["fingerprint"])
        assert denied.mission_state == str(MissionState.CANCELED)
        assert core.world.outbox == []

    async def test_kill_switch_stops_subsequent_work(self, core: JarvisCore):
        await core.handle_command("Jarvis, stopp alles")
        assert core.permissions.kill_switch_engaged

        before = dict(core.world.lights)
        blocked = await core.handle_command("Licht im Office an")

        # Refused at the door: no mission, no execution, nothing touched.
        # The Permission Engine would deny it as well - see
        # test_permission.py::test_engaged_kill_switch_denies_even_p0 - but
        # the Core declines before it gets that far.
        assert blocked.extra["refused"] == "kill_switch"
        assert blocked.execution is None
        assert blocked.mission_id is None
        assert core.world.lights == before

        await core.handle_command("weitermachen")
        assert not core.permissions.kill_switch_engaged


class TestCriterion4ToolRegistry:
    """Tool Registry führt mindestens drei Mock-Tools aus."""

    async def test_at_least_three_mock_tools_execute(self, core: JarvisCore):
        executed: list[str] = []

        for text in (
            "Systemstatus",
            "Licht im Office an",
            "verschiebe /notes/todo.md nach /archive/todo.md",
        ):
            result = await core.handle_command(text)
            assert result.execution is not None, text
            assert result.execution.outcome is ExecutionOutcome.EXECUTED, text
            executed.append(result.execution.capability)

        assert len(set(executed)) >= 3

    async def test_execution_really_changes_the_world(self, core: JarvisCore):
        await core.handle_command("Licht im Office an")
        assert core.world.lights["office"] == "on"

        await core.handle_command("verschiebe /notes/todo.md nach /archive/todo.md")
        assert "/archive/todo.md" in core.world.files
        assert "/notes/todo.md" not in core.world.files

    async def test_parameters_are_validated_against_the_schema(self, core: JarvisCore):
        from jarvis.capability.models import ExecutionContext

        core.permissions.issue_grant("m-x", frozenset({"*"}))
        result = await core.gateway.execute(
            "home.set_light",
            {"room": "office", "state": "purple"},
            ExecutionContext(correlation_id="c", mission_id="m-x"),
        )
        assert result.outcome is ExecutionOutcome.INVALID

    async def test_unknown_parameters_are_rejected(self, core: JarvisCore):
        from jarvis.capability.models import ExecutionContext

        core.permissions.issue_grant("m-x", frozenset({"*"}))
        result = await core.gateway.execute(
            "home.set_light",
            {"room": "office", "state": "on", "force": True},
            ExecutionContext(correlation_id="c", mission_id="m-x"),
        )
        assert result.outcome is ExecutionOutcome.INVALID


class TestCriterion5EventsAndAudit:
    """Jede Aktion erzeugt Events und Audit-Logs."""

    async def test_the_full_pipeline_is_visible_in_events(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        stored = await core.store.list_events(limit=200)
        types = {e.type for e in stored}

        assert {
            ev.COMMAND_RECEIVED,
            ev.COMMAND_ROUTED,
            ev.MISSION_CREATED,
            ev.MISSION_STATE_CHANGED,
            ev.PERMISSION_CHECKED,
            ev.TOOL_INVOKED,
            ev.VERIFICATION_STARTED,
            ev.VERIFICATION_PASSED,
            ev.TOOL_SUCCEEDED,
        } <= types
        assert result.mission_id is not None

    async def test_events_are_persisted_not_just_broadcast(self, core: JarvisCore):
        await core.handle_command("Systemstatus")
        assert len(await core.store.list_events(limit=500)) > 0

    async def test_denied_actions_are_audited(self, core: JarvisCore):
        await core.handle_command("Mach einen factory reset")
        entries = await core.audit.entries()
        assert any(e["action"] == "tool.denied" for e in entries)

    async def test_audit_chain_is_intact(self, core: JarvisCore):
        await core.handle_command("Licht im Office an")
        await core.handle_command("verschiebe /notes/todo.md nach /tmp/todo.md")
        ok, broken = await core.audit.verify_chain()
        assert ok, f"chain broken at {broken}"

    async def test_audit_records_previous_state_and_rollback_point(self, core: JarvisCore):
        await core.handle_command("verschiebe /notes/todo.md nach /archive/todo.md")
        entries = await core.audit.entries()
        executed = [e for e in entries if e["action"] == "tool.executed"]
        assert executed
        assert executed[-1]["prev_state"] is not None
        assert executed[-1]["rollback_point"] is not None

    async def test_correlation_id_ties_one_command_together(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        mission = await core.missions.load(result.mission_id)
        related = await core.store.list_events(correlation_id=mission.correlation_id)
        assert len(related) >= 5


class TestCriterion6MissionSurvivesRestart:
    """Mission bleibt nach Prozessneustart erhalten."""

    async def test_completed_mission_is_readable_by_a_fresh_process(self, config):
        first = JarvisCore(config)
        await first.start()
        result = await first.handle_command("Licht im Office an")
        mission_id = result.mission_id
        await first.stop()

        # A genuinely new Core object over the same database file.
        second = JarvisCore(config)
        await second.start()
        try:
            restored = await second.missions.load(mission_id)
            assert restored is not None
            assert restored.state is MissionState.COMPLETED
            assert restored.goal == "Licht im Office an"
            assert len(restored.history) >= 4
        finally:
            await second.stop()

    async def test_pending_approval_mission_survives_restart(self, config):
        first = JarvisCore(config)
        await first.start()
        result = await first.handle_command(
            "Nachricht an anna: bis gleich", grants=frozenset({"comms"})
        )
        mission_id = result.mission_id
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            restored = await second.missions.load(mission_id)
            assert restored.state is MissionState.WAITING_FOR_APPROVAL
        finally:
            await second.stop()

    async def test_interrupted_run_comes_back_as_paused_not_running(self, config):
        first = JarvisCore(config)
        await first.start()
        mission = await first.missions.create("langer Job")
        await first.missions.transition(mission, MissionState.PLANNING)
        await first.missions.transition(mission, MissionState.RUNNING)
        await first.stop()

        second = JarvisCore(config)
        resumed = await second.start()
        try:
            assert mission.mission_id in {m.mission_id for m in resumed}
            restored = await second.missions.load(mission.mission_id)
            # An honest state: it is demonstrably not running any more.
            assert restored.state is MissionState.PAUSED
        finally:
            await second.stop()

    async def test_audit_chain_continues_across_a_restart(self, config):
        first = JarvisCore(config)
        await first.start()
        await first.handle_command("verschiebe /notes/todo.md nach /a/todo.md")
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            await second.handle_command("verschiebe /a/todo.md nach /b/todo.md")
            ok, broken = await second.audit.verify_chain()
            assert ok, f"chain broken at {broken}"
        finally:
            await second.stop()


class TestCriterion7Verifier:
    """Verifier unterscheidet "Tool wurde aufgerufen" von "Ziel erreicht"."""

    async def test_a_tool_that_lies_about_success_fails_verification(self, core: JarvisCore):
        from jarvis.capability.models import ExecutionContext

        core.permissions.issue_grant("m-lie", frozenset({"*"}))
        result = await core.gateway.execute(
            "demo.unreliable_writer",
            {"path": "/tmp/never-written.txt"},
            ExecutionContext(correlation_id="c-lie", mission_id="m-lie"),
        )

        # The tool ran and reported success...
        assert result.outcome is ExecutionOutcome.EXECUTED
        assert result.result["ok"] is True
        assert result.verification.tool_reported_success is True
        # ...but the goal was not reached, and the Core says so.
        assert result.verification.goal_reached is False
        assert result.succeeded is False

    async def test_a_lying_tool_fails_its_mission(self, core: JarvisCore):
        from jarvis.capability.models import ExecutionContext

        mission = await core.missions.create("schreibe eine Datei")
        await core.missions.transition(mission, MissionState.PLANNING)
        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        await core.missions.transition(mission, MissionState.RUNNING)

        result = await core.gateway.execute(
            "demo.unreliable_writer",
            {"path": "/tmp/nope.txt"},
            ExecutionContext(correlation_id=mission.correlation_id, mission_id=mission.mission_id),
        )
        settled = await core._settle(mission, result=result)
        assert settled.mission_state == str(MissionState.FAILED)

    async def test_an_honest_tool_passes_verification(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        assert result.execution.verification.goal_reached is True
        assert result.execution.succeeded is True

    async def test_verification_failure_emits_its_own_event(self, core: JarvisCore):
        from jarvis.capability.models import ExecutionContext

        core.permissions.issue_grant("m-lie", frozenset({"*"}))
        await core.gateway.execute(
            "demo.unreliable_writer",
            {"path": "/tmp/x"},
            ExecutionContext(correlation_id="c-lie", mission_id="m-lie"),
        )
        stored = await core.store.list_events(limit=200)
        assert any(e.type == ev.VERIFICATION_FAILED for e in stored)

    async def test_unverifiable_is_not_silently_treated_as_verified(self, core: JarvisCore):
        from jarvis.capability.models import Capability, ExecutionContext
        from jarvis.permission.levels import PermissionLevel
        from jarvis.verify.verifier import VerificationStatus

        async def handler(params, ctx):
            return {"ok": True}

        core.registry.register(
            Capability(
                name="demo.no_contract",
                description="No verification contract.",
                level=PermissionLevel.P1_SAFE,
                handler=handler,
            )
        )
        core.permissions.issue_grant("m-nc", frozenset({"*"}))
        result = await core.gateway.execute(
            "demo.no_contract",
            {},
            ExecutionContext(correlation_id="c", mission_id="m-nc"),
        )
        assert result.verification.status is VerificationStatus.UNVERIFIABLE
        assert result.verification.goal_reached is False


class TestCriterion8MinimalDebugUiOnly:
    """Keine UI außer minimalem Debug-Dashboard ist für diesen Meilenstein nötig."""

    def test_the_only_page_served_is_the_debug_dashboard(self, config):
        with TestClient(create_app(config=config)) as client:
            page = client.get("/")
            assert page.status_code == 200
            assert "CORE 0.1" in page.text

    def test_the_dashboard_has_no_external_dependencies(self):
        from jarvis.api.server import DASHBOARD

        html = DASHBOARD.read_text(encoding="utf-8")
        # Local-first, and nothing that phones home for a stylesheet.
        assert "http://" not in html.replace("http://${location.host}", "")
        assert "https://" not in html
        assert "<script src=" not in html

    def test_no_3d_or_hud_dependencies_are_declared(self):
        """Principle 5: build core before spectacle."""
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((root / "pyproject.toml").read_text())
        declared = " ".join(pyproject["project"]["dependencies"]).lower()
        for spectacle in ("three", "tauri", "monaco", "react"):
            assert spectacle not in declared


@pytest.mark.parametrize(
    "criterion",
    [
        "1 local api/websocket",
        "2 intent router",
        "3 permission engine",
        "4 three mock tools",
        "5 events + audit",
        "6 mission survives restart",
        "7 verifier",
        "8 debug ui only",
    ],
)
def test_every_exit_criterion_has_a_test_class(criterion):
    """Guards the mapping itself: 5.4 has eight items, so this file has eight."""
    classes = [name for name in globals() if name.startswith("TestCriterion")]
    assert len(classes) == 8
