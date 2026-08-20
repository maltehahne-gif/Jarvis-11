"""Capability model - Blueprint 5.1, Capability Registry.

"Alle Tools/Skills mit Schema, Risiko, benötigten Rechten und Health
registrieren."

A `Capability` is the only way an action reaches the real world. It bundles four
things the rest of the Core relies on:

* **schema** - what parameters are legal, validated before anything runs;
* **level** - the P0-P6 risk class the Permission Engine gates on;
* **required_grants** - the named scopes a mission must hold to call it;
* **verify** - an *independent* check of whether the goal was actually reached,
  which is what separates "Tool wurde aufgerufen" from "Ziel erreicht"
  (DoD 5.4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.permission.levels import PermissionLevel


class Health(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


@dataclass(frozen=True, slots=True)
class Param:
    """One declared parameter."""

    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    choices: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": str(self.type),
            "required": self.required,
            "description": self.description,
            "choices": list(self.choices) if self.choices else None,
        }


class SchemaError(ValueError):
    """Raised when supplied parameters do not match the declared schema."""


_PY_TYPES: dict[ParamType, type | tuple[type, ...]] = {
    ParamType.STRING: str,
    ParamType.INTEGER: int,
    ParamType.NUMBER: (int, float),
    ParamType.BOOLEAN: bool,
    ParamType.OBJECT: dict,
    ParamType.ARRAY: list,
}


@dataclass(frozen=True, slots=True)
class Schema:
    """A deliberately small parameter schema.

    Small on purpose: the Core validates arguments that may have been proposed
    by a model, so the validator itself must be boring, total and easy to audit.
    Unknown keys are rejected rather than ignored - a model inventing an extra
    `force=true` must not slip through unnoticed.
    """

    params: tuple[Param, ...] = ()

    def validate(self, values: dict[str, Any]) -> dict[str, Any]:
        declared = {p.name: p for p in self.params}

        unknown = set(values) - set(declared)
        if unknown:
            raise SchemaError(f"unknown parameter(s): {', '.join(sorted(unknown))}")

        cleaned: dict[str, Any] = {}
        for name, param in declared.items():
            if name not in values:
                if param.required:
                    raise SchemaError(f"missing required parameter: {name}")
                continue
            value = values[name]
            expected = _PY_TYPES[param.type]
            # bool is a subclass of int in Python; keep the distinction sharp.
            if param.type in (ParamType.INTEGER, ParamType.NUMBER) and isinstance(value, bool):
                raise SchemaError(f"parameter {name} must be {param.type}, got boolean")
            if not isinstance(value, expected):
                raise SchemaError(
                    f"parameter {name} must be {param.type}, got {type(value).__name__}"
                )
            if param.choices is not None and value not in param.choices:
                raise SchemaError(
                    f"parameter {name} must be one of {', '.join(param.choices)}, got {value!r}"
                )
            cleaned[name] = value
        return cleaned

    def to_dict(self) -> dict[str, Any]:
        return {"params": [p.to_dict() for p in self.params]}


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """What a capability is allowed to know about the call around it."""

    correlation_id: str
    mission_id: str | None = None
    device_id: str | None = None
    actor: str = "core"
    grants: frozenset[str] = frozenset()
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """The Verifier's independent judgement about a completed call."""

    goal_reached: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[dict[str, Any], ExecutionContext], Awaitable[dict[str, Any]]]
Verifier = Callable[
    [dict[str, Any], dict[str, Any], ExecutionContext], Awaitable[VerificationOutcome]
]
Undo = Callable[[dict[str, Any], dict[str, Any], ExecutionContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Capability:
    """A registered, executable action."""

    name: str
    description: str
    level: PermissionLevel
    handler: Handler
    schema: Schema = field(default_factory=Schema)
    required_grants: frozenset[str] = frozenset()
    verifier: Verifier | None = None
    undo: Undo | None = None
    health: Health = Health.HEALTHY
    #: Set for actions whose effect can be taken back; P2 (Reversible) in the
    #: blueprint's table is "Automatisch + Undo/Log".
    reversible: bool = False
    tags: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "level": self.level.code,
            "level_label": self.level.label,
            "schema": self.schema.to_dict(),
            "required_grants": sorted(self.required_grants),
            "health": str(self.health),
            "reversible": self.reversible,
            "verifiable": self.verifier is not None,
            "undoable": self.undo is not None,
            "tags": sorted(self.tags),
        }
