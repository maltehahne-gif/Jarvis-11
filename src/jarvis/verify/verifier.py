"""Verifier - Blueprint 5.1 and DoD 5.4.

"Prüfen, ob die reale Aufgabe tatsächlich erfolgreich war" and, as an explicit
exit criterion, "Verifier unterscheidet 'Tool wurde aufgerufen' von 'Ziel
erreicht'".

The distinction is the whole point, so the implementation states it plainly: a
handler's return value is *evidence*, never proof. Verification re-inspects the
world through the capability's own verification contract and reports what it
finds there. A tool that returns `{"ok": true}` without having changed anything
fails verification - that is the "Falsches 'fertig'" row of the threat model
(Blueprint 7.3), whose countermeasure is named as "independent verifier +
outcome checks".

A capability with no contract yields `UNVERIFIABLE`, which is deliberately not
a synonym for success. Callers must decide what to do with an unverified claim
rather than have it silently promoted to "done".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.capability.models import Capability, ExecutionContext, VerificationOutcome

log = logging.getLogger(__name__)


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: No verification contract exists for this capability.
    UNVERIFIABLE = "unverifiable"
    #: The contract itself raised. Treated as not-verified, never as success.
    ERRORED = "errored"


@dataclass(frozen=True, slots=True)
class VerificationReport:
    status: VerificationStatus
    capability: str
    detail: str = ""
    #: What the tool claimed about itself.
    tool_reported_success: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def goal_reached(self) -> bool:
        """Only a passing independent check counts as the goal being reached."""
        return self.status is VerificationStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "capability": self.capability,
            "detail": self.detail,
            "tool_reported_success": self.tool_reported_success,
            "goal_reached": self.goal_reached,
            "evidence": self.evidence,
        }


class Verifier:
    """Runs capability verification contracts."""

    async def verify(
        self,
        capability: Capability,
        params: dict[str, Any],
        result: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationReport:
        claimed = bool(result.get("ok", False))

        if capability.verifier is None:
            return VerificationReport(
                status=VerificationStatus.UNVERIFIABLE,
                capability=capability.name,
                detail="capability declares no verification contract",
                tool_reported_success=claimed,
            )

        try:
            outcome: VerificationOutcome = await capability.verifier(params, result, context)
        except Exception as exc:
            log.exception("verification contract for %s raised", capability.name)
            return VerificationReport(
                status=VerificationStatus.ERRORED,
                capability=capability.name,
                detail=f"verification raised {type(exc).__name__}: {exc}",
                tool_reported_success=claimed,
            )

        return VerificationReport(
            status=(
                VerificationStatus.PASSED if outcome.goal_reached else VerificationStatus.FAILED
            ),
            capability=capability.name,
            detail=outcome.detail,
            tool_reported_success=claimed,
            evidence=outcome.evidence,
        )
