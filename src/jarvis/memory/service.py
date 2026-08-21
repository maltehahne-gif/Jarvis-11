"""Memory service - Blueprint 8, figure 3.

The pipeline, assembled:

    Observations -> Privacy + Sensitivity Filter -> stores
      -> Knowledge Graph + Vector Search -> Context Builder

This module owns the left half. The Context Builder (`jarvis.context.builder`)
owns the right.

Memory is fed from the Event Bus rather than called directly from the command
path, because Blueprint 5.1 names Memory as one of the Bus's consumers
alongside HUD, Mobile, Logs and Automation. Handlers are registered with
`bus.on()`, so a write lands before the command returns - a local SQLite insert
is fast enough for that, and it makes "did JARVIS learn this?" answerable
immediately instead of eventually.

Episodic entries are keyed by the episode they describe (`mission:<id>`,
`command:<id>`) rather than by a fixed predicate on `owner`. Two commands are
two events, not two versions of one belief, and the store's uniqueness
constraint would otherwise quietly collapse a history into a single row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jarvis.audit.logger import AuditLogger
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Sensitivity
from jarvis.memory.index import KnowledgeGraph, LexicalIndex, MemoryIndex, ScoredMemory
from jarvis.memory.learning import (
    OWNER,
    ProposalStatus,
    RoutineProposal,
    observations_for_action,
    propose_from,
)
from jarvis.memory.models import (
    MemoryEntry,
    MemoryType,
    Observation,
    Retention,
    Source,
    new_entry,
)
from jarvis.memory.privacy import FilterVerdict, PrivacyFilter, PrivacySettings
from jarvis.persistence.ports import MemoryStore, StateStore

log = logging.getLogger(__name__)

PRIVACY_STATE_KEY = "memory.privacy"
PROPOSALS_STATE_KEY = "memory.proposals"

#: Verification outcomes that count as "this really happened" for learning.
#: A failed verification must not teach a habit - Blueprint 7.3's "falsches
#: 'fertig'" would otherwise become a learned routine.
LEARNABLE_VERIFICATION = frozenset({"passed", "unverifiable"})

#: Called when the owner approves a routine whose trigger the Scheduler can
#: watch. Returns the new job id, or `None` if it could not be scheduled.
RoutineActivator = Callable[[RoutineProposal], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class RememberResult:
    """What happened to one observation."""

    stored: bool
    verdict: FilterVerdict
    entry: MemoryEntry | None = None
    created: bool = False
    proposal: RoutineProposal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stored": self.stored,
            "created": self.created,
            "verdict": self.verdict.to_dict(),
            "entry": self.entry.to_dict() if self.entry else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
        }


class MemoryService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        state_store: StateStore,
        audit: AuditLogger | None = None,
        index: MemoryIndex | None = None,
        privacy: PrivacyFilter | None = None,
        activator: RoutineActivator | None = None,
    ) -> None:
        self._store = store
        self._state_store = state_store
        self._audit = audit
        #: Turns an approved routine into a scheduled job. Injected rather
        #: than imported so Memory keeps knowing nothing about the Scheduler.
        self._activator = activator
        self.privacy = privacy or PrivacyFilter()
        self.index: MemoryIndex = index or LexicalIndex(store)
        self.graph = KnowledgeGraph(store)
        self._proposals: dict[str, RoutineProposal] = {}
        #: Working memory for sequence detection. Deliberately not persisted -
        #: Blueprint 8.1 lists Working memory as the volatile tier.
        self._last_capability: str | None = None

    # -- lifecycle ----------------------------------------------------------

    async def load(self) -> None:
        """Restore privacy settings and pending proposals after a restart."""
        record = await self._state_store.get_state(PRIVACY_STATE_KEY)
        if record is not None:
            self.privacy.settings = PrivacySettings(
                learn_behaviour=record.get("learn_behaviour", True),
                conversation_memory=record.get("conversation_memory", True),
                screen_camera_learning=record.get("screen_camera_learning", False),
                blocked={
                    tuple(item.split(":", 1))
                    for item in record.get("blocked", [])  # type: ignore[misc]
                },
            )
        stored = await self._state_store.get_state(PROPOSALS_STATE_KEY)
        if stored is not None:
            self._proposals = {
                p["proposal_id"]: RoutineProposal.from_dict(p) for p in stored.get("proposals", [])
            }

    async def save_privacy(self) -> None:
        await self._state_store.put_state(PRIVACY_STATE_KEY, self.privacy.settings.to_dict())

    async def _save_proposals(self) -> None:
        await self._state_store.put_state(
            PROPOSALS_STATE_KEY,
            {"proposals": [p.to_dict() for p in self._proposals.values()]},
        )

    # -- the pipeline -------------------------------------------------------

    async def remember(
        self, observation: Observation, *, sensitivity: Sensitivity | None = None
    ) -> RememberResult:
        """Run one observation through the filter and into the stores."""
        verdict = self.privacy.check(observation)
        if not verdict.accepted:
            return RememberResult(stored=False, verdict=verdict)

        assigned = verdict.sensitivity
        if sensitivity is not None:
            from jarvis.memory.privacy import raise_sensitivity

            assigned = raise_sensitivity(assigned, sensitivity)

        existing_record = await self._store.find_memory(
            observation.subject, observation.predicate, observation.project_scope
        )

        if existing_record is None:
            entry = new_entry(
                type=observation.type,
                subject=observation.subject,
                predicate=observation.predicate,
                value=observation.value,
                source=observation.source,
                sensitivity=assigned,
                project_scope=observation.project_scope,
                correlation_id=observation.correlation_id,
            )
            created = True
        else:
            entry = MemoryEntry.from_dict(existing_record).with_observation(
                source=observation.source, value=observation.value
            )
            created = False

        await self._store.put_memory(entry.to_dict())

        proposal = None
        if entry.type is MemoryType.HABIT:
            proposal = await self._maybe_propose(entry)

        return RememberResult(
            stored=True, verdict=verdict, entry=entry, created=created, proposal=proposal
        )

    async def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        project_scope: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[ScoredMemory]:
        return await self.index.search(
            query, limit=limit, project_scope=project_scope, min_confidence=min_confidence
        )

    async def entries(
        self, *, type: MemoryType | None = None, limit: int = 500
    ) -> list[MemoryEntry]:
        records = await self._store.list_memories(type=str(type) if type else None, limit=limit)
        return [MemoryEntry.from_dict(r) for r in records]

    async def get(self, memory_id: str) -> MemoryEntry | None:
        record = await self._store.get_memory(memory_id)
        return MemoryEntry.from_dict(record) if record else None

    async def put(self, entry: MemoryEntry) -> MemoryEntry:
        await self._store.put_memory(entry.to_dict())
        return entry

    async def purge_expired(self) -> list[str]:
        """Drop entries whose TEMPORARY retention has run out (Blueprint 8.4)."""
        expired = [
            e.memory_id
            for e in await self.entries()
            if e.retention is Retention.TEMPORARY and e.is_expired()
        ]
        return await self._store.delete_memories(expired)

    # -- routine proposals (Blueprint 8.3) ----------------------------------

    async def _maybe_propose(self, entry: MemoryEntry) -> RoutineProposal | None:
        if any(
            p.source_memory_id == entry.memory_id and p.status is ProposalStatus.PROPOSED
            for p in self._proposals.values()
        ):
            return None
        proposal = propose_from(entry, params=await self._params_for(entry))
        if proposal is None:
            return None
        self._proposals[proposal.proposal_id] = proposal
        await self._save_proposals()
        return proposal

    async def _params_for(self, entry: MemoryEntry) -> dict[str, Any] | None:
        """Recover the arguments a time or sequence pattern should replay.

        Only `repeats:` entries carry the parameters; a `time_pattern:` entry's
        value is the hour, and a `follows:` entry's value is the next
        capability. Proposing "daily around 20h, run home.set_light" with no
        room and no state would be a suggestion the owner cannot meaningfully
        accept, so the sibling repetition entry supplies them.
        """
        for prefix in ("time_pattern:", "follows:"):
            if not entry.predicate.startswith(prefix):
                continue
            capability = (
                entry.predicate.removeprefix(prefix)
                if prefix == "time_pattern:"
                else str(entry.value)
            )
            sibling = await self._store.find_memory(
                entry.subject, f"repeats:{capability}", entry.project_scope
            )
            if sibling is None:
                return None
            try:
                return json.loads(str(sibling["value"]))
            except (ValueError, TypeError):
                return None
        return None

    def proposals(self, *, status: ProposalStatus | None = None) -> list[RoutineProposal]:
        values = list(self._proposals.values())
        if status is not None:
            values = [p for p in values if p.status is status]
        return sorted(values, key=lambda p: p.created_at, reverse=True)

    async def decide_proposal(self, proposal_id: str, *, approve: bool) -> RoutineProposal | None:
        """Record the owner's decision on a suggested routine.

        Approval does two things, and only the owner's word starts either.
        The routine is written to procedural memory, so the Planner can reuse
        it the next time the goal comes up; and, when its trigger is one the
        Scheduler can actually watch for, it is registered as a job.

        A `WHENEVER` or `AFTER` trigger gets memory but no job. The Scheduler
        works from a clock, so a routine keyed to "after you check the system
        status" has nothing for it to wait on - registering it would create a
        job that never fires and a promise that is never kept.
        """
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            return None

        job_id: str | None = None
        if approve:
            await self._store.put_memory(
                new_entry(
                    type=MemoryType.PROCEDURAL,
                    subject=OWNER,
                    predicate=f"routine:{proposal.capability}",
                    value={"trigger": proposal.trigger, "params": proposal.params},
                    source=Source.EXPLICIT_STATEMENT,
                ).to_dict()
            )
            if proposal.schedulable and self._activator is not None:
                job_id = await self._activator(proposal)

        decided = proposal.approved(job_id=job_id) if approve else proposal.rejected()
        self._proposals[proposal_id] = decided
        await self._save_proposals()

        if self._audit is not None:
            await self._audit.log(
                action="memory.routine_proposal",
                actor="owner",
                subject=decided.capability,
                decision="approved" if approve else "rejected",
                correlation_id=decided.proposal_id,
                trigger=decided.trigger,
                trigger_kind=str(decided.trigger_kind),
                observations=decided.observations,
                scheduled_job_id=job_id,
            )
        return decided

    # -- event bus wiring ---------------------------------------------------

    def attach_to_bus(self, bus: EventBus) -> None:
        """Subscribe to the events memory learns from (Blueprint 5.1)."""

        @bus.on(ev.COMMAND_RECEIVED, name="memory.command")
        async def _on_command(event: Event) -> None:
            await self._observe_command(event)

        @bus.on(ev.TOOL_SUCCEEDED, name="memory.tool")
        async def _on_tool(event: Event) -> None:
            await self._observe_tool(event)

        @bus.on(ev.MISSION_STATE_CHANGED, name="memory.mission")
        async def _on_mission(event: Event) -> None:
            await self._observe_mission(event)

    async def _observe_command(self, event: Event) -> None:
        text = event.payload.get("text")
        if not text:
            return
        await self.remember(
            Observation(
                type=MemoryType.EPISODIC,
                subject=f"command:{event.correlation_id}",
                predicate="text",
                value=text,
                source=Source.OBSERVATION,
                correlation_id=event.correlation_id,
                from_conversation=True,
            )
        )

    async def _observe_tool(self, event: Event) -> None:
        capability = event.payload.get("capability")
        if not capability:
            return
        if event.payload.get("verification") not in LEARNABLE_VERIFICATION:
            # A tool that did not demonstrably reach its goal teaches nothing.
            return

        params = event.payload.get("params") or {}
        for observation in observations_for_action(
            capability,
            params,
            at=event.timestamp,
            previous_capability=self._last_capability,
            correlation_id=event.correlation_id,
        ):
            await self.remember(observation)
        self._last_capability = capability

    async def _observe_mission(self, event: Event) -> None:
        state = event.payload.get("to")
        if state not in ("COMPLETED", "FAILED"):
            return
        mission_id = event.payload.get("mission_id")
        if not mission_id:
            return
        await self.remember(
            Observation(
                type=MemoryType.EPISODIC,
                subject=f"mission:{mission_id}",
                predicate="outcome",
                value={"state": state, "reason": event.payload.get("reason", "")},
                source=Source.OBSERVATION,
                correlation_id=event.correlation_id,
            )
        )

    # -- introspection ------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        all_entries = await self.entries()
        by_type: dict[str, int] = {}
        for entry in all_entries:
            by_type[str(entry.type)] = by_type.get(str(entry.type), 0) + 1
        return {
            "total": len(all_entries),
            "by_type": by_type,
            "actionable": sum(1 for e in all_entries if e.actionable),
            "pinned": sum(1 for e in all_entries if e.pinned),
            "privacy": self.privacy.settings.to_dict(),
            "open_proposals": len(self.proposals(status=ProposalStatus.PROPOSED)),
        }
