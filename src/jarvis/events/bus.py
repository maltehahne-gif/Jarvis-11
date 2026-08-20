"""Event Bus - Blueprint 5.1.

Distributes every typed state change to HUD, Mobile, Logs, Memory and
Automation.

Two rules shape the implementation:

* **Persist before fan-out.** A subscriber must never see an event that did not
  survive. This is what lets the UI render only server-persisted events instead
  of inventing animations (Blueprint 7.3).
* **A slow subscriber must never stall a producer.** Fluid-first (Principle 4):
  wake feedback and local actions cannot wait behind a sluggish consumer, so
  every subscriber owns a bounded queue and is drained by its own task.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from jarvis.events.envelope import Event

log = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]
EventSink = Callable[[Event], Awaitable[None]]

DEFAULT_QUEUE_SIZE = 1024


@dataclass(slots=True)
class Subscription:
    """One consumer's view of the stream.

    `patterns` are fnmatch globs over the event type, e.g. `mission.*` or `*`.
    """

    patterns: tuple[str, ...]
    queue: asyncio.Queue[Event]
    name: str = "anonymous"
    dropped: int = 0
    _bus: EventBus | None = field(default=None, repr=False)

    def matches(self, event: Event) -> bool:
        return any(fnmatch.fnmatchcase(event.type, p) for p in self.patterns)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self)
            self._bus = None

    async def stream(self) -> AsyncIterator[Event]:
        """Yield events until the subscription is closed."""
        while True:
            event = await self.queue.get()
            yield event


class EventBus:
    """In-process async fan-out with an optional durable sink.

    Version 0.x is a modular monolith (Blueprint 4.2), so this is in-process.
    The `sink` seam is where Redis/NATS slots in later without touching
    producers.
    """

    def __init__(self, sink: EventSink | None = None, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._subscriptions: list[Subscription] = []
        self._handlers: list[tuple[tuple[str, ...], EventHandler, str]] = []
        self._sink = sink
        self._queue_size = queue_size
        self._published = 0

    @property
    def published_count(self) -> int:
        return self._published

    def subscribe(self, *patterns: str, name: str = "anonymous") -> Subscription:
        """Register a queue-backed subscription. Remember to `close()` it."""
        sub = Subscription(
            patterns=patterns or ("*",),
            queue=asyncio.Queue(maxsize=self._queue_size),
            name=name,
        )
        sub._bus = self
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subscriptions:
            self._subscriptions.remove(sub)

    def on(self, *patterns: str, name: str = "handler") -> Callable[[EventHandler], EventHandler]:
        """Decorator registering an inline async handler.

        Handlers run sequentially inside `publish` and are therefore only for
        fast, in-core reactions (state updates, counters). Anything that can
        block belongs in a `subscribe()` consumer.
        """

        def decorate(fn: EventHandler) -> EventHandler:
            self._handlers.append((patterns or ("*",), fn, name))
            return fn

        return decorate

    async def publish(self, event: Event) -> Event:
        """Persist, then fan out. Returns the event for call-site chaining."""
        if self._sink is not None:
            await self._sink(event)
        self._published += 1

        for patterns, handler, name in self._handlers:
            if any(fnmatch.fnmatchcase(event.type, p) for p in patterns):
                try:
                    await handler(event)
                except Exception:
                    # A misbehaving handler must not sink the producer.
                    log.exception("event handler %s failed on %s", name, event.type)

        for sub in list(self._subscriptions):
            if not sub.matches(event):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.dropped += 1
                log.warning(
                    "subscriber %s is full, dropped %s (total dropped: %d)",
                    sub.name,
                    event.type,
                    sub.dropped,
                )
        return event
