"""The perceptual half of the Board, wrapped as a Genblaze provider.

The problem this solves
-----------------------
`chat()` is explicitly not a Pipeline citizen. The SDK docs say so directly:
"Not integrated with Pipeline / Step / Asset / manifest." Left as a bare call,
the Board's verdict -- the most consequential judgement in the entire product --
would sit outside the provenance record. A judge would rightly ask why the
approval is not part of the thing being sealed.

The SDK also documents the fix: "To make a script-writing chat() call appear as
a step in the manifest (so provenance covers the words as well as the
downstream media), wrap it in a small local SyncProvider." The example builds a
step, calls `chat()` inside `generate()`, and stores the response as an Asset
with stable content addressing (`url=f"text:{digest}"`).

`BoardReviewProvider` is that pattern applied to compliance review. The result:
the verdict is an asset with a content hash, produced by a step with a recorded
model and parameters, inside the run whose manifest is signed and sealed. The
approval is *in* the record, not beside it.

Design rules that are not negotiable
------------------------------------
* temperature=0. A compliance verdict that changes between identical runs is
  not a verdict.
* Unparseable model output is UNCERTAIN, never PASS. The failure mode of a
  JSON-parsing bug must be "a human looks at it", not "it shipped".
* The model is asked only about criteria that genuinely need judgement. Every
  measurable property was already decided in board/moderation.py, and telling
  the model about them would invite it to contradict a measurement.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..genblaze_compat import (
    GENBLAZE_AVAILABLE,
    ProviderError,
    SyncProvider,
    extract_chat_text,
    extract_token_usage,
    vision_chat,
)
from ..models import (
    BoardDecision,
    BoardVerdict,
    CampaignBrief,
    CheckKind,
    CriterionId,
    CriterionOutcome,
    CriterionVerdict,
    Severity,
)
from .rubric import get_profile

log = logging.getLogger(__name__)

# Rough per-1M-token pricing for the vision tier. `chat()` always returns
# cost_usd=None, so Notary derives cost itself. Override via ModelRegistry when
# the deployed model is present there.
_DEFAULT_PRICE_PER_MTOK_IN = 0.30
_DEFAULT_PRICE_PER_MTOK_OUT = 0.90

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


SYSTEM_PROMPT = """\
You are a compliance reviewer on a brand's creative review board. You are \
reviewing sampled frames from a generated video advertisement.

You are the second stage of a two-stage review. Objectively measurable \
properties -- aspect ratio, clip duration, colour-palette adherence, prohibited \
terms, mandatory disclosure text -- have ALREADY been measured deterministically \
from the file and are not your concern. Do not comment on them.

Your job is only what requires visual judgement.

Rules you must follow exactly:
1. Reply with a single JSON object and nothing else. No prose, no preamble.
2. For each criterion return one of: "pass", "fail", "uncertain".
3. Use "uncertain" whenever the frames genuinely do not let you decide. \
Choosing "uncertain" routes the asset to a human reviewer, which is the correct \
and safe outcome. It is never penalised. Guessing is.
4. Give a specific, actionable rationale citing what you actually saw and in \
which frame. "Looks fine" is not a rationale.
5. Report confidence between 0.0 and 1.0 reflecting genuine certainty.
6. Never invent detail that is not visible in the supplied frames.

