"""Mock capabilities for Core 0.1 - DoD 5.4, "Tool Registry führt mindestens
drei Mock-Tools aus".

These stand in for the real Desktop Agent, Home Assistant and messaging
adapters that arrive in later phases. They are mocks in the sense that they act
on an in-process world instead of the machine, but they are *not* stubs: each
one really changes that world, and each verification contract really inspects
it afterwards rather than trusting the handler's return value. That is what
makes the Verifier's job observable this early.

The set deliberately spans P0 through P6 so the Permission Engine's whole table
is exercised by something runnable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jarvis.capability.models import (
    Capability,
    ExecutionContext,
    Health,
    Param,
    ParamType,
    Schema,
    VerificationOutcome,
)
from jarvis.capability.registry import CapabilityRegistry
from jarvis.events.envelope import utc_now
from jarvis.permission.levels import PermissionLevel


@dataclass(slots=True)
class MockWorld:
    """The small world the mock tools operate on.

    Verification contracts read *this*, never the handler's return value.
    """

    lights: dict[str, str] = field(default_factory=lambda: {"office": "off", "living": "off"})
    files: dict[str, str] = field(default_factory=lambda: {"/notes/todo.md": "- ship core 0.1"})
    outbox: list[dict[str, Any]] = field(default_factory=list)
    installed: set[str] = field(default_factory=set)
    secrets: dict[str, str] = field(default_factory=lambda: {"wifi": "hunter2"})

    def snapshot(self) -> dict[str, Any]:
        return {
            "lights": dict(self.lights),
            "files": sorted(self.files),
            "outbox": len(self.outbox),
            "installed": sorted(self.installed),
        }


def build_mock_capabilities(world: MockWorld) -> list[Capability]:
    """Create the capability set bound to one world instance."""

    # -- P0 Observe: read-only system status -------------------------------

    async def system_status(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        return {
            "ok": True,
            "checked_at": utc_now().isoformat(),
            "lights_on": sum(1 for s in world.lights.values() if s == "on"),
            "files": len(world.files),
            "messages_sent": len(world.outbox),
        }

    async def verify_status(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        # An observation is verified by agreeing with the world it described.
        actual = sum(1 for s in world.lights.values() if s == "on")
        ok = result.get("lights_on") == actual
        return VerificationOutcome(
            goal_reached=ok,
            detail="status matches world state" if ok else "status disagrees with world state",
            evidence={"reported": result.get("lights_on"), "actual": actual},
        )

    # -- P1 Safe: switch a light -------------------------------------------

    async def set_light(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        room, state = params["room"], params["state"]
        previous = world.lights.get(room)
        world.lights[room] = state
        return {
            "ok": True,
            "room": room,
            "state": state,
            "prev_state": {"room": room, "state": previous},
        }

    async def verify_light(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        actual = world.lights.get(params["room"])
        ok = actual == params["state"]
        return VerificationOutcome(
            goal_reached=ok,
            detail=f"light {params['room']} is {actual}",
            evidence={"room": params["room"], "expected": params["state"], "actual": actual},
        )

    async def undo_light(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> None:
        prev = result.get("prev_state") or {}
        if prev.get("state") is not None:
            world.lights[prev["room"]] = prev["state"]

    # -- P2 Reversible: move a file ----------------------------------------

    async def move_file(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        source, target = params["source"], params["target"]
        if source not in world.files:
            raise FileNotFoundError(f"no such file: {source}")
        content = world.files.pop(source)
        world.files[target] = content
        return {
            "ok": True,
            "source": source,
            "target": target,
            "prev_state": {"path": source, "content": content},
            "rollback_point": {"undo": "files.move", "source": target, "target": source},
        }

    async def verify_move(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        source_gone = params["source"] not in world.files
        target_present = params["target"] in world.files
        ok = source_gone and target_present
        return VerificationOutcome(
            goal_reached=ok,
            detail=("file moved" if ok else "file is not where it should be"),
            evidence={"source_gone": source_gone, "target_present": target_present},
        )

    async def undo_move(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> None:
        target, source = params["target"], params["source"]
        if target in world.files:
            world.files[source] = world.files.pop(target)

    # -- P3 Sensitive: send a message --------------------------------------

    async def send_message(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        entry = {
            "to": params["to"],
            "body": params["body"],
            "sent_at": utc_now().isoformat(),
        }
        world.outbox.append(entry)
        return {"ok": True, "message": entry, "outbox_size": len(world.outbox)}

    async def verify_message(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        ok = any(m["to"] == params["to"] and m["body"] == params["body"] for m in world.outbox)
        return VerificationOutcome(
            goal_reached=ok,
            detail="message present in outbox" if ok else "message not found in outbox",
            evidence={"outbox_size": len(world.outbox)},
        )

    # -- P4 Critical: install software -------------------------------------

    async def install_software(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        package = params["package"]
        world.installed.add(package)
        return {"ok": True, "package": package, "prev_state": {"installed": False}}

    async def verify_install(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        ok = params["package"] in world.installed
        return VerificationOutcome(
            goal_reached=ok,
            detail=f"{params['package']} {'is' if ok else 'is not'} installed",
            evidence={"installed": sorted(world.installed)},
        )

    # -- P5 Restricted: read a secret --------------------------------------

    async def read_secret(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        name = params["name"]
        if name not in world.secrets:
            raise KeyError(f"no such secret: {name}")
        # Blueprint 7.2: the model should never see key material in clear text.
        # The broker returns a handle; only the gateway-side consumer resolves
        # it. Core 0.1 models that by never putting the value in the result.
        return {"ok": True, "name": name, "handle": f"secret://{name}", "value_returned": False}

    async def verify_secret(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        leaked = any(isinstance(v, str) and v in world.secrets.values() for v in result.values())
        return VerificationOutcome(
            goal_reached=not leaked and result.get("handle") is not None,
            detail="handle issued without exposing the secret"
            if not leaked
            else "secret material leaked into the result",
            evidence={"leaked": leaked},
        )

    # -- P6 Forbidden: never runs ------------------------------------------

    async def factory_reset(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        # Unreachable by construction: the Permission Engine denies P6 before
        # dispatch. Raising here turns any regression into a loud failure
        # rather than a wiped world.
        raise AssertionError("P6 capability was dispatched - permission engine regression")

    # -- demo: a tool that lies about its own success ----------------------

    async def unreliable_writer(params: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        # Reports success without writing anything. Exists to make the
        # "Falsches 'fertig'" row of the threat model (Blueprint 7.3)
        # reproducible on demand.
        return {"ok": True, "path": params["path"], "written": True}

    async def verify_write(
        params: dict[str, Any], result: dict[str, Any], ctx: ExecutionContext
    ) -> VerificationOutcome:
        ok = params["path"] in world.files
        return VerificationOutcome(
            goal_reached=ok,
            detail=("file exists" if ok else "tool reported success but no file was written"),
            evidence={"path": params["path"], "exists": ok},
        )

    return [
        Capability(
            name="system.status",
            description="Read current system and device status.",
            level=PermissionLevel.P0_OBSERVE,
            handler=system_status,
            verifier=verify_status,
            tags=frozenset({"observe", "mock"}),
        ),
        Capability(
            name="home.set_light",
            description="Switch a light on or off.",
            level=PermissionLevel.P1_SAFE,
            handler=set_light,
            verifier=verify_light,
            undo=undo_light,
            reversible=True,
            schema=Schema(
                params=(
                    Param("room", ParamType.STRING, description="Room name."),
                    Param(
                        "state",
                        ParamType.STRING,
                        choices=("on", "off"),
                        description="Desired light state.",
                    ),
                )
            ),
            tags=frozenset({"home", "mock"}),
        ),
        Capability(
            name="files.move",
            description="Move a file to a new path.",
            level=PermissionLevel.P2_REVERSIBLE,
            handler=move_file,
            verifier=verify_move,
            undo=undo_move,
            reversible=True,
            schema=Schema(
                params=(
                    Param("source", ParamType.STRING, description="Current path."),
                    Param("target", ParamType.STRING, description="Destination path."),
                )
            ),
            tags=frozenset({"files", "mock"}),
        ),
        Capability(
            name="comms.send_message",
            description="Send a message to a contact.",
            level=PermissionLevel.P3_SENSITIVE,
            handler=send_message,
            verifier=verify_message,
            required_grants=frozenset({"comms"}),
            schema=Schema(
                params=(
                    Param("to", ParamType.STRING, description="Recipient."),
                    Param("body", ParamType.STRING, description="Message text."),
                )
            ),
            tags=frozenset({"comms", "mock"}),
        ),
        Capability(
            name="system.install_software",
            description="Install a software package.",
            level=PermissionLevel.P4_CRITICAL,
            handler=install_software,
            verifier=verify_install,
            required_grants=frozenset({"system.admin"}),
            schema=Schema(params=(Param("package", ParamType.STRING, description="Package."),)),
            tags=frozenset({"system", "mock"}),
        ),
        Capability(
            name="security.read_secret",
            description="Resolve a secret handle from the credential broker.",
            level=PermissionLevel.P5_RESTRICTED,
            handler=read_secret,
            verifier=verify_secret,
            required_grants=frozenset({"secrets"}),
            schema=Schema(params=(Param("name", ParamType.STRING, description="Secret name."),)),
            tags=frozenset({"security", "mock"}),
        ),
        Capability(
            name="system.factory_reset",
            description="Wipe the device. Explicitly forbidden.",
            level=PermissionLevel.P6_FORBIDDEN,
            handler=factory_reset,
            health=Health.HEALTHY,
            tags=frozenset({"system", "mock", "forbidden"}),
        ),
        Capability(
            name="demo.unreliable_writer",
            description=(
                "Demo tool that reports success without doing the work, "
                "so the Verifier's independence is observable."
            ),
            level=PermissionLevel.P2_REVERSIBLE,
            handler=unreliable_writer,
            verifier=verify_write,
            schema=Schema(params=(Param("path", ParamType.STRING, description="Target path."),)),
            tags=frozenset({"demo", "mock"}),
        ),
    ]


def register_mock_tools(registry: CapabilityRegistry, world: MockWorld | None = None) -> MockWorld:
    """Register the mock capability set. Returns the world they act on."""
    world = world or MockWorld()
    for capability in build_mock_capabilities(world):
        registry.register(capability, replace=True)
    return world
