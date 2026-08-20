"""Rule-based intelligence provider.

This is the "lokales Modell + deterministic intents" row of Blueprint 6.1: the
provider that keeps Home and PC basics working with no cloud reachable, and the
one the Model Router falls back to for `SECRET` traffic that must never leave
the device.

It is also what Core 0.1 runs on. Building the Core against a deterministic
provider first is the point of Principle 1 - if the Core only works while Claude
is reachable, then Claude *is* the system, which is the coupling Blueprint 4.1
explicitly rejects. Wiring the Claude Agent SDK in behind the same
`IntelligenceProvider` port is a later, additive step, not a rewrite.

Planning reuses the shared local rule table rather than a second copy of it, so
the offline planner and the fast path agree on what a phrase means.
"""

from __future__ import annotations

from jarvis.agents.provider import AgentRequest, AgentResponse, ProposedToolCall
from jarvis.intent.rules import match_local


class RuleBasedProvider:
    """Implements `jarvis.agents.provider.IntelligenceProvider`."""

    name = "rules"

    async def plan(
        self, request: AgentRequest, *, model: str = "rules", effort: str = "low"
    ) -> AgentResponse:
        # One proposal per turn: each step stays individually permission-checked
        # and individually visible in the HUD, rather than arriving as a batch
        # the owner has to approve blind.
        if request.history:
            return AgentResponse(summary="No further steps proposed.", done=True, model=model)

        matched = match_local(request.goal)
        if matched is None:
            return AgentResponse(summary="No local rule matched this goal.", done=True, model=model)

        rule, params = matched
        available = {c["name"] for c in request.available_capabilities}
        if rule.capability not in available:
            return AgentResponse(
                summary=f"Rule {rule.name!r} matched but {rule.capability} is not available.",
                done=True,
                model=model,
            )

        return AgentResponse(
            summary=f"Matched rule {rule.name!r}; proposing {rule.capability}.",
            tool_calls=(
                ProposedToolCall(
                    capability=rule.capability, params=params, rationale=rule.rationale
                ),
            ),
            model=model,
        )