Response schema:
{
  "criteria": [
    {
      "id": "<criterion id>",
      "outcome": "pass" | "fail" | "uncertain",
      "confidence": 0.0-1.0,
      "rationale": "<what you saw, and in which frame>",
      "frame_index": <0-based index of the most relevant frame, or null>
    }
  ],
  "summary": "<two sentences maximum>"
}"""


@dataclass
class VisionReviewOutput:
    """What one vision pass produced, before it becomes a BoardVerdict."""

    criteria: list[CriterionVerdict]
    summary: str
    raw_response: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    parse_failed: bool = False


def encode_frame(path: Path, *, max_edge: int = 768) -> str:
    """Frame -> data URL, downscaled.

    Downscaling is a real cost control, not a nicety: vision pricing scales with
    pixels, and 768px is comfortably enough to judge logo presence and gross
    artifacts. Reviewing six frames at native 1080p would cost several times
    more per take and would not change a single verdict.
    """
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_edge:
            scale = max_edge / max(im.size)
            im = im.resize(
                (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                Image.LANCZOS,
            )
        import io

        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        payload = buf.getvalue()

    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def build_user_prompt(brief: CampaignBrief, frame_count: int) -> str:
    perceptual = [
        c for c in get_profile(brief.compliance_profile) if c.kind is CheckKind.PERCEPTUAL
    ]
    lines = [
        f"Brand: {brief.brand_kit.name}",
        f"Campaign: {brief.title}",
        f"Creative intent: {brief.prompt}",
    ]
    if brief.brand_kit.tone_guidance:
        lines.append(f"Tone guidance: {brief.brand_kit.tone_guidance}")
    if brief.brand_kit.logo_uri:
        lines.append(
            "A brand logo is expected to appear in the creative."
        )

    lines.append(f"\nYou are shown {frame_count} frames sampled evenly across the clip.")
    lines.append("\nAssess exactly these criteria:")
    for c in perceptual:
        lines.append(f'- "{c.id.value}" ({c.label}): {c.description}')

    lines.append(
        "\nReturn the JSON object described in your instructions, covering "
        "every criterion listed above and no others."
    )
    return "\n".join(lines)


def parse_verdict_json(
    text: str, brief: CampaignBrief
) -> tuple[list[CriterionVerdict], str, bool]:
    """Parse the model's reply into criterion verdicts.

    Returns (verdicts, summary, parse_failed). On ANY parsing problem every
    expected perceptual criterion is returned as UNCERTAIN, which escalates the
    take to a human. This function must never be able to produce a PASS it did
    not read.
    """
    perceptual = {
        c.id.value: c
        for c in get_profile(brief.compliance_profile)
        if c.kind is CheckKind.PERCEPTUAL
    }

    payload: dict[str, Any] | None = None
    for candidate in _extract_json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    if payload is None:
        log.warning("Board vision response did not contain parseable JSON.")
        return _all_uncertain(perceptual, "Model response was not valid JSON."), "", True

    entries = payload.get("criteria")
    if not isinstance(entries, list):
        return (
            _all_uncertain(perceptual, "Model response lacked a 'criteria' array."),
            str(payload.get("summary", "")),
            True,
        )

    by_id: dict[str, CriterionVerdict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("id", "")).strip()
        criterion = perceptual.get(cid)
        if criterion is None:
            continue  # model invented a criterion; ignore rather than trust

        outcome = _coerce_outcome(entry.get("outcome"))
        confidence = _coerce_confidence(entry.get("confidence"))
        rationale = str(entry.get("rationale", "")).strip()

        # A confident-sounding FAIL with no reasoning is not actionable and
        # cannot drive a revision, so it degrades to UNCERTAIN.
        if outcome is CriterionOutcome.FAIL and not rationale:
            outcome = CriterionOutcome.UNCERTAIN
            rationale = "Model reported a failure without a rationale; escalating."

        by_id[cid] = CriterionVerdict(
            criterion=CriterionId(cid),
            outcome=outcome,
            kind=CheckKind.PERCEPTUAL,
            severity=criterion.severity,
            rationale=rationale or "No rationale supplied.",
            confidence=confidence,
            evidence_frame=_coerce_frame_index(entry.get("frame_index")),
        )

    # Any criterion the model skipped is UNCERTAIN, not absent.
    for cid, criterion in perceptual.items():
        if cid not in by_id:
            by_id[cid] = CriterionVerdict(
                criterion=CriterionId(cid),
                outcome=CriterionOutcome.UNCERTAIN,
                kind=CheckKind.PERCEPTUAL,
                severity=criterion.severity,
                rationale="Model did not return a verdict for this criterion.",
                confidence=None,
            )

    summary = str(payload.get("summary", "")).strip()
    return list(by_id.values()), summary, False


def _extract_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for match in _JSON_BLOCK.finditer(text or ""):
        candidates.append(match.group(1))
    match = _BARE_OBJECT.search(text or "")
    if match:
        candidates.append(match.group(0))
    return candidates


def _all_uncertain(
    perceptual: dict[str, Any], reason: str
) -> list[CriterionVerdict]:
    return [
        CriterionVerdict(
            criterion=CriterionId(cid),
            outcome=CriterionOutcome.UNCERTAIN,
            kind=CheckKind.PERCEPTUAL,
            severity=criterion.severity,
            rationale=reason,
            confidence=None,
        )
        for cid, criterion in perceptual.items()
    ]


def _coerce_outcome(value: Any) -> CriterionOutcome:
    text = str(value or "").strip().lower()
    if text in {"pass", "passed", "ok", "compliant"}:
        return CriterionOutcome.PASS
    if text in {"fail", "failed", "violation", "non-compliant"}:
        return CriterionOutcome.FAIL
    return CriterionOutcome.UNCERTAIN


def _coerce_confidence(value: Any) -> float | None:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, conf))


def _coerce_frame_index(value: Any) -> str | None:
    try:
        return f"frame:{int(value)}"
    except (TypeError, ValueError):
        return None


def estimate_cost(tokens_in: int | None, tokens_out: int | None) -> float | None:
    if tokens_in is None and tokens_out is None:
        return None
    cost = 0.0
    if tokens_in:
        cost += (tokens_in / 1_000_000) * _DEFAULT_PRICE_PER_MTOK_IN
    if tokens_out:
        cost += (tokens_out / 1_000_000) * _DEFAULT_PRICE_PER_MTOK_OUT
    return round(cost, 6)


def run_vision_review(
    brief: CampaignBrief,
    frame_paths: list[Path],
    *,
    model: str,
    api_key: str | None = None,
    client: Any = None,
) -> VisionReviewOutput:
    """One vision pass over sampled frames. The perceptual half of the Board."""
    if not frame_paths:
        perceptual = {
            c.id.value: c
            for c in get_profile(brief.compliance_profile)
            if c.kind is CheckKind.PERCEPTUAL
        }
        return VisionReviewOutput(
            criteria=_all_uncertain(perceptual, "No frames were available to review."),
            summary="",
            raw_response="",
            model=model,
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
            parse_failed=True,
        )

    images = [encode_frame(p) for p in frame_paths]
    user_prompt = build_user_prompt(brief, len(images))

    response = vision_chat(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        image_data_urls=images,
        api_key=api_key,
        client=client,
    )

    text = extract_chat_text(response)
    tokens_in, tokens_out = extract_token_usage(response)
    criteria, summary, parse_failed = parse_verdict_json(text, brief)

    return VisionReviewOutput(
        criteria=criteria,
        summary=summary,
        raw_response=text,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=estimate_cost(tokens_in, tokens_out),
        parse_failed=parse_failed,
    )


# --------------------------------------------------------------------------
# The provider wrapper -- what puts the verdict in the manifest
# --------------------------------------------------------------------------


class BoardReviewProvider(SyncProvider):
    """A local Genblaze provider whose 'generation' is a compliance verdict.

    Implements the SDK's documented pattern for bringing an LLM call into the
    provenance record: subclass SyncProvider, call `chat()` inside `generate()`,
    return the response as a content-addressed Asset.

    The emitted asset's URL is `text:<sha256>` -- stable content addressing, so
    two identical verdicts over identical frames produce the same address, and
    any change to the verdict changes the manifest.
    """

    name = "notary-board"

    def __init__(
        self,
        brief: CampaignBrief,
        frame_paths: list[Path],
        *,
        api_key: str | None = None,
        client: Any = None,
    ) -> None:
        super().__init__()
        self.brief = brief
        self.frame_paths = frame_paths
        self.api_key = api_key
        self.client = client
        self.last_output: VisionReviewOutput | None = None

    def generate(self, step: Any) -> Any:
        model = getattr(step, "model", None) or "unknown-vision-model"
        try:
            output = run_vision_review(
                self.brief,
                self.frame_paths,
                model=model,
                api_key=self.api_key,
                client=self.client,
            )
        except Exception as exc:  # noqa: BLE001
            # Providers must raise ProviderError with an explicit error_code so
            # the SDK can classify it. A vision outage is a MODEL_ERROR, which
            # makes it eligible for provider-level fallback -- distinct from a
            # quality rejection, which is not.
            raise ProviderError(
                f"Board vision review failed: {exc}", error_code="MODEL_ERROR"
            ) from exc

        self.last_output = output

        body = json.dumps(
            {
                "criteria": [c.model_dump(mode="json") for c in output.criteria],
                "summary": output.summary,
                "model": output.model,
                "parse_failed": output.parse_failed,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

        self._attach_metadata(step, output, digest)
        return self._build_asset(body, digest)

    # -------------------------------------------------------------- internals

    def _attach_metadata(self, step: Any, output: VisionReviewOutput, digest: str) -> None:
        """Record the LLM call's details on the step.

        The SDK's guidance for chat() provenance is to stash call details in
        step metadata. Because this runs inside a real step, that metadata is
        carried by the manifest.
        """
        meta = getattr(step, "metadata", None)
        if meta is None:
            return
        try:
            meta["board_review"] = {
                "model": output.model,
                "tokens_in": output.tokens_in,
                "tokens_out": output.tokens_out,
                "estimated_cost_usd": output.cost_usd,
                "frames_reviewed": len(self.frame_paths),
                "parse_failed": output.parse_failed,
                "verdict_digest": digest,
                "outcomes": {
                    c.criterion.value: c.outcome.value for c in output.criteria
                },
            }
        except (TypeError, AttributeError):  # pragma: no cover - defensive
            log.debug("step.metadata is not writable; skipping board metadata")

    def _build_asset(self, body: str, digest: str) -> Any:
        from ..genblaze_compat import Asset as GBAsset

        url = f"text:{digest}"
        if GENBLAZE_AVAILABLE and GBAsset is not None:
            try:
                return GBAsset(url=url, mime_type="application/json", sha256=digest)
            except TypeError:
                try:
                    return GBAsset(url=url)
                except Exception:  # noqa: BLE001 - fall through to the shim
                    pass

        return _TextAsset(url=url, sha256=digest, body=body)


@dataclass
class _TextAsset:
    """Shim asset for replay/testing when the SDK is absent."""

    url: str
    sha256: str
    body: str
    mime_type: str = "application/json"


# --------------------------------------------------------------------------
# Combining both halves into a decision
# --------------------------------------------------------------------------


def decide(
    deterministic: list[CriterionVerdict],
    perceptual: list[CriterionVerdict],
    *,
    run_id: str,
    take_number: int,
    iterations_remaining: int,
    uncertainty_floor: float = 0.55,
) -> BoardVerdict:
    """Fold both halves of the review into one decision.

    Precedence, strictest first:

    1. Any deterministic blocking FAIL  -> REJECTED. It is measured, it is
       reproducible, and it is fixable by a better prompt. Revise.
    2. Any blocking UNCERTAIN           -> ESCALATED. Ambiguity never ships.
    3. A low-confidence perceptual FAIL -> ESCALATED, not REJECTED. Spending a
       render on a revision driven by a guess is worse than asking a person.
    4. A confident perceptual FAIL      -> REJECTED if revisions remain,
       otherwise ESCALATED. The revision budget must never silently become an
       approval.
    5. Otherwise                        -> VERIFIED.
    """
    criteria = [*deterministic, *perceptual]

    det_fail = [
        c
        for c in deterministic
        if c.outcome is CriterionOutcome.FAIL and c.severity is Severity.BLOCKING
    ]
    uncertain_blocking = [
        c
        for c in criteria
        if c.outcome is CriterionOutcome.UNCERTAIN and c.severity is Severity.BLOCKING
    ]
    perc_fail = [
        c
        for c in perceptual
        if c.outcome is CriterionOutcome.FAIL and c.severity is Severity.BLOCKING
    ]

    low_confidence_fail = [
        c for c in perc_fail if (c.confidence is None or c.confidence < uncertainty_floor)
    ]
    confident_fail = [c for c in perc_fail if c not in low_confidence_fail]

    if det_fail:
        decision = (
            BoardDecision.REJECTED if iterations_remaining > 0 else BoardDecision.ESCALATED
        )
        summary = _summarize(det_fail, "measured compliance failure")
    elif uncertain_blocking:
        decision = BoardDecision.ESCALATED
        summary = _summarize(uncertain_blocking, "unresolved criterion")
    elif low_confidence_fail:
        decision = BoardDecision.ESCALATED
        summary = _summarize(low_confidence_fail, "low-confidence finding")
    elif confident_fail:
        decision = (
            BoardDecision.REJECTED if iterations_remaining > 0 else BoardDecision.ESCALATED
        )
        summary = _summarize(confident_fail, "review failure")
    else:
        decision = BoardDecision.VERIFIED
        summary = (
            f"All {len(criteria)} criteria cleared "
            f"({len(deterministic)} measured, {len(perceptual)} reviewed)."
        )

    if decision is BoardDecision.ESCALATED and iterations_remaining <= 0 and (
        det_fail or confident_fail
    ):
        summary += " Revision budget exhausted; routed to human review."

    return BoardVerdict(
        run_id=run_id,
        take_number=take_number,
        decision=decision,
        criteria=criteria,
        summary=summary,
    )


def _summarize(items: list[CriterionVerdict], label: str) -> str:
    names = ", ".join(c.criterion.value for c in items)
    plural = "s" if len(items) != 1 else ""
    return f"{len(items)} {label}{plural}: {names}."
