"""Permission Engine - Blueprint 7.1 and 7.2."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jarvis.capability.models import Capability, ExecutionContext, Health
from jarvis.capability.registry import CapabilityRegistry
from jarvis.permission.engine import PermissionEngine, fingerprint
from jarvis.permission.levels import PermissionLevel
from jarvis.permission.policy import Confirmation, Decision, Policy
from jarvis.tools.mock import MockWorld, register_mock_tools


@pytest.fixture
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    register_mock_tools(reg, MockWorld())
    return reg


@pytest.fixture
def engine() -> PermissionEngine:
    return PermissionEngine(Policy.default(trusted_devices=frozenset({"desk-01"})))


def ctx(**kw) -> ExecutionContext:
    return ExecutionContext(correlation_id="corr-1", **kw)


class TestLevelTable:
    """Blueprint 7.1: each level's documented default."""

    @pytest.mark.parametrize(
        ("capability", "expected"),
        [
            ("system.status", Decision.ALLOW),
            ("home.set_light", Decision.ALLOW),
            ("files.move", Decision.ALLOW),
            ("comms.send_message", Decision.REQUIRE_CONFIRMATION),
            ("system.install_software", Decision.REQUIRE_CONFIRMATION),
            ("system.factory_reset", Decision.DENY),
        ],
    )
    def test_default_decision_per_level(self, registry, engine, capability, expected):
        cap = registry.get(capability)
        # Hand over every scope the capability declares, so this test isolates
        # the level table rather than scope checking.
        verdict = engine.check(cap, {}, ctx(grants=frozenset(cap.required_grants)))
        assert verdict.decision is expected

    def test_p4_demands_strong_confirmation(self, registry, engine):
        cap = registry.get("system.install_software")
        verdict = engine.check(cap, {}, ctx(grants=frozenset({"system.admin"})))
        assert verdict.confirmation is Confirmation.STRONG

    def test_p3_accepts_simple_confirmation(self, registry, engine):
        cap = registry.get("comms.send_message")
        verdict = engine.check(cap, {}, ctx(grants=frozenset({"comms"})))
        assert verdict.confirmation is Confirmation.SIMPLE


class TestForbidden:
    def test_p6_is_always_denied(self, registry, engine):
        verdict = engine.check(registry.get("system.factory_reset"), {}, ctx())
        assert verdict.decision is Decision.DENY
        assert verdict.rule == "forbidden_level"

    def test_p6_cannot_be_unlocked_by_policy_override(self, registry, engine):
        # An override that tries to permit a forbidden action must not win.
        engine.policy.capability_overrides["system.factory_reset"] = (
            Decision.ALLOW,
            Confirmation.NONE,
        )
        verdict = engine.check(registry.get("system.factory_reset"), {}, ctx())
        assert verdict.decision is Decision.DENY

    def test_p6_cannot_be_unlocked_by_an_approval(self, registry, engine):
        cap = registry.get("system.factory_reset")
        fp = fingerprint(cap.name, {}, None)
        engine.approvals.grant(fp, confirmation=Confirmation.STRONG)
        assert engine.check(cap, {}, ctx()).decision is Decision.DENY


class TestKillSwitch:
    """Blueprint 7.2: the Core stops work independently of the model."""

    def test_engaged_kill_switch_denies_even_p0(self, registry, engine):
        engine.engage_kill_switch("owner said stop")
        verdict = engine.check(registry.get("system.status"), {}, ctx())
        assert verdict.decision is Decision.DENY
        assert verdict.rule == "kill_switch"

    def test_release_restores_normal_operation(self, registry, engine):
        engine.engage_kill_switch()
        engine.release_kill_switch()
        assert engine.check(registry.get("system.status"), {}, ctx()).allowed


class TestGrants:
    """Blueprint 7.2: temporary, per-mission capabilities that expire."""

    def test_mission_without_grant_is_denied(self, registry, engine):
        verdict = engine.check(registry.get("system.status"), {}, ctx(mission_id="m-1"))
        assert verdict.decision is Decision.DENY
        assert verdict.rule == "no_grant"

    def test_grant_outside_its_scope_is_denied(self, registry, engine):
        engine.issue_grant("m-1", frozenset({"home.set_light"}))
        verdict = engine.check(registry.get("files.move"), {}, ctx(mission_id="m-1"))
        assert verdict.rule == "grant_scope"

    def test_expired_grant_is_denied(self, registry, engine):
        engine.issue_grant("m-1", frozenset({"*"}), ttl=timedelta(seconds=-1))
        verdict = engine.check(registry.get("system.status"), {}, ctx(mission_id="m-1"))
        assert verdict.decision is Decision.DENY
        assert verdict.rule == "grant_expired"

    def test_missing_named_scope_is_denied(self, registry, engine):
        engine.issue_grant("m-1", frozenset({"*"}))
        verdict = engine.check(registry.get("comms.send_message"), {}, ctx(mission_id="m-1"))
        assert verdict.rule == "missing_scope"

    def test_scope_from_the_grant_satisfies_the_requirement(self, registry, engine):
        engine.issue_grant("m-1", frozenset({"*"}), grants=frozenset({"comms"}))
        verdict = engine.check(registry.get("comms.send_message"), {}, ctx(mission_id="m-1"))
        assert verdict.decision is Decision.REQUIRE_CONFIRMATION


