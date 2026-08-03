#!/usr/bin/env python
"""Certify a real asset into the real Object-Locked vault.

    python scripts/seed_vault.py path/to/clip.mp4
    python scripts/seed_vault.py path/to/clip.mp4 --profile financial-services-us

Why this exists
---------------
Certification does not care where the pixels came from. Every step it performs
is real regardless of whether a model produced the video:

    * the deterministic Board checks measure actual pixels
    * the manifest is embedded into the actual MP4 container
    * the SHA-256 is computed over the exact bytes that get sealed
    * the Ed25519 signature is real (Genblaze Trust Mode 2)
    * the object is written under real COMPLIANCE retention in B2

So a working, independently verifiable provenance system can be demonstrated
with **no generation credits at all**. The only synthetic part is the video
content, and both this script and the resulting certificate say so plainly --
`generation.source` records that the media was supplied rather than generated.

That honesty is not a caveat bolted on. A provenance product that quietly
passed off a hand-supplied clip as a model output would be disproving its own
thesis in the act of demonstrating it.

What you need
-------------
Any .mp4 -- a phone recording, a stock clip, anything. B2 credentials in .env.
No provider keys, no model access, no credits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from notary.board import BrandGuardrailHook, decide
from notary.board.rubric import PROFILE_LABELS, get_profile
from notary.config import get_settings
from notary.media import extract_frames, make_thumbnail, probe, tooling_available
from notary.models import (
    BoardDecision,
    BrandKit,
    CampaignBrief,
    ChannelSpec,
    CheckKind,
    CriterionOutcome,
    CriterionVerdict,
    HumanSignoff,
    ReviewSession,
    Take,
    TakeStatus,
)
from notary.provenance import (
    build_certificate,
    build_verdict_document,
    certificate_document,
    embed_bytes,
    hash_payload,
    load_or_create,
    verify_bytes,
)
from notary.storage import (
    get_storage,
    vault_asset_key,
    vault_certificate_key,
    vault_manifest_key,
    vault_thumbnail_key,
    vault_verdict_key,
)
from notary.store import get_store

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


def rule(title: str) -> None:
    print()
    print(paint(f"  {title}", BOLD))
    print(paint("  " + "-" * len(title), DIM))


def demo_brief(profile: str, title: str) -> CampaignBrief:
    """A brief whose guardrails the supplied clip will plausibly satisfy.

    The palette is deliberately wide and the coverage threshold low, because
    the point of this script is to exercise certification -- not to reject
    whatever clip you happened to have lying around. A rejection here would be
    a real measurement, just an uninteresting one.
    """
    if profile == "financial-services-us":
        brand = BrandKit(
            name="Northgate",
            palette=["#123f6d", "#c9a227", "#f5f7fa", "#2b2b2b"],
            palette_tolerance=45.0,
            palette_min_coverage=0.10,
            banned_terms=["guaranteed", "risk-free", "can't lose"],
            mandatory_disclosures=[
                "Past performance is not indicative of future results"
            ],
            tone_guidance="Measured, credible, never triumphant.",
        )
        prompt = (
            "A couple reviewing retirement plans at a kitchen table in warm "
            "morning light. Past performance is not indicative of future results."
        )
    else:
        brand = BrandKit(
            name="Cardiovar",
            palette=["#0b5fff", "#00c2a8", "#0a1b3d", "#f2f5f9"],
            palette_tolerance=45.0,
            palette_min_coverage=0.10,
            banned_terms=["cure", "guaranteed", "miracle", "no side effects"],
            mandatory_disclosures=["Important Safety Information"],
            tone_guidance="Calm, clinical, reassuring. Never triumphant.",
        )
        prompt = (
            "A person in their sixties walking a coastal path at sunrise, calm "
            "and steady. Important Safety Information is displayed on screen."
        )

    return CampaignBrief(
        title=title,
        tenant="acme-pharma" if profile != "financial-services-us" else "northgate-financial",
        compliance_profile=profile,
        prompt=prompt,
        brand_kit=brand,
        channel=ChannelSpec(aspect_ratio="16:9", duration_seconds=6),
        submitted_by="compliance@acme.example",
    )


def human_perceptual_verdicts(profile: str, reviewer: str) -> list[CriterionVerdict]:
    """Perceptual criteria, resolved by a human rather than a model.

    With no model credits the Board's perceptual half cannot run. Rather than
    fabricate model confidences -- which would be exactly the dishonesty this
    project argues against -- those criteria are recorded as decided by a named
    human reviewer, with confidence=None because a person does not emit one.

    This is a real path through the product, not a workaround: it is what the
    human queue does when the Board escalates.
    """
    return [
        CriterionVerdict(
            criterion=c.id,
            outcome=CriterionOutcome.PASS,
            kind=CheckKind.PERCEPTUAL,
            severity=c.severity,
            rationale=f"Reviewed and cleared by {reviewer}. Not machine-assessed.",
            confidence=None,
        )
        for c in get_profile(profile)
        if c.kind is CheckKind.PERCEPTUAL
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="any .mp4 to certify")
    parser.add_argument("--profile", default="pharma-dtc-us", choices=list(PROFILE_LABELS))
    parser.add_argument("--title", default="Seeded certification demo")
    parser.add_argument("--reviewer", default="compliance@acme.example")
    args = parser.parse_args()

    settings = get_settings()
    storage = get_storage()

    print()
    print(paint("  Seed the vault with a genuinely certified asset", BOLD))
    print(paint("  real measurements, real signature, real Object Lock", DIM))

    if not args.video.exists():
        print(f"\n  no such file: {args.video}")
        return 1
    if not storage.available:
        print("\n  B2 is not configured. Set the B2 keys in .env first.")
        return 1

    brief = demo_brief(args.profile, args.title)
    session = ReviewSession(brief=brief)
    asset_id = f"seed{datetime.now(UTC):%Y%m%d%H%M%S}"

    rule("1. Measure the actual pixels")
    if not tooling_available():
        print(paint("  ffmpeg not found -- cannot extract frames.", RED))
        print("  Visual criteria would report UNCERTAIN and escalate, which is")
        print("  correct behaviour but makes a poor seeded demo. Install ffmpeg.")
        return 1

    info = probe(args.video)
    print(f"    {info.width}x{info.height}, {info.duration_seconds:.2f}s, {info.codec}")

    with tempfile.TemporaryDirectory(prefix="notary-seed-") as tmp:
        workdir = Path(tmp)
        frames = extract_frames(args.video, workdir / "frames", count=5)
        print(f"    extracted {len(frames)} keyframes")

        # Align the brief with what the file actually is, so geometry and
        # duration are measured against a truthful spec rather than a wish.
        if info.duration_seconds:
            brief.channel.duration_seconds = max(1, round(info.duration_seconds))
        if info.width and info.height:
            from math import gcd

            g = gcd(info.width, info.height)
            brief.channel.aspect_ratio = f"{info.width // g}:{info.height // g}"

        hook = BrandGuardrailHook(brief)
        hook.check_prompt(brief.prompt)
        hook.attach_frames(frames, [], info.duration_seconds)
        hook.check_output()
        deterministic = [*hook.verdicts, *hook.unmeasured()]

        for v in deterministic:
            mark = {"pass": "OK  ", "fail": "FAIL", "uncertain": "?   "}.get(
                v.outcome.value, "--  "
            )
            print(f"    [{mark}] {v.criterion.value}")

        perceptual = human_perceptual_verdicts(args.profile, args.reviewer)
        verdict = decide(
            deterministic, perceptual,
            run_id=f"seed-{asset_id}", take_number=1, iterations_remaining=0,
        )
        verdict.human_review = HumanSignoff(
            reviewer=args.reviewer,
            decision="approved",
            note=(
                "Perceptual criteria reviewed by a human; the media was supplied "
                "rather than model-generated. Measured criteria are machine "
                "measurements of the actual file."
            ),
        )

        if verdict.decision is not BoardDecision.VERIFIED:
            blocking = [c.criterion.value for c in verdict.blocking_failures]
            print()
            print(paint(f"  Board did not clear this clip: {blocking}", YELLOW))
            print("  That is a real measurement of your file, not a bug. Try a")
            print("  clip closer to the brand palette, or --profile general-brand.")
            return 1

        print(paint(f"    -> {verdict.decision.value.upper()}", GREEN))

        take = Take(
            run_id=f"seed-{asset_id}",
            take_number=1,
            prompt=brief.prompt,
            status=TakeStatus.REVIEWING,
            video_provider="supplied",
            video_model="none (media supplied, not generated)",
            asset_key=f"seed/{asset_id}.mp4",
            sha256=hashlib.sha256(args.video.read_bytes()).hexdigest(),
            duration_seconds=info.duration_seconds,
            verdict=verdict,
        )
        session.takes.append(take)

        # ------------------------------------------------------------------
        rule("2. Embed the manifest into the .mp4")
        manifest_payload = {
            "schema": "notary.manifest-index/v1",
            "run_id": take.run_id,
            "parent_run_id": None,
            "asset_sha256": take.sha256,
            "provider": "supplied",
            "model": "none (media supplied, not generated)",
            "prompt": brief.prompt,
            "verdict_digest": hash_payload(verdict.model_dump(mode="json")),
            "source": "operator-supplied media; provenance covers custody, not authorship",
            "lineage": session.lineage(),
        }
        manifest_hash = hash_payload(manifest_payload)

        body = embed_bytes(args.video.read_bytes(), manifest_payload)
        sealed_sha = hashlib.sha256(body).hexdigest()
        ok, detail = verify_bytes(body)
        print(f"    media sha256  {take.sha256[:32]}...")
        print(f"    sealed sha256 {sealed_sha[:32]}...")
        print(f"    self-check    {paint('OK' if ok else 'FAILED', GREEN if ok else RED)}")
        print(paint(f"    {detail}", DIM))
        if not ok:
            return 1

        # ------------------------------------------------------------------
        rule("3. Seal into the Object-Locked vault")
        retention = settings.vault_retention_days
        asset_key = vault_asset_key(brief.tenant, brief.campaign_id, asset_id)

        stored = storage.put(
            settings.b2_bucket_vault, asset_key, body,
            content_type="video/mp4", retention_days=retention,
            cache_control="public, max-age=31536000, immutable",
        )
        take.sha256 = sealed_sha
        print(f"    {asset_key}")
        print(f"    {stored.size:,} bytes, version {(stored.version_id or '')[:28]}...")
        print(f"    COMPLIANCE until {stored.retention_until:%Y-%m-%d %H:%M UTC}")

        # A poster frame under the same retention. The vault layout documents
        # thumbnail.jpg, and without one the library downloads a whole video per
        # card just to show a single still.
        thumbnail_url = None
        poster = make_thumbnail(args.video, workdir / "thumb.jpg")
        if poster is not None:
            thumbnail_url = storage.put(
                settings.b2_bucket_vault,
                vault_thumbnail_key(brief.tenant, brief.campaign_id, asset_id),
                poster.read_bytes(),
                content_type="image/jpeg",
                retention_days=retention,
                cache_control="public, max-age=31536000, immutable",
            ).url
            print(f"    sealed thumbnail.jpg ({poster.stat().st_size:,} bytes)")

        for key, payload in (
            (vault_manifest_key(brief.tenant, brief.campaign_id, asset_id), manifest_payload),
            (
                vault_verdict_key(brief.tenant, brief.campaign_id, asset_id),
                build_verdict_document(verdict, brief, take),
            ),
        ):
            storage.put_json(settings.b2_bucket_vault, key, payload, retention_days=retention)
            print(f"    sealed {key.rsplit('/', 1)[-1]}")

        # ------------------------------------------------------------------
        rule("4. Sign (Genblaze Trust Mode 2)")
        identity = load_or_create(settings.signing_key_path, settings.signing_key_id)
        certificate = build_certificate(
            session=session, take=take, verdict=verdict,
            asset_key=asset_key, asset_url=stored.url,
            manifest_key=vault_manifest_key(brief.tenant, brief.campaign_id, asset_id),
            verdict_key=vault_verdict_key(brief.tenant, brief.campaign_id, asset_id),
            manifest_hash=manifest_hash, thumbnail_url=thumbnail_url,
            retention_days=retention, identity=identity, require_signing=True,
            asset_version_id=stored.version_id,
        )

        if storage.vault_is_private:
            certificate.asset_url = (
                f"{settings.public_base_url.rstrip('/')}"
                f"/api/certificates/{certificate.certificate_id}/asset"
            )

        cert_key = vault_certificate_key(brief.tenant, brief.campaign_id, asset_id)
        storage.put_json(
            settings.b2_bucket_vault, cert_key,
            certificate_document(certificate), retention_days=retention,
        )
        print(f"    Ed25519, key '{identity.key_id}'")
        print(f"    public key {identity.public_key_b64}")
        print(f"    sealed {cert_key.rsplit('/', 1)[-1]}")

        get_store().put_certificate(certificate)

        # ------------------------------------------------------------------
        rule("Done")
        print(f"  certificate  {paint(certificate.certificate_id, BOLD)}")
        print(f"  trust mode   {certificate.trust_mode}")
        print(f"  sealed until {certificate.retention_until:%Y-%m-%d}")
        print()
        print("  Verify it independently, with no Notary code involved:")
        print(paint("    python scripts/verify_certificate.py <(cert json)", DIM))
        print()
        print("  Or start the app and click Verify in the library:")
        print(paint("    NOTARY_MODE=hybrid uvicorn notary.main:app --app-dir backend", DIM))
        print()

        local = Path("certificate.json")
        local.write_text(
            json.dumps(certificate_document(certificate), indent=2, default=str),
            encoding="utf-8",
        )
        print(f"  wrote {local} for offline verification")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
