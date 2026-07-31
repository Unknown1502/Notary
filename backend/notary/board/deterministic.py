"""Computed compliance checks. No model, no network, no judgement.

Every function here turns bytes into a measurement and a pass/fail against a
stated threshold. That property is the point: a FAIL produced by this module is
reproducible by anyone holding the asset, without trusting Notary, without an
API key, and without the same weather. It is evidence, not an opinion.

This is deliberately the *first* line of the Board. Asking a vision model
whether a 1:1 video is 16:9 would be slower, costlier, and less accurate than
dividing two integers. Models are reserved for the questions that actually need
one -- see board/review.py.

All functions are pure and dependency-light (numpy + Pillow) so the test suite
exercises them with synthetic frames and no SDK, no credentials, and no spend.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image

from ..models import (
    BrandKit,
    ChannelSpec,
    CheckKind,
    CriterionId,
    CriterionOutcome,
    CriterionVerdict,
    Severity,
)

# --------------------------------------------------------------------------
# Color science: sRGB -> CIE L*a*b*, CIE76 deltaE
# --------------------------------------------------------------------------

# D65 reference white, 2-degree observer.
_WHITE_D65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)

# sRGB (linear) -> XYZ, D65.
_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        raise ValueError(f"invalid hex color: {value!r}")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _srgb_to_linear(channel: np.ndarray) -> np.ndarray:
    """Undo the sRGB transfer function. Vectorized over any shape."""
    return np.where(
        channel <= 0.04045,
        channel / 12.92,
        np.power((channel + 0.055) / 1.055, 2.4),
    )


def _f_lab(t: np.ndarray) -> np.ndarray:
    delta = 6.0 / 29.0
    return np.where(t > delta**3, np.cbrt(t), t / (3 * delta**2) + 4.0 / 29.0)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert uint8 or float RGB in [0,255] to CIE L*a*b*.

    Accepts (..., 3) and returns (..., 3).
    """
    arr = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = _srgb_to_linear(arr)
    xyz = linear @ _RGB_TO_XYZ.T
    xyz /= _WHITE_D65
    f = _f_lab(xyz)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def delta_e_76(lab_a: np.ndarray, lab_b: np.ndarray) -> np.ndarray:
    """CIE76 color difference.

    CIE76 rather than CIEDE2000 on purpose: it is a plain Euclidean distance in
    Lab, which means a reviewer can recompute it in a spreadsheet to audit a
    rejection. For brand-palette adherence at these tolerances the two metrics
    agree on the decision, and auditability wins the tie.
    """
    diff = np.asarray(lab_a, dtype=np.float64) - np.asarray(lab_b, dtype=np.float64)
    return np.sqrt(np.sum(diff * diff, axis=-1))


# --------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PaletteMeasurement:
    coverage: float
    """Fraction of chromatic pixels within tolerance of a brand color."""

    sampled_pixels: int
    chromatic_pixels: int
    mean_delta_e: float
    worst_delta_e: float
    dominant_offenders: tuple[str, ...]
    """Hex of the most common off-palette colors, for the reviewer's benefit."""