class TestGates:
    def test_unhealthy_capability_is_blocked(self, registry, engine):
        registry.set_health("system.status", Health.UNAVAILABLE)
        verdict = engine.check(registry.get("system.status"), {}, ctx())
        assert verdict.rule == "unhealthy_capability"

    def test_credential_material_in_params_is_blocked(self, registry, engine):
        verdict = engine.check(
            registry.get("home.set_light"),
            {"room": "office", "state": "sk-ant-not-a-real-key"},
            ctx(),
        )
        assert verdict.decision is Decision.DENY
        assert verdict.rule == "secrets_in_params"

    def test_p5_requires_a_trusted_device(self, registry, engine):
        cap = registry.get("security.read_secret")
        untrusted = engine.check(
            cap, {}, ctx(device_id="unknown-phone", grants=frozenset({"secrets"}))
        )
        assert untrusted.rule == "device_binding"

        missing = engine.check(cap, {}, ctx(grants=frozenset({"secrets"})))
        assert missing.rule == "device_binding"

        trusted = engine.check(cap, {}, ctx(device_id="desk-01", grants=frozenset({"secrets"})))
        assert trusted.decision is Decision.REQUIRE_CONFIRMATION


class TestApprovals:
    def test_matching_approval_upgrades_to_allow(self, registry, engine):
        cap = registry.get("comms.send_message")
        params = {"to": "anna", "body": "hi"}
        context = ctx(grants=frozenset({"comms"}))

        first = engine.check(cap, params, context)
        assert first.decision is Decision.REQUIRE_CONFIRMATION

        engine.approvals.grant(first.fingerprint, confirmation=Confirmation.SIMPLE)
        assert engine.check(cap, params, context).allowed

    def test_approval_does_not_transfer_to_different_params(self, registry, engine):
        cap = registry.get("comms.send_message")
        context = ctx(grants=frozenset({"comms"}))
        approved = engine.check(cap, {"to": "anna", "body": "hi"}, context)
        engine.approvals.grant(approved.fingerprint)

        other = engine.check(cap, {"to": "everyone", "body": "hi"}, context)
        assert other.decision is Decision.REQUIRE_CONFIRMATION

    def test_simple_approval_does_not_satisfy_a_strong_requirement(self, registry, engine):
        cap = registry.get("system.install_software")
        context = ctx(grants=frozenset({"system.admin"}))
        verdict = engine.check(cap, {"package": "vim"}, context)
        engine.approvals.grant(verdict.fingerprint, confirmation=Confirmation.SIMPLE)

        assert engine.check(cap, {"package": "vim"}, context).decision is (
            Decision.REQUIRE_CONFIRMATION
        )

    def test_expired_approval_is_ignored(self, registry, engine):
        cap = registry.get("comms.send_message")
        context = ctx(grants=frozenset({"comms"}))
        verdict = engine.check(cap, {"to": "a", "body": "b"}, context)
        engine.approvals.grant(verdict.fingerprint, ttl=timedelta(seconds=-1))

        assert not engine.check(cap, {"to": "a", "body": "b"}, context).allowed


class TestOverrides:
    def test_an_override_may_tighten(self, registry, engine):
        engine.policy.capability_overrides["home.set_light"] = (
            Decision.REQUIRE_CONFIRMATION,
            Confirmation.STRONG,
        )
        verdict = engine.check(registry.get("home.set_light"), {}, ctx())
        assert verdict.decision is Decision.REQUIRE_CONFIRMATION

    def test_an_override_may_not_loosen(self, registry, engine):
        # P3 demands confirmation; an override trying to allow it outright is
        # ignored in favour of the stricter default.
        engine.policy.capability_overrides["comms.send_message"] = (
            Decision.ALLOW,
            Confirmation.NONE,
        )
        verdict = engine.check(
            registry.get("comms.send_message"), {}, ctx(grants=frozenset({"comms"}))
        )
        assert verdict.decision is Decision.REQUIRE_CONFIRMATION


class TestDeterminism:
    def test_same_inputs_produce_the_same_verdict(self, registry, engine):
        cap = registry.get("home.set_light")
        params = {"room": "office", "state": "on"}
        a = engine.check(cap, params, ctx())
        b = engine.check(cap, params, ctx())
        assert (a.decision, a.rule, a.fingerprint) == (b.decision, b.rule, b.fingerprint)


def test_unregistered_capability_has_no_level_to_reason_about(registry):
    """A closed world: an unknown action cannot be permitted."""
    assert not registry.has("totally.made.up")


def test_capability_levels_span_the_whole_table(registry):
    levels = {c.level for c in registry.all()}
    assert levels >= {
        PermissionLevel.P0_OBSERVE,
        PermissionLevel.P1_SAFE,
        PermissionLevel.P2_REVERSIBLE,
        PermissionLevel.P3_SENSITIVE,
        PermissionLevel.P4_CRITICAL,
        PermissionLevel.P5_RESTRICTED,
        PermissionLevel.P6_FORBIDDEN,
    }


def test_capability_handlers_never_reach_the_provider_catalogue(registry):
    """Blueprint 4.1/6.2: the model sees descriptions, never callables."""
    for entry in registry.to_dict():
        assert "handler" not in entry
        assert not any(callable(v) for v in entry.values())


def test_unknown_capability_lookup_raises(registry):
    from jarvis.capability.registry import UnknownCapability

    with pytest.raises(UnknownCapability):
        registry.get("nope")


def test_capability_is_frozen(registry):
    import dataclasses

    cap = registry.get("system.status")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cap.level = PermissionLevel.P0_OBSERVE  # type: ignore[misc]


def test_registry_rejects_duplicate_registration(registry):
    cap = Capability(
        name="system.status",
        description="dup",
        level=PermissionLevel.P0_OBSERVE,
        handler=registry.get("system.status").handler,
    )
    with pytest.raises(ValueError):
        registry.register(cap)
