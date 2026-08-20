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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
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
