"""Local API and WebSocket - DoD 5.4, "Textkommando erreicht Core über lokale
API/WebSocket".

Bound to loopback. Blueprint 7.2 is explicit that remote access must never run
over admin ports exposed to the internet, so reaching JARVIS from a phone is a
job for the private mesh/WireGuard tunnel described in Blueprint 10.3 - not for
widening this bind address.

The WebSocket carries the live event stream. It forwards only events the Core
actually published and persisted; the surface never invents status of its own
(Blueprint 7.3, "UI täuscht Status vor").
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.memory.models import MemoryType
from jarvis.permission.policy import Confirmation

log = logging.getLogger(__name__)

DASHBOARD = Path(__file__).parent / "dashboard.html"


class CommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    device_id: str | None = None
    #: Named scopes the owner deliberately hands to this one command, e.g.
    #: `["comms"]`. Absent scopes mean sensitive capabilities stay denied.
    grants: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    fingerprint: str
    device_id: str | None = None
    strong: bool = False


class CorrectionRequest(BaseModel):
    """Blueprint 8.4's "Correct"."""

    value: Any


class DontLearnRequest(BaseModel):
    """Blueprint 8.4's "Don't Learn This". `*` blocks the whole subject."""

    subject: str
    predicate: str = "*"


class ForgetWindowRequest(BaseModel):
    """ "Jarvis, vergiss die letzten 30 Minuten"."""

    minutes: int = Field(default=30, ge=1, le=60 * 24 * 7)


class PrivacyRequest(BaseModel):
    """The three independent switches from Blueprint 8.4.

    `None` leaves a switch untouched, so a caller can flip one without having
    to restate the other two.
    """

    learn_behaviour: bool | None = None
    conversation_memory: bool | None = None
    screen_camera_learning: bool | None = None


class RoutineDecisionRequest(BaseModel):
    approve: bool


