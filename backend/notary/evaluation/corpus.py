"""A labelled corpus with known ground truth.

Why this can produce real numbers
---------------------------------
Evaluating a classifier normally requires humans to label data, which is slow
and is why most projects skip it. Notary's deterministic half is different: the
corpus can be *constructed* with known ground truth, because the property being
measured is a property of pixels we choose.

If a frame is built from exactly 40% on-palette and 60% off-palette chromatic
pixels, then its true coverage is 0.40. No labelling required. That makes the
measured half of the Board genuinely and cheaply evaluable — including at the
decision boundary, which is the only region where a threshold classifier can
actually be wrong.

The corpus deliberately concentrates samples near the threshold. A corpus of
obviously-good and obviously-bad frames would report ~100% accuracy and tell
you nothing; every real disagreement lives within a few percent of the cutoff.

What this does NOT evaluate
---------------------------
The perceptual half. Judging whether a logo is legible needs real generated
video and human labels, and synthesising it would be measuring our own
assumptions. `harness.py` reports that gap explicitly rather than quietly
scoring only the easy half and presenting it as the whole.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..models import BrandKit, ChannelSpec


@dataclass(frozen=True)
class Sample:
    """One labelled corpus item."""

    sample_id: str
    path: Path
    criterion: str

    true_coverage: float | None = None
    """Ground-truth fraction of chromatic pixels that are on-palette."""

    expected_outcome: str = "pass"
    """What a correct implementation must return."""

    near_boundary: bool = False
    """Within +/-10% of the decision threshold, where errors actually happen."""

    note: str = ""


def _blend_frame(
    path: Path,
    on_palette_rgb: tuple[int, int, int],
    off_palette_rgb: tuple[int, int, int],
    coverage: float,
    *,
    size: tuple[int, int] = (640, 360),
) -> float:
    """Build a frame with a precisely known on-palette fraction.

    Pixels are assigned by a deterministic column split rather than randomly,
    so the realised ratio is exact and the same corpus regenerates byte for
    byte on any machine. A random fill would introduce sampling noise into the
    ground truth itself, which would make the resulting scores meaningless.
    """
    width, height = size
    total = width * height
    on_pixels = int(round(total * coverage))

    flat = np.zeros((total, 3), dtype=np.uint8)
    flat[:on_pixels] = on_palette_rgb
    flat[on_pixels:] = off_palette_rgb

    # No explicit mode= -- it is deprecated and removed in Pillow 13, and a
    # uint8 (h, w, 3) array is inferred as RGB anyway.
    Image.fromarray(flat.reshape(height, width, 3)).save(path)
    return on_pixels / total


def build_palette_corpus(
    directory: Path, brand: BrandKit, *, samples_per_band: int = 3
) -> list[Sample]:
    """Frames spanning the palette-coverage decision boundary.

    Both fill colours are strongly chromatic so neither is excluded by the
    chroma floor -- otherwise the realised coverage would not match the
    intended one and the ground truth would be wrong.
    """
    directory.mkdir(parents=True, exist_ok=True)

    on_rgb = (11, 95, 255)    # #0b5fff, in the brand palette
    off_rgb = (255, 140, 0)   # saturated orange, far outside it
    threshold = brand.palette_min_coverage

    coverages: list[tuple[float, bool]] = []

    # Clear cases, to confirm the obvious still works.
    for value in (0.0, 0.15, 0.85, 1.0):
        coverages.append((value, False))

    # The interesting region: tight band either side of the threshold.
    for offset in (-0.10, -0.05, -0.02, 0.02, 0.05, 0.10):
        value = round(threshold + offset, 4)
        if 0.0 <= value <= 1.0:
            coverages.append((value, True))

    samples: list[Sample] = []
    for index, (coverage, near) in enumerate(coverages):
        for repeat in range(samples_per_band if near else 1):
            sample_id = f"palette-{index:02d}-{repeat}"
            path = directory / f"{sample_id}.png"
            realised = _blend_frame(path, on_rgb, off_rgb, coverage)

            samples.append(
                Sample(
                    sample_id=sample_id,
                    path=path,
                    criterion="palette_adherence",
                    true_coverage=realised,
                    expected_outcome="pass" if realised >= threshold else "fail",
                    near_boundary=near,
                    note=f"constructed coverage {realised:.4f}, threshold {threshold}",
                )
            )
    return samples


def build_geometry_corpus(directory: Path, channel: ChannelSpec) -> list[Sample]:
    """Frames at, near, and off the required aspect ratio."""
    directory.mkdir(parents=True, exist_ok=True)
    target = channel.ratio
    tolerance = 0.02

    cases: list[tuple[str, tuple[int, int], bool]] = [
        ("exact", (1280, 720), False),
        ("exact-small", (640, 360), False),
        ("within-tol", (1280, 716), True),      # ~0.6% drift
        ("edge-inside", (1280, 707), True),     # ~1.8% drift
        ("edge-outside", (1280, 695), True),    # ~3.6% drift
        ("square", (720, 720), False),
        ("vertical", (720, 1280), False),
    ]

    samples: list[Sample] = []
    for name, (width, height) in ((c[0], c[1]) for c in cases):
        near = next(c[2] for c in cases if c[0] == name)
        path = directory / f"geometry-{name}.png"
        Image.new("RGB", (width, height), (11, 95, 255)).save(path)

        observed = width / height
        drift = abs(observed - target) / target
        samples.append(
            Sample(
                sample_id=f"geometry-{name}",
                path=path,
                criterion="aspect_ratio",
                expected_outcome="pass" if drift <= tolerance else "fail",
                near_boundary=near,
                note=f"{width}x{height} = {observed:.4f}, drift {drift:.4f}",
            )
        )
    return samples


def build_neutral_corpus(directory: Path) -> list[Sample]:
    """Frames with no chromatic content.

    These must return NOT_APPLICABLE, not PASS and not FAIL. A greyscale frame
    contains nothing that either confirms or contradicts a brand palette, and
    scoring it either way would be a lie about what was measured.
    """
    directory.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    for name, grey in (("black", 0), ("mid", 128), ("white", 250)):
        path = directory / f"neutral-{name}.png"
        Image.new("RGB", (640, 360), (grey, grey, grey)).save(path)
        samples.append(
            Sample(
                sample_id=f"neutral-{name}",
                path=path,
                criterion="palette_adherence",
                expected_outcome="not_applicable",
                note=f"uniform grey {grey}, zero chroma",
            )
        )
    return samples


def build_text_corpus() -> list[tuple[str, str, list[str], str, str]]:
    """Copy cases for the lexical checks.

    Returns (id, text, terms, criterion, expected_outcome).

    The adversarial cases are the point. `secure` containing `cure` is the
    exact failure a naive substring check produces, and it would make the
    prohibited-term list unusable in the financial profile.
    """
    banned = ["cure", "guaranteed", "miracle", "risk-free"]
    disclosures = ["Important Safety Information"]

    return [
        ("ban-clean", "A calm coastal morning.", banned, "banned_lexemes", "pass"),
        ("ban-hit", "A cure for your symptoms.", banned, "banned_lexemes", "fail"),
        ("ban-case", "A CURE, guaranteed.", banned, "banned_lexemes", "fail"),
        # Substring traps -- must NOT fire.
        ("ban-secure", "A secure investment.", banned, "banned_lexemes", "pass"),
        ("ban-cureless", "The cureless condition.", banned, "banned_lexemes", "pass"),
        ("ban-miracles", "Miracles of nature.", banned, "banned_lexemes", "pass"),
        ("ban-punct", "Is it a cure? Ask.", banned, "banned_lexemes", "fail"),
        ("ban-hyphen", "A risk-free option.", banned, "banned_lexemes", "fail"),
        ("disc-present", "See Important Safety Information.", disclosures,
         "mandatory_disclosure", "pass"),
        ("disc-absent", "Ask your doctor.", disclosures, "mandatory_disclosure", "fail"),
        ("disc-case", "important safety information below", disclosures,
         "mandatory_disclosure", "pass"),
        ("disc-space", "Important   Safety    Information", disclosures,
         "mandatory_disclosure", "pass"),
    ]


def corpus_summary(samples: list[Sample]) -> dict[str, object]:
    boundary = [s for s in samples if s.near_boundary]
    return {
        "total": len(samples),
        "near_boundary": len(boundary),
        "boundary_fraction": round(len(boundary) / len(samples), 3) if samples else 0.0,
        "by_criterion": {
            criterion: sum(1 for s in samples if s.criterion == criterion)
            for criterion in sorted({s.criterion for s in samples})
        },
        "by_expected": {
            outcome: sum(1 for s in samples if s.expected_outcome == outcome)
            for outcome in sorted({s.expected_outcome for s in samples})
        },
    }