def measure_palette(
    image_paths: Sequence[Path],
    palette: Sequence[str],
    *,
    tolerance: float,
    max_samples_per_frame: int = 20_000,
    chroma_floor: float = 12.0,
) -> PaletteMeasurement:
    """Measure how much of the chromatic content sits on the brand palette.

    Neutral pixels (chroma below `chroma_floor` in Lab) are excluded. Almost
    every frame is substantially neutral -- skin, sky, shadow, white product
    background -- and counting those against a three-color brand palette would
    make the check fire on every asset and therefore mean nothing.
    """
    if not palette:
        return PaletteMeasurement(1.0, 0, 0, 0.0, 0.0, ())

    brand_lab = rgb_to_lab(np.array([hex_to_rgb(c) for c in palette], dtype=np.float64))

    total_sampled = 0
    total_chromatic = 0
    on_palette = 0
    delta_accumulator: list[np.ndarray] = []
    offender_rgb: list[np.ndarray] = []

    for path in image_paths:
        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB"), dtype=np.uint8).reshape(-1, 3)

        if rgb.shape[0] > max_samples_per_frame:
            # Deterministic stride sampling -- NOT random. The same asset must
            # always produce the same measurement, or a rejection is not
            # reproducible and the whole evidence claim collapses.
            stride = math.ceil(rgb.shape[0] / max_samples_per_frame)
            rgb = rgb[::stride]

        total_sampled += rgb.shape[0]
        lab = rgb_to_lab(rgb)

        chroma = np.sqrt(lab[:, 1] ** 2 + lab[:, 2] ** 2)
        chromatic_mask = chroma >= chroma_floor
        chromatic = lab[chromatic_mask]
        if chromatic.size == 0:
            continue

        total_chromatic += chromatic.shape[0]

        # (n_pixels, n_brand_colors) distance matrix -> nearest brand color.
        distances = delta_e_76(chromatic[:, None, :], brand_lab[None, :, :])
        nearest = distances.min(axis=1)

        within = nearest <= tolerance
        on_palette += int(np.count_nonzero(within))
        delta_accumulator.append(nearest)

        off = rgb[chromatic_mask][~within]
        if off.size:
            offender_rgb.append(off)

    if total_chromatic == 0:
        # Entirely neutral frames. Nothing to contradict the palette, so this
        # cannot be a failure -- it is simply not applicable.
        return PaletteMeasurement(1.0, total_sampled, 0, 0.0, 0.0, ())

    all_deltas = np.concatenate(delta_accumulator)
    offenders = _dominant_hexes(offender_rgb)

    return PaletteMeasurement(
        coverage=on_palette / total_chromatic,
        sampled_pixels=total_sampled,
        chromatic_pixels=total_chromatic,
        mean_delta_e=float(all_deltas.mean()),
        worst_delta_e=float(all_deltas.max()),
        dominant_offenders=offenders,
    )


