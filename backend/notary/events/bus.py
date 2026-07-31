"""In-process event bus backing the SSE stream.

A review run is a slow, multi-stage process where the interesting part is the
*middle* — the criteria resolving one by one, a provider stalling and the
fallback firing, a rejection turning into a revision. A spinner that resolves
into a final verdict throws all of that away, and it is exactly the part a
reviewer needs to trust the result.

So every meaningful transition is published here and relayed to the browser.

Scope, stated plainly: this bus is in-process and per-instance. That is correct
for a single-node deployment and wrong for a horizontally scaled one, where a
client connected to instance B would not see events published on instance A.
The fix is a Redis pub/sub or B2 Event Notifications fan-out behind the same
`EventBus` interface — the publish/subscribe surface here does not change.
docs/OPERATIONS.md#scaling describes that path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)

MAX_BUFFERED_EVENTS = 500
SUBSCRIBER_QUEUE_SIZE = 256


class EventType(StrEnum):
    SESSION_STARTED = "session.started"
    TAKE_STARTED = "take.started"

    STEP_STARTED = "step.started"
    STEP_PROGRESS = "step.progress"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"

    FALLBACK_FIRED = "fallback.fired"

    BOARD_CONVENED = "board.convened"
    BOARD_CRITERION = "board.criterion"
    BOARD_VERDICT = "board.verdict"

    REVISION_STARTED = "revision.started"
    ESCALATED = "escalated"

    CERTIFICATION_STARTED = "certification.started"
    CERTIFICATION_SEALED = "certification.sealed"

    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"
    HEARTBEAT = "heartbeat"


TERMINAL_EVENTS = {
    EventType.SESSION_COMPLETED,
    EventType.SESSION_FAILED,
}


@dataclass
class Event:
    type: EventType
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> dict[str, str]:
        return {
            "event": self.type.value,
            "id": str(self.sequence),
            "data": json.dumps(
                {
                    "type": self.type.value,
                    "session_id": self.session_id,
                    "sequence": self.sequence,
                    "timestamp": self.timestamp,
                    **self.data,
                },
                default=str,
            ),
        }

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


class EventBus:
    """Fan-out with replay-on-connect.

    Late subscribers get the full history before the live stream. Without this,
    a browser that connects 200ms after a run starts misses the opening events
    and renders a checklist with holes in it — and the reconnect case (a laptop
    waking, a flaky network) hits it constantly.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._history: dict[str, deque[Event]] = defaultdict(
            lambda: deque(maxlen=MAX_BUFFERED_EVENTS)
        )
        self._sequences: dict[str, int] = defaultdict(int)
        self._closed: set[str] = set()
        self._lock = asyncio.Lock()

    async def publish(
        self, session_id: str, event_type: EventType, **data: Any
    ) -> Event:
        async with self._lock:
            self._sequences[session_id] += 1
            event = Event(
                type=event_type,
                session_id=session_id,
                data=data,
                sequence=self._sequences[session_id],
            )
            self._history[session_id].append(event)
            subscribers = list(self._subscribers[session_id])
            if event_type in TERMINAL_EVENTS:
                self._closed.add(session_id)

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A subscriber too slow to keep up is dropped rather than
                # allowed to apply backpressure to the pipeline. The run is the
                # product; a stalled browser tab is not a reason to slow it.
                log.warning(
                    "dropping event %s for a saturated subscriber on %s",
                    event_type.value, session_id,
                )

        return event

    async def subscribe(
        self, session_id: str, *, from_sequence: int = 0
    ) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)

        async with self._lock:
            backlog = [
                e for e in self._history[session_id] if e.sequence > from_sequence
            ]
            already_closed = session_id in self._closed
            self._subscribers[session_id].append(queue)

        try:
            for event in backlog:
                yield event

            if already_closed and backlog:
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # Heartbeat keeps proxies and load balancers from reaping an
                    # idle connection during a four-minute video render.
                    yield Event(
                        type=EventType.HEARTBEAT,
                        session_id=session_id,
                        data={},
                        sequence=-1,
                    )
                    continue

                yield event
                if event.type in TERMINAL_EVENTS:
                    return
        finally:
            async with self._lock:
                with contextlib.suppress(ValueError):
                    self._subscribers[session_id].remove(queue)

    def history(self, session_id: str) -> list[Event]:
        return list(self._history.get(session_id, ()))

    def is_closed(self, session_id: str) -> bool:
        return session_id in self._closed

    async def close(self, session_id: str) -> None:
        async with self._lock:
            self._closed.add(session_id)

    def forget(self, session_id: str) -> None:
        self._history.pop(session_id, None)
        self._sequences.pop(session_id, None)
        self._closed.discard(session_id)
        self._subscribers.pop(session_id, None)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
