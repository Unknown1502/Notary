"""Record and replay real run event streams.

Every live run writes its event stream to `seed/<session>/events.ndjson`
(and to B2 under `replay/`). Those recordings can then be played back on
demand, which buys four things from one mechanism:

1. **A judge reaches the hero moment in seconds, not minutes.** A Kling render
   takes minutes and costs real money on every page view. Replay makes the
   deployed URL usable by a stranger.
2. **Frontend development costs nothing.** The review UI is built against
   recorded streams, so iterating on the checklist animation does not burn
   provider credits.
3. **The demo cannot fail live.** The recording is of a real run that really
   happened; playback is deterministic.
4. **CI has a fixture.** The SSE contract is tested against captured bytes.

The honesty constraint: replayed runs are labelled as replays everywhere they
appear — in the API payload, in the run header, in the health endpoint. A
provenance product that quietly passed off a recording as a live render would
be undermining its own thesis. See `Recording.is_replay`.

Timing is compressed by default (`speed=3.0`) because faithfully reproducing a
four-minute wait is faithful to the wrong thing. Gaps are also clamped, so a
90-second provider stall replays as a visible pause rather than a dead tab.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from .bus import Event, EventBus, EventType

log = logging.getLogger(__name__)

MAX_GAP_SECONDS = 2.5
MIN_GAP_SECONDS = 0.05


@dataclass
class Recording:
    session_id: str
    events: list[Event]
    recorded_at: datetime
    source_mode: str = "live"
    title: str = ""
    is_replay: bool = True

    @property
    def duration_seconds(self) -> float:
        if len(self.events) < 2:
            return 0.0
        return self.events[-1].timestamp - self.events[0].timestamp

    def summary(self) -> dict[str, Any]:
        verdicts = [
            e.data.get("decision")
            for e in self.events
            if e.type is EventType.BOARD_VERDICT
        ]
        return {
            "session_id": self.session_id,
            "title": self.title,
            "recorded_at": self.recorded_at.isoformat(),
            "source_mode": self.source_mode,
            "event_count": len(self.events),
            "wall_clock_seconds": round(self.duration_seconds, 1),
            "verdicts": verdicts,
            "took_fallback": any(
                e.type is EventType.FALLBACK_FIRED for e in self.events
            ),
            "escalated": any(e.type is EventType.ESCALATED for e in self.events),
            "certified": any(
                e.type is EventType.CERTIFICATION_SEALED for e in self.events
            ),
        }


class Recorder:
    """Append-only NDJSON writer. One file per session."""

    def __init__(self, seed_dir: Path, session_id: str, *, title: str = "") -> None:
        self.path = seed_dir / session_id / "events.ndjson"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self._meta_path = self.path.parent / "recording.json"
        self._count = 0
        self._write_meta(title)

    def _write_meta(self, title: str) -> None:
        self._meta_path.write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "title": title,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "source_mode": "live",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def write(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_record(), default=str) + "\n")
        self._count += 1

    @property
    def event_count(self) -> int:
        return self._count


def load_recording(directory: Path) -> Recording | None:
    events_path = directory / "events.ndjson"
    if not events_path.exists():
        return None

    meta: dict[str, Any] = {}
    meta_path = directory / "recording.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("unreadable recording metadata at %s", meta_path)

    events: list[Event] = []
    for line_no, line in enumerate(
        events_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            events.append(
                Event(
                    type=EventType(record["type"]),
                    session_id=record.get("session_id", directory.name),
                    data=record.get("data", {}),
                    sequence=int(record.get("sequence", line_no)),
                    timestamp=float(record.get("timestamp", 0.0)),
                )
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("skipping malformed event at %s:%d (%s)", events_path, line_no, exc)

    if not events:
        return None

    recorded_at = datetime.now(UTC)
    if raw := meta.get("recorded_at"):
        try:
            recorded_at = datetime.fromisoformat(raw)
        except ValueError:
            pass

    return Recording(
        session_id=meta.get("session_id", directory.name),
        events=events,
        recorded_at=recorded_at,
        source_mode=meta.get("source_mode", "live"),
        title=meta.get("title", ""),
    )


def list_recordings(seed_dir: Path) -> list[Recording]:
    if not seed_dir.exists():
        return []
    found: list[Recording] = []
    for child in sorted(seed_dir.iterdir()):
        if not child.is_dir():
            continue
        if (recording := load_recording(child)) is not None:
            found.append(recording)
    return found


async def stream_recording(
    recording: Recording, *, speed: float = 3.0, realtime: bool = True
) -> AsyncIterator[Event]:
    """Yield recorded events with their original relative pacing.

    `speed` divides every inter-event gap; gaps are then clamped into
    [MIN_GAP_SECONDS, MAX_GAP_SECONDS]. The clamp matters more than the speed:
    a recorded 90-second provider stall should read as "something is happening"
    for a beat, not reproduce the stall.
    """
    previous: float | None = None

    for event in recording.events:
        if realtime and previous is not None:
            gap = (event.timestamp - previous) / max(speed, 0.01)
            await asyncio.sleep(max(MIN_GAP_SECONDS, min(gap, MAX_GAP_SECONDS)))
        previous = event.timestamp

        yield Event(
            type=event.type,
            session_id=event.session_id,
            data={**event.data, "replayed": True},
            sequence=event.sequence,
            timestamp=event.timestamp,
        )


async def replay_into_bus(
    recording: Recording, bus: EventBus, session_id: str, *, speed: float = 3.0
) -> None:
    """Play a recording into the live bus under a fresh session id.

    Lets a replayed run use the identical SSE endpoint, subscription semantics,
    and frontend code path as a live one. The only difference visible anywhere
    is the `replayed: true` flag on each event — which the UI surfaces rather
    than hides.
    """
    async for event in stream_recording(recording, speed=speed):
        await bus.publish(session_id, event.type, **event.data)