def _dominant_hexes(chunks: list[np.ndarray], top_n: int = 3) -> tuple[str, ...]:
    """Most common off-palette colors, quantized to a 32-level cube."""
    if not chunks:
        return ()
    stacked = np.concatenate(chunks)
    quantized = (stacked // 32) * 32
    packed = (
        quantized[:, 0].astype(np.int32) << 16
        | quantized[:, 1].astype(np.int32) << 8
        | quantized[:, 2].astype(np.int32)
    )
    values, counts = np.unique(packed, return_counts=True)
    order = np.argsort(counts)[::-1][:top_n]
    return tuple(f"#{int(values[i]):06x}" for i in order)


def measure_aspect_ratio(image_path: Path) -> tuple[int, int, float]:
    with Image.open(image_path) as im:
        w, h = im.size
    return w, h, (w / h if h else 0.0)


def find_banned_terms(text: str, banned: Iterable[str]) -> list[str]:
    """Whole-word, case-insensitive prohibited term match.

    Word-boundary anchored so a prohibited term like "cure" does not fire on
    "cureless" or, more importantly, on "secure" -- which it would with a naive
    substring check, and which would make the check useless in the financial
    profile where "secure" is everywhere.
    """
    if not text:
        return []
    hits: list[str] = []
    lowered = text.lower()
    for term in banned:
        term = term.strip()
        if not term:
            continue
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        if re.search(pattern, lowered):
            hits.append(term)
    return hits


def find_missing_disclosures(text: str, required: Iterable[str]) -> list[str]:
    """Required disclosure phrases absent from the copy.

    Substring rather than word-boundary here: disclosures are phrases, and
    normal punctuation/casing variation should not read as a miss.
    """
    lowered = " ".join((text or "").lower().split())
    missing: list[str] = []
    for phrase in required:
        needle = " ".join(phrase.lower().split())
        if needle and needle not in lowered:
            missing.append(phrase)
    return missing


# --------------------------------------------------------------------------
# Checks -> CriterionVerdict
# --------------------------------------------------------------------------


def _verdict(
    criterion: CriterionId,
    outcome: CriterionOutcome,
    rationale: str,
    *,
    severity: Severity = Severity.BLOCKING,
    measurement: dict[str, float | str | bool] | None = None,
    evidence_frame: str | None = None,
) -> CriterionVerdict:
    return CriterionVerdict(
        criterion=criterion,
        outcome=outcome,
        kind=CheckKind.DETERMINISTIC,
        severity=severity,
        rationale=rationale,
        confidence=None,  # a measurement has a value, not a confidence
        measurement=measurement,
        evidence_frame=evidence_frame,
    )


def check_aspect_ratio(
    frames: Sequence[Path], channel: ChannelSpec, *, tolerance: float = 0.02
) -> CriterionVerdict:
    if not frames:
        return _verdict(
            CriterionId.ASPECT_RATIO,
            CriterionOutcome.UNCERTAIN,
            "No frames were available to measure.",
        )

    w, h, observed = measure_aspect_ratio(frames[0])
    expected = channel.ratio
    drift = abs(observed - expected) / expected if expected else 1.0
    measurement = {
        "observed_ratio": round(observed, 4),
        "expected_ratio": round(expected, 4),
        "observed_pixels": f"{w}x{h}",
        "relative_drift": round(drift, 4),
        "tolerance": tolerance,
    }

    if drift <= tolerance:
        return _verdict(
            CriterionId.ASPECT_RATIO,
            CriterionOutcome.PASS,
            f"Rendered {w}x{h} ({observed:.3f}), within tolerance of "
            f"{channel.aspect_ratio} ({expected:.3f}).",
            measurement=measurement,
        )

    return _verdict(
        CriterionId.ASPECT_RATIO,
        CriterionOutcome.FAIL,
        f"Rendered {w}x{h} ({observed:.3f}) but the channel spec requires "
        f"{channel.aspect_ratio} ({expected:.3f}). Set aspect_ratio="
        f"'{channel.aspect_ratio}' explicitly on the generation step.",
        measurement=measurement,
    )


def check_duration(
    observed_seconds: float | None, channel: ChannelSpec, *, tolerance: float = 0.5
) -> CriterionVerdict:
    if observed_seconds is None:
        return _verdict(
            CriterionId.DURATION,
            CriterionOutcome.UNCERTAIN,
            "Clip duration could not be probed.",
        )

    expected = float(channel.duration_seconds)
    delta = abs(observed_seconds - expected)
    measurement = {
        "observed_seconds": round(observed_seconds, 3),
        "expected_seconds": expected,
        "delta_seconds": round(delta, 3),
        "tolerance_seconds": tolerance,
    }

    if delta <= tolerance:
        return _verdict(
            CriterionId.DURATION,
            CriterionOutcome.PASS,
            f"Clip is {observed_seconds:.2f}s against a {expected:.0f}s spec.",
            measurement=measurement,
        )

    return _verdict(
        CriterionId.DURATION,
        CriterionOutcome.FAIL,
        f"Clip is {observed_seconds:.2f}s but the placement requires "
        f"{expected:.0f}s (±{tolerance}s).",
        measurement=measurement,
    )


def check_palette(
    frames: Sequence[Path], brand: BrandKit, *, frame_keys: Sequence[str] = ()
) -> CriterionVerdict:
    if not brand.palette:
        return _verdict(
            CriterionId.PALETTE_ADHERENCE,
            CriterionOutcome.NOT_APPLICABLE,
            "No brand palette was supplied.",
        )
    if not frames:
        return _verdict(
            CriterionId.PALETTE_ADHERENCE,
            CriterionOutcome.UNCERTAIN,
            "No frames were available to measure.",
        )

    m = measure_palette(frames, brand.palette, tolerance=brand.palette_tolerance)
    measurement = {
        "coverage": round(m.coverage, 4),
        "required_coverage": brand.palette_min_coverage,
        "mean_delta_e": round(m.mean_delta_e, 2),
        "worst_delta_e": round(m.worst_delta_e, 2),
        "tolerance_delta_e": brand.palette_tolerance,
        "chromatic_pixels": m.chromatic_pixels,
        "sampled_pixels": m.sampled_pixels,
    }

    if m.chromatic_pixels == 0:
        return _verdict(
            CriterionId.PALETTE_ADHERENCE,
            CriterionOutcome.NOT_APPLICABLE,
            "Frames are effectively neutral; no chromatic content to assess.",
            measurement=measurement,
        )

    if m.coverage >= brand.palette_min_coverage:
        return _verdict(
            CriterionId.PALETTE_ADHERENCE,
            CriterionOutcome.PASS,
            f"{m.coverage:.0%} of chromatic pixels are within dE "
            f"{brand.palette_tolerance:.0f} of the brand palette "
            f"(threshold {brand.palette_min_coverage:.0%}).",
            measurement=measurement,
        )

    offenders = ", ".join(m.dominant_offenders) or "unclassified hues"
    return _verdict(
        CriterionId.PALETTE_ADHERENCE,
        CriterionOutcome.FAIL,
        f"Only {m.coverage:.0%} of chromatic pixels are on-palette "
        f"(threshold {brand.palette_min_coverage:.0%}); mean dE "
        f"{m.mean_delta_e:.1f} against a tolerance of "
        f"{brand.palette_tolerance:.0f}. Dominant off-palette colors: "
        f"{offenders}. Restate the exact palette "
        f"({', '.join(brand.palette)}) in the prompt.",
        measurement=measurement,
        evidence_frame=frame_keys[0] if frame_keys else None,
    )


def check_banned_lexemes(text: str, brand: BrandKit) -> CriterionVerdict:
    if not brand.banned_terms:
        return _verdict(
            CriterionId.BANNED_LEXEMES,
            CriterionOutcome.NOT_APPLICABLE,
            "No prohibited-term list was supplied.",
        )

    hits = find_banned_terms(text, brand.banned_terms)
    measurement = {
        "terms_checked": len(brand.banned_terms),
        "matches": len(hits),
        "matched_terms": ", ".join(hits) if hits else "",
    }

    if not hits:
        return _verdict(
            CriterionId.BANNED_LEXEMES,
            CriterionOutcome.PASS,
            f"None of the {len(brand.banned_terms)} prohibited terms appear.",
            measurement=measurement,
        )

    return _verdict(
        CriterionId.BANNED_LEXEMES,
        CriterionOutcome.FAIL,
        f"Prohibited term(s) present: {', '.join(repr(h) for h in hits)}. "
        "Legal maintains this list per market; remove the term entirely rather "
        "than rephrasing around it.",
        measurement=measurement,
    )


def check_mandatory_disclosure(text: str, brand: BrandKit) -> CriterionVerdict:
    if not brand.mandatory_disclosures:
        return _verdict(
            CriterionId.MANDATORY_DISCLOSURE,
            CriterionOutcome.NOT_APPLICABLE,
            "No mandatory disclosure configured for this profile.",
        )

    missing = find_missing_disclosures(text, brand.mandatory_disclosures)
    measurement = {
        "required": len(brand.mandatory_disclosures),
        "missing": len(missing),
        "missing_phrases": " | ".join(missing) if missing else "",
    }

    if not missing:
        return _verdict(
            CriterionId.MANDATORY_DISCLOSURE,
            CriterionOutcome.PASS,
            "All mandatory disclosures are present in the brief copy.",
            measurement=measurement,
        )

    return _verdict(
        CriterionId.MANDATORY_DISCLOSURE,
        CriterionOutcome.FAIL,
        f"Missing required disclosure: {'; '.join(repr(m) for m in missing)}. "
        "Fair balance requires the safety disclosure to accompany the benefit "
        "claim in the same asset — it cannot be added downstream.",
        measurement=measurement,
    )
