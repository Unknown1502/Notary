"""Keyframe extraction and probing via ffmpeg.

The Board reviews *frames*, not video. Three reasons, in order of importance:

1. **Accuracy.** A vision model given six clean stills judges logo presence and
   artifacts far more reliably than one given a compressed video it must
   summarize.
2. **Cost.** Six 768px JPEGs is a fraction of the tokens of a video-native call,
   and the review runs on every take including the ones that get rejected.
3. **Explainability.** A verdict can point at *the frame* that failed, and the
   UI can show it. "Artifact at 00:03" beats "artifacts detected".

Frames are sampled evenly across the clip rather than at scene changes, so the
sample is reproducible: the same asset always yields the same frames, which is
required for a rejection to be re-derivable.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class MediaToolingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float | None
    width: int | None
    height: int | None
    codec: str | None
    fps: float | None

    @property
    def aspect_ratio(self) -> float | None:
        if self.width and self.height:
            return self.width / self.height
        return None


def tooling_available() -> bool:
    return bool(FFMPEG and FFPROBE)


def probe(path: Path) -> MediaInfo:
    """Read stream metadata. Returns empty MediaInfo if ffprobe is missing.

    Deliberately non-fatal: a missing ffprobe yields `duration_seconds=None`,
    which the duration check reports as UNCERTAIN, which escalates to a human.
    Degrading to "ask a person" is correct; degrading to "assume it passed"
    would not be.
    """
    if not FFPROBE:
        log.warning("ffprobe not found; duration checks will report UNCERTAIN")
        return MediaInfo(None, None, None, None, None)

    try:
        result = subprocess.run(
            [
                FFPROBE, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,avg_frame_rate",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        payload = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("ffprobe failed on %s: %s", path, exc)
        return MediaInfo(None, None, None, None, None)

    streams = payload.get("streams") or [{}]
    stream = streams[0]
    fmt = payload.get("format") or {}

    duration: float | None = None
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass

    fps: float | None = None
    rate = stream.get("avg_frame_rate") or ""
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den) if float(den) else None
        except (ValueError, ZeroDivisionError):
            fps = None

    return MediaInfo(
        duration_seconds=duration,
        width=stream.get("width"),
        height=stream.get("height"),
        codec=stream.get("codec_name"),
        fps=fps,
    )


def extract_frames(
    video_path: Path,
    output_dir: Path,
    *,
    count: int = 5,
    max_edge: int = 768,
) -> list[Path]:
    """Sample `count` frames evenly across the clip.

    Frames are taken at the midpoint of `count` equal segments rather than at
    0% and 100%, because the first and last frames of generated video are
    routinely a black fade and would waste two of six review slots.
    """
    if not FFMPEG:
        raise MediaToolingUnavailable(
            "ffmpeg not found on PATH. Install ffmpeg, or use replay mode."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    info = probe(video_path)
    duration = info.duration_seconds

    if not duration or duration <= 0:
        log.warning("unknown duration for %s; sampling first frame only", video_path)
        timestamps = [0.0]
    else:
        segment = duration / count
        timestamps = [segment * (i + 0.5) for i in range(count)]

    frames: list[Path] = []
    for index, ts in enumerate(timestamps):
        out = output_dir / f"frame-{index:02d}.jpg"
        cmd = [
            FFMPEG, "-nostdin", "-y",
            "-ss", f"{ts:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale='min({max_edge},iw)':-2",
            "-q:v", "3",
            str(out),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        except subprocess.SubprocessError as exc:
            log.warning("frame extraction failed at t=%.2fs: %s", ts, exc)
            continue
        if out.exists() and out.stat().st_size > 0:
            frames.append(out)

    if not frames:
        log.error("no frames could be extracted from %s", video_path)
    return frames


def make_thumbnail(
    video_path: Path, output_path: Path, *, timestamp: float = 1.0, width: int = 640
) -> Path | None:
    if not FFMPEG:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                FFMPEG, "-nostdin", "-y",
                "-ss", f"{timestamp:.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale={width}:-2",
                "-q:v", "4",
                str(output_path),
            ],
            capture_output=True, timeout=60, check=True,
        )
    except subprocess.SubprocessError as exc:
        log.warning("thumbnail generation failed: %s", exc)
        return None
    return output_path if output_path.exists() else None
