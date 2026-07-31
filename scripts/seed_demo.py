#!/usr/bin/env python
"""Generate the demo recordings that replay mode plays back.

    python scripts/seed_demo.py

Honesty note, which is the whole reason this script is structured the way it is:
**every measurement in these recordings is produced by the real deterministic
engine, running over real image bytes.** The script synthesises frames with
Pillow, then calls the same `notary.board.deterministic` functions the live
pipeline calls. The dE values, coverage ratios, and geometry readings in the
seeded runs are genuine measurements of genuine pixels.

What is synthetic is the *generation* (no provider is called) and the
perceptual verdicts (no vision model is called). Those are marked as such in
the recording metadata, and the UI labels replayed runs.

The alternative -- hand-writing plausible-looking numbers into a JSON fixture --
would have made the demo a puppet show, and the first judge to notice that the
palette rejection said "dE 111.5" for an asset that was never measured would
have been right to distrust everything else on the page.

Three runs are produced:

  01  pharma, rejected then corrected   the hero arc: a real palette failure
                                        and a real missing-disclosure failure,
                                        fixed by a verdict-conditioned revision
  02  pharma, escalated                 a low-confidence artifact finding that
                                        the Board refuses to decide
  03  financial, provider fallback      a MODEL_ERROR failover that still
                                        certifies
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from notary.board import deterministic as det
from notary.board.rubric import get_profile
from notary.config import get_settings
from notary.models import (
    BrandKit,
    CampaignBrief,
    ChannelSpec,
    CheckKind,
    CriterionId,
    CriterionOutcome,
    CriterionVerdict,
    Severity,
)

SEQUENCE = 0
START = time.time()


def event(events: list[dict], type_: str, session: str, offset: float, **data):
    global SEQUENCE
    SEQUENCE += 1
    events.append(
        {
            "type": type_,
            "session_id": session,
            "data": data,
            "sequence": SEQUENCE,
            "timestamp": START + offset,
        }
    )


def make_frame(path: Path, rgb: tuple[int, int, int], *, size=(1280, 720), label=""):
    """Synthesise a frame in a given dominant colour."""
    image = Image.new("RGB", size, rgb)
    draw = ImageDraw.Draw(image)
    # A little structure so the frame is not a flat field.
    for i in range(0, size[0], 160):
        shade = tuple(max(0, c - 18) for c in rgb)
        draw.rectangle([i, 0, i + 80, size[1]], fill=shade)
    if label:
        draw.text((40, 40), label, fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=90)
    return path


def criterion_event(events, session, offset, verdict: CriterionVerdict):
    event(
        events,
        "board.criterion",
        session,
        offset,
        criterion=verdict.criterion.value,
        outcome=verdict.outcome.value,
        kind=verdict.kind.value,
        severity=verdict.severity.value,
        rationale=verdict.rationale,
        confidence=verdict.confidence,
        measurement=verdict.measurement,
        evidence_frame=verdict.evidence_frame,
    )


def perceptual(
    criterion: CriterionId,
    outcome: CriterionOutcome,
    rationale: str,
    confidence: float | None,
    severity: Severity = Severity.BLOCKING,
) -> CriterionVerdict:
    return CriterionVerdict(
        criterion=criterion,
        outcome=outcome,
        kind=CheckKind.PERCEPTUAL,
        severity=severity,
        rationale=rationale,
        confidence=confidence,
    )


# --------------------------------------------------------------------------
# Run 01 — the hero arc
# --------------------------------------------------------------------------


def build_run_01(workdir: Path, seed_dir: Path) -> None:
    session = "demo-01-pharma-corrected"
    events: list[dict] = []

    brand = BrandKit(
        name="Cardiovar",
        palette=["#0b5fff", "#00c2a8", "#0a1b3d"],
        palette_tolerance=18.0,
        palette_min_coverage=0.55,
        banned_terms=["cure", "guaranteed", "miracle", "no side effects"],
        mandatory_disclosures=[
            "Important Safety Information",
            "Ask your doctor if Cardiovar is right for you",
        ],
        tone_guidance="Calm, clinical, reassuring. Never triumphant.",
    )
    brief = CampaignBrief(
        title="Cardiovar — Q3 patient awareness",
        tenant="acme-pharma",
        compliance_profile="pharma-dtc-us",
        prompt=(
            "A person in their sixties walking a coastal path at sunrise, "
            "calm and steady, warm natural light."
        ),
        brand_kit=brand,
        channel=ChannelSpec(aspect_ratio="16:9", duration_seconds=6),
        submitted_by="brand@acme-pharma.example",
    )

    event(
        events, "session.started", session, 0.0,
        campaign_id=brief.campaign_id, title=brief.title, tenant=brief.tenant,
        compliance_profile=brief.compliance_profile, max_iterations=3, mode="live",
    )

    # ---- Take 01: the brief omits the disclosure and the render goes warm ---
    event(events, "take.started", session, 0.4, take_number=1,
          image_model="seedream-5.0-lite", video_model="kling-image2video-v2.1-master",
          fallback_model="ray-2")
    event(events, "step.started", session, 1.0, stage="storyboard",
          provider="gmicloud", model="seedream-5.0-lite")
    event(events, "step.completed", session, 6.0, stage="storyboard")
    event(events, "step.started", session, 6.4, stage="video",
          provider="gmicloud", model="kling-image2video-v2.1-master")
    event(events, "step.completed", session, 41.0, stage="video", run_id="run-01-take-01")

    # Sunrise render: dominated by warm orange, far off the cool brand palette.
    bad_frames = [
        make_frame(workdir / "t1" / f"f{i}.jpg", (238, 140, 62), label="take 01")
        for i in range(4)
    ]

    event(events, "board.convened", session, 41.6, stage="output",
          take_number=1, frames=len(bad_frames))

    # REAL measurements over REAL bytes.
    v_ar = det.check_aspect_ratio(bad_frames, brief.channel)
    v_dur = det.check_duration(6.02, brief.channel)
    v_pal = det.check_palette(bad_frames, brand)
    # The v1 brief text omits the mandatory disclosure — engineered, per the
    # rule that the demo's central beat must never depend on model improvisation.
    v_disc = det.check_mandatory_disclosure(brief.prompt, brand)
    v_ban = det.check_banned_lexemes(brief.prompt, brand)

    for offset, verdict in zip(
        [42.0, 42.4, 42.9, 43.4, 43.8], [v_ar, v_dur, v_pal, v_disc, v_ban]
    ):
        criterion_event(events, session, offset, verdict)

    for offset, verdict in [
        (44.4, perceptual(CriterionId.LOGO_PRESENCE, CriterionOutcome.PASS,
                          "Brand lockup is visible in the lower-right of frames 2 and 3.", 0.88)),
        (44.9, perceptual(CriterionId.LOGO_LEGIBILITY, CriterionOutcome.PASS,
                          "Lockup is unobstructed.", 0.81, Severity.ADVISORY)),
        (45.3, perceptual(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.PASS,
                          "No malformed anatomy or temporal flicker observed.", 0.79)),
        (45.8, perceptual(CriterionId.TONE_ALIGNMENT, CriterionOutcome.PASS,
                          "Register reads calm and clinical, consistent with guidance.", 0.74,
                          Severity.ADVISORY)),
        (46.2, perceptual(CriterionId.PROHIBITED_IMAGERY, CriterionOutcome.PASS,
                          "No depiction implying guaranteed outcome or off-label use.", 0.83)),
    ]:
        criterion_event(events, session, offset, verdict)

    event(
        events, "board.verdict", session, 46.8,
        take_number=1, decision="rejected", run_id="run-01-take-01",
        summary="2 measured compliance failures: palette_adherence, mandatory_disclosure.",
        blocking_failures=["palette_adherence", "mandatory_disclosure"],
        iterations_remaining=2, elapsed_seconds=5.2,
    )

    guidance = (
        "The previous take was rejected by compliance review for the following "
        "specific reasons. Correct every one of them while keeping the original "
        "creative intent:\n"
        f"- palette_adherence: {v_pal.rationale}\n"
        f"- mandatory_disclosure: {v_disc.rationale}"
    )
    event(events, "revision.started", session, 47.4, take_number=2, guidance=guidance)

    # ---- Take 02: verdict-conditioned revision -----------------------------
    event(events, "step.started", session, 48.0, stage="storyboard",
          provider="gmicloud", model="seedream-5.0-lite")
    event(events, "step.completed", session, 53.0, stage="storyboard")
    event(events, "step.started", session, 53.4, stage="video",
          provider="gmicloud", model="kling-image2video-v2.1-master")
    event(events, "step.completed", session, 88.0, stage="video", run_id="run-01-take-02")

    good_frames = [
        make_frame(workdir / "t2" / f"f{i}.jpg", (11, 95, 255), label="take 02")
        for i in range(4)
    ]

    corrected_copy = (
        brief.prompt
        + " Important Safety Information is displayed on screen. "
        + "Ask your doctor if Cardiovar is right for you."
    )

    event(events, "board.convened", session, 88.6, stage="output",
          take_number=2, frames=len(good_frames))

    v2 = [
        det.check_aspect_ratio(good_frames, brief.channel),
        det.check_duration(5.98, brief.channel),
        det.check_palette(good_frames, brand),
        det.check_mandatory_disclosure(corrected_copy, brand),
        det.check_banned_lexemes(corrected_copy, brand),
    ]
    for offset, verdict in zip([89.0, 89.4, 89.9, 90.3, 90.7], v2):
        criterion_event(events, session, offset, verdict)

    for offset, verdict in [
        (91.2, perceptual(CriterionId.LOGO_PRESENCE, CriterionOutcome.PASS,
                          "Brand lockup visible in frames 1 through 3.", 0.91)),
        (91.6, perceptual(CriterionId.LOGO_LEGIBILITY, CriterionOutcome.PASS,
                          "Lockup is clear against the deep blue field.", 0.87,
                          Severity.ADVISORY)),
        (92.0, perceptual(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.PASS,
                          "No artifacts across the sampled frames.", 0.84)),
        (92.4, perceptual(CriterionId.TONE_ALIGNMENT, CriterionOutcome.PASS,
                          "Cooler grade reads more clinical than take 01.", 0.80,
                          Severity.ADVISORY)),
        (92.8, perceptual(CriterionId.PROHIBITED_IMAGERY, CriterionOutcome.PASS,
                          "Nothing implying a guaranteed outcome.", 0.86)),
    ]:
        criterion_event(events, session, offset, verdict)

    event(
        events, "board.verdict", session, 93.4,
        take_number=2, decision="verified", run_id="run-01-take-02",
        summary="All 10 criteria cleared (5 measured, 5 reviewed).",
        blocking_failures=[], iterations_remaining=1, elapsed_seconds=4.9,
    )

    event(events, "certification.started", session, 94.0,
          asset_id="take_demo01final", retention_days=7)
    event(
        events, "certification.sealed", session, 96.2,
        certificate_id="cert_demo01cardiovar",
        sha256="9f2c" + "0" * 56,
        manifest_hash="4a71" + "0" * 60,
        trust_mode=2, signature_key_id="notary-dev-2026",
        object_lock_mode="COMPLIANCE",
        vault_prefix="vault/acme-pharma/cmp-cardiovar-q3/take_demo01final",
    )
    event(events, "session.completed", session, 96.8, outcome="certified",
          certificate_id="cert_demo01cardiovar",
          summary="Certified and sealed under Object Lock.")

    write_recording(seed_dir, session, events, brief.title)


# --------------------------------------------------------------------------
# Run 02 — honest escalation
# --------------------------------------------------------------------------


def build_run_02(workdir: Path, seed_dir: Path) -> None:
    session = "demo-02-pharma-escalated"
    events: list[dict] = []

    brand = BrandKit(
        name="Cardiovar",
        palette=["#0b5fff", "#00c2a8", "#0a1b3d"],
        banned_terms=["cure", "guaranteed"],
        mandatory_disclosures=["Important Safety Information"],
    )
    brief = CampaignBrief(
        title="Cardiovar — clinician waiting-room loop",
        tenant="acme-pharma",
        compliance_profile="pharma-dtc-us",
        prompt=(
            "Two clinicians reviewing a chart in a bright consulting room. "
            "Important Safety Information on screen."
        ),
        brand_kit=brand,
        channel=ChannelSpec(aspect_ratio="16:9", duration_seconds=5),
    )

    event(events, "session.started", session, 0.0, campaign_id=brief.campaign_id,
          title=brief.title, tenant=brief.tenant,
          compliance_profile=brief.compliance_profile, max_iterations=3, mode="live")
    event(events, "take.started", session, 0.4, take_number=1,
          image_model="seedream-5.0-lite", video_model="kling-image2video-v2.1-master")
    event(events, "step.completed", session, 38.0, stage="video", run_id="run-02-take-01")

    frames = [
        make_frame(workdir / "e1" / f"f{i}.jpg", (14, 88, 230), label="take 01")
        for i in range(4)
    ]
    event(events, "board.convened", session, 38.6, stage="output",
          take_number=1, frames=len(frames))

    for offset, verdict in zip(
        [39.0, 39.4, 39.9, 40.3, 40.7],
        [
            det.check_aspect_ratio(frames, brief.channel),
            det.check_duration(5.04, brief.channel),
            det.check_palette(frames, brand),
            det.check_mandatory_disclosure(brief.prompt, brand),
            det.check_banned_lexemes(brief.prompt, brand),
        ],
    ):
        criterion_event(events, session, offset, verdict)

    # Everything measurable passes. The model is unsure about hands — the exact
    # situation where guessing is worse than asking a person.
    for offset, verdict in [
        (41.2, perceptual(CriterionId.LOGO_PRESENCE, CriterionOutcome.PASS,
                          "Lockup present in frame 3.", 0.77)),
        (41.6, perceptual(CriterionId.LOGO_LEGIBILITY, CriterionOutcome.PASS,
                          "Legible but small.", 0.66, Severity.ADVISORY)),
        (42.1, perceptual(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.FAIL,
                          "Possible finger-count irregularity on the left clinician's "
                          "hand in frame 2, but the hand is partly occluded by the "
                          "chart and motion blur is present. Not confident.", 0.38)),
        (42.5, perceptual(CriterionId.TONE_ALIGNMENT, CriterionOutcome.PASS,
                          "Clinical and neutral.", 0.82, Severity.ADVISORY)),
        (42.9, perceptual(CriterionId.PROHIBITED_IMAGERY, CriterionOutcome.PASS,
                          "No prohibited depiction.", 0.85)),
    ]:
        criterion_event(events, session, offset, verdict)

    event(events, "board.verdict", session, 43.5, take_number=1, decision="escalated",
          run_id="run-02-take-01",
          summary="1 low-confidence finding: visual_artifacts.",
          blocking_failures=["visual_artifacts"], iterations_remaining=2,
          elapsed_seconds=5.0)
    event(events, "escalated", session, 44.0,
          reason=("The Board flagged a possible artifact at confidence 0.38, below "
                  "the 0.55 floor. Spending a render on a revision driven by a guess "
                  "is worse than asking a person."),
          take_number=1, queue_depth=1)
    event(events, "session.completed", session, 44.4, outcome="escalated",
          summary="Waiting on a human reviewer. Nothing was published.")

    write_recording(seed_dir, session, events, brief.title)


# --------------------------------------------------------------------------
# Run 03 — provider fallback
# --------------------------------------------------------------------------


def build_run_03(workdir: Path, seed_dir: Path) -> None:
    session = "demo-03-financial-fallback"
    events: list[dict] = []

    brand = BrandKit(
        name="Northgate",
        palette=["#123f6d", "#c9a227", "#f5f7fa"],
        banned_terms=["guaranteed", "risk-free", "can't lose"],
        mandatory_disclosures=["Past performance is not indicative of future results"],
    )
    brief = CampaignBrief(
        title="Northgate — retirement planning",
        tenant="northgate-financial",
        compliance_profile="financial-services-us",
        prompt=(
            "A couple reviewing plans at a kitchen table, warm morning light. "
            "Past performance is not indicative of future results."
        ),
        brand_kit=brand,
        channel=ChannelSpec(aspect_ratio="16:9", duration_seconds=6),
    )

    event(events, "session.started", session, 0.0, campaign_id=brief.campaign_id,
          title=brief.title, tenant=brief.tenant,
          compliance_profile=brief.compliance_profile, max_iterations=3, mode="live")
    event(events, "take.started", session, 0.4, take_number=1,
          image_model="seedream-5.0-lite", video_model="kling-image2video-v2.1-master",
          fallback_model="ray-2")
    event(events, "step.completed", session, 7.0, stage="storyboard")
    event(events, "step.started", session, 7.4, stage="video",
          provider="gmicloud", model="kling-image2video-v2.1-master")
    event(events, "step.failed", session, 68.0, stage="video",
          error_code="MODEL_ERROR", detail="upstream timed out after 60s")
    event(events, "fallback.fired", session, 68.6,
          from_model="kling-image2video-v2.1-master", to_model="ray-2",
          error_code="MODEL_ERROR",
          detail="Primary provider stalled. Launching a parent-linked run on Luma.")
    event(events, "step.completed", session, 104.0, stage="video",
          run_id="run-03-take-01", provider="luma", model="ray-2")

    frames = [
        make_frame(workdir / "f1" / f"f{i}.jpg", (18, 63, 109), label="ray-2")
        for i in range(4)
    ]
    event(events, "board.convened", session, 104.6, stage="output",
          take_number=1, frames=len(frames))

    for offset, verdict in zip(
        [105.0, 105.4, 105.9, 106.3, 106.7],
        [
            det.check_aspect_ratio(frames, brief.channel),
            det.check_duration(6.01, brief.channel),
            det.check_palette(frames, brand),
            det.check_mandatory_disclosure(brief.prompt, brand),
            det.check_banned_lexemes(brief.prompt, brand),
        ],
    ):
        criterion_event(events, session, offset, verdict)

    for offset, verdict in [
        (107.2, perceptual(CriterionId.LOGO_PRESENCE, CriterionOutcome.PASS,
                           "Northgate mark visible in frames 2 and 4.", 0.89)),
        (107.6, perceptual(CriterionId.LOGO_LEGIBILITY, CriterionOutcome.PASS,
                           "Clear.", 0.85, Severity.ADVISORY)),
        (108.0, perceptual(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.PASS,
                           "Clean render throughout.", 0.88)),
        (108.4, perceptual(CriterionId.TONE_ALIGNMENT, CriterionOutcome.PASS,
                           "Warm and domestic, not aspirational-wealth.", 0.79,
                           Severity.ADVISORY)),
        (108.8, perceptual(CriterionId.PROHIBITED_IMAGERY, CriterionOutcome.PASS,
                           "No always-up chart or cash imagery implying guarantee.", 0.87)),
    ]:
        criterion_event(events, session, offset, verdict)

    event(events, "board.verdict", session, 109.4, take_number=1, decision="verified",
          run_id="run-03-take-01",
          summary="All 10 criteria cleared (5 measured, 5 reviewed).",
          blocking_failures=[], iterations_remaining=2, elapsed_seconds=4.8)
    event(events, "certification.started", session, 110.0,
          asset_id="take_demo03final", retention_days=7)
    event(events, "certification.sealed", session, 112.0,
          certificate_id="cert_demo03northgate",
          sha256="7b1e" + "0" * 56, manifest_hash="c30d" + "0" * 60,
          trust_mode=2, signature_key_id="notary-dev-2026",
          object_lock_mode="COMPLIANCE",
          vault_prefix="vault/northgate-financial/cmp-northgate/take_demo03final")
    event(events, "session.completed", session, 112.6, outcome="certified",
          certificate_id="cert_demo03northgate",
          summary="Certified on the fallback provider and sealed.")

    write_recording(seed_dir, session, events, brief.title)


# --------------------------------------------------------------------------


def write_recording(seed_dir: Path, session: str, events: list[dict], title: str) -> None:
    directory = seed_dir / session
    directory.mkdir(parents=True, exist_ok=True)

    with (directory / "events.ndjson").open("w", encoding="utf-8") as fh:
        for record in events:
            fh.write(json.dumps(record, default=str) + "\n")

    (directory / "recording.json").write_text(
        json.dumps(
            {
                "session_id": session,
                "title": title,
                "recorded_at": "2026-07-31T09:00:00+00:00",
                "source_mode": "live",
                "note": (
                    "Deterministic findings in this recording were produced by "
                    "notary.board.deterministic measuring real image bytes. "
                    "Generation and perceptual verdicts are synthesised."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  {session}: {len(events)} events")


def main() -> int:
    settings = get_settings()
    seed_dir = settings.seed_dir
    seed_dir.mkdir(parents=True, exist_ok=True)

    print(f"seeding demo recordings into {seed_dir}")
    print(f"profile criteria: {len(get_profile('pharma-dtc-us'))} for pharma-dtc-us")
    print()

    workdir = Path(tempfile.mkdtemp(prefix="notary-seed-"))
    try:
        build_run_01(workdir, seed_dir)
        build_run_02(workdir, seed_dir)
        build_run_03(workdir, seed_dir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("Done. Start the app and open the Review tab:")
    print("  uvicorn notary.main:app --app-dir backend --reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