def create_app(core: JarvisCore | None = None, config: CoreConfig | None = None) -> FastAPI:
    config = config or CoreConfig()
    core = core or JarvisCore(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resumed = await core.start()
        log.info("JARVIS core online (%d mission(s) resumed)", len(resumed))
        try:
            yield
        finally:
            await core.stop()

    app = FastAPI(title="JARVIS Core", version="0.1.0", lifespan=lifespan)
    app.state.core = core

    # -- debug dashboard (DoD 5.4: nothing more than this) -----------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return DASHBOARD.read_text(encoding="utf-8")

    # -- commands ----------------------------------------------------------

    @app.post("/command")
    async def command(request: CommandRequest) -> dict[str, Any]:
        result = await core.handle_command(
            request.text,
            device_id=request.device_id,
            grants=frozenset(request.grants),
        )
        return result.to_dict()

    @app.post("/approve")
    async def approve(request: ApprovalRequest) -> dict[str, Any]:
        result = await core.approve(
            request.fingerprint,
            confirmation=Confirmation.STRONG if request.strong else Confirmation.SIMPLE,
            device_id=request.device_id,
        )
        return result.to_dict()

    @app.post("/deny")
    async def deny(request: ApprovalRequest) -> dict[str, Any]:
        result = await core.deny(request.fingerprint)
        return result.to_dict()

    # -- inspection --------------------------------------------------------

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return {
            "kill_switch": core.permissions.kill_switch_engaged,
            "kill_switch_reason": core.permissions.kill_switch_reason,
            "events_published": core.bus.published_count,
            "active_missions": sorted(core.state.active_missions),
            "pending_approvals": core.permissions.approvals.pending(),
            "state": core.state.snapshot(),
            "world": core.world.snapshot(),
        }

    @app.get("/capabilities")
    async def capabilities() -> list[dict[str, Any]]:
        return core.registry.to_dict()

    @app.get("/missions")
    async def missions(limit: int = 50) -> list[dict[str, Any]]:
        return [m.to_dict() for m in await core.missions.list(limit=limit)]

    @app.get("/missions/{mission_id}")
    async def mission(mission_id: str) -> dict[str, Any]:
        found = await core.missions.load(mission_id)
        if found is None:
            raise HTTPException(status_code=404, detail="mission not found")
        return found.to_dict()

    @app.get("/events")
    async def events(correlation_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        stored = await core.store.list_events(correlation_id=correlation_id, limit=limit)
        return [e.to_dict() for e in stored]

    @app.get("/audit")
    async def audit(limit: int = 100) -> dict[str, Any]:
        ok, broken_at = await core.audit.verify_chain()
        return {
            "chain_valid": ok,
            "first_broken_entry": broken_at,
            "entries": await core.audit.entries(limit=limit),
        }

    @app.get("/routing")
    async def routing() -> dict[str, Any]:
        return core.model_router.table()

    # -- "What JARVIS Knows" (Blueprint 8.4) -------------------------------

    @app.get("/memory")
    async def memory(type: str | None = None, limit: int = 200) -> dict[str, Any]:
        return {
            "summary": await core.memory.snapshot(),
            "entries": await core.memory_control.what_jarvis_knows(
                type=MemoryType(type) if type else None, limit=limit
            ),
        }

    @app.get("/memory/search")
    async def memory_search(q: str, limit: int = 10) -> list[dict[str, Any]]:
        return [s.to_dict() for s in await core.memory.recall(q, limit=limit)]

    @app.post("/memory/{memory_id}/correct")
    async def memory_correct(memory_id: str, request: CorrectionRequest) -> dict[str, Any]:
        entry = await core.memory_control.correct(memory_id, request.value)
        if entry is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return entry.to_dict()

    @app.post("/memory/{memory_id}/forget")
    async def memory_forget(memory_id: str) -> dict[str, Any]:
        return (await core.memory_control.forget(memory_id)).to_dict()

    @app.post("/memory/{memory_id}/pin")
    async def memory_pin(memory_id: str) -> dict[str, Any]:
        entry = await core.memory_control.pin(memory_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return entry.to_dict()

    @app.post("/memory/{memory_id}/make-temporary")
    async def memory_make_temporary(memory_id: str, hours: float = 1.0) -> dict[str, Any]:
        entry = await core.memory_control.make_temporary(memory_id, timedelta(hours=hours))
        if entry is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return entry.to_dict()

    @app.post("/memory/dont-learn")
    async def memory_dont_learn(request: DontLearnRequest) -> dict[str, Any]:
        receipt = await core.memory_control.dont_learn_this(request.subject, request.predicate)
        return receipt.to_dict()

    @app.post("/memory/forget-window")
    async def memory_forget_window(request: ForgetWindowRequest) -> dict[str, Any]:
        return (await core.memory_control.forget_window(request.minutes)).to_dict()

    @app.get("/memory/privacy")
    async def memory_privacy() -> dict[str, Any]:
        return core.memory.privacy.settings.to_dict()

    @app.post("/memory/privacy")
    async def memory_set_privacy(request: PrivacyRequest) -> dict[str, Any]:
        return await core.memory_control.set_privacy(
            learn_behaviour=request.learn_behaviour,
            conversation_memory=request.conversation_memory,
            screen_camera_learning=request.screen_camera_learning,
        )

    @app.get("/memory/routines")
    async def memory_routines() -> list[dict[str, Any]]:
        return await core.memory_control.pending_routines()

    @app.post("/memory/routines/{proposal_id}")
    async def memory_decide_routine(
        proposal_id: str, request: RoutineDecisionRequest
    ) -> dict[str, Any]:
        decided = await core.memory_control.decide_routine(proposal_id, approve=request.approve)
        if decided is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return decided

    # -- live event stream --------------------------------------------------

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        subscription = core.bus.subscribe("*", name="websocket")
        try:
            while True:
                event = await subscription.queue.get()
                await ws.send_json(event.to_dict())
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            subscription.close()

    return app


def run(config: CoreConfig | None = None) -> None:
    import uvicorn

    config = config or CoreConfig.from_env()
    config.data_dir().mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(config=config), host=config.host, port=config.port)
