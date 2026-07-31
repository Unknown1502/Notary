# Providers and models

Every AI provider and model Notary calls, what it is used for, and why it was chosen over the alternative.

---

## Summary table

| Role | Provider | Model | Modality | Genblaze package |
|---|---|---|---|---|
| Storyboard keyframe | **GMI Cloud** | `seedream-5.0-lite` | Image | `genblaze-gmicloud` |
| Keyframe fallback | **GMI Cloud** | FLUX-class image model | Image | `genblaze-gmicloud` |
| Video generation (primary) | **GMI Cloud** | `kling-image2video-v2.1-master` | Video | `genblaze-gmicloud` |
| Video generation (fallback) | **Luma** | `ray-2` | Video | `genblaze-luma` |
| Board perceptual review | **GMI Cloud** | `qwen2.5-vl-72b-instruct` | Vision → text | `genblaze-core` (`chat()`) |
| Board measured review | *(none — no model)* | — | — | — |

Configured in [`backend/notary/config.py`](../backend/notary/config.py); override any of them by environment variable without touching code.

---

## Storyboard keyframe — GMI Cloud `seedream-5.0-lite`

**What it does.** Produces the single keyframe the video step animates. The pipeline runs with `chain=True`, so this step's asset becomes the video step's input and the manifest records the dependency between them.

**Why an image step at all.** Generating a keyframe first and animating it is both cheaper and more controllable than text-to-video: composition, palette, and logo placement are settled in an image that costs cents, before committing to a video render that costs minutes. It also means a palette failure can be caught on the keyframe rather than after a full render.

**Why `-lite`.** The storyboard is an intermediate artifact that never ships. Paying for a flagship image model to produce something the video model will substantially redraw is waste.

---

## Video generation — GMI Cloud `kling-image2video-v2.1-master`

**What it does.** Animates the approved keyframe into the deliverable clip at the brief's aspect ratio and duration.

**Why image-to-video rather than text-to-video.** The whole review architecture depends on the video inheriting the keyframe's composition and palette. An i2v model constrained by a real starting frame drifts far less than a t2v model working from a prompt, which directly reduces the palette-adherence failure rate — the single most common rejection cause in the seeded runs.

---

## Video fallback — Luma `ray-2`

**What it does.** Takes over when the primary video provider returns `MODEL_ERROR`, stalls, or times out.

**Why a different provider, not just a different model.** A same-provider fallback does not survive the failure mode that actually matters — the provider itself being degraded. Notary's fallback crosses the provider boundary so a GMI Cloud outage does not stop the pipeline.

**How it is wired, and why twice.** `fallback_models=["ray-2"]` is set on the video step so the SDK handles the in-band case. Separately, [`runner.py`](../backend/notary/pipeline/runner.py) catches a *terminal* provider fault and launches a fresh run against the secondary provider, explicitly linked to the failed parent.

The second path exists because whether `fallback_models` may name a model on a *different* provider is undocumented — `docs/features/retry-policy.md` covers transient retry within the submit/poll/fetch lifecycle and is silent on fallback routing ([SPIKES](SPIKES.md) #2). Rather than depend on an unverified behaviour, Notary implements the cross-provider case itself.

That turned out to be the better design regardless: the explicit path makes the provider switch a **visible edge in the lineage graph** instead of an invisible retry inside the SDK. A reviewer looking at a certified asset can see it was produced on the fallback provider, which is exactly the kind of thing a compliance audit asks about.

---

## Board perceptual review — GMI Cloud `qwen2.5-vl-72b-instruct`

**What it does.** The one model call that involves judgement. It receives 3–6 keyframes sampled evenly across the clip plus the perceptual half of the compliance rubric, and returns structured JSON: a per-criterion verdict, a confidence, and a written rationale citing the frame it saw.

**Why a vision model reviews frames, not video.**

1. **Accuracy** — six clean stills produce better judgements about logo legibility and anatomy than one compressed video the model must summarise.
2. **Cost** — the review runs on *every* take including the rejected ones, so it must be cheap. Frames are downscaled to 768px before encoding, which is ample for the questions being asked and a fraction of the tokens.
3. **Explainability** — a finding can point at *the frame*, and the interface can show it. "Possible finger-count irregularity in frame 2" is actionable; "artifacts detected" is not.

**Why it is wrapped as a provider, not called directly.** `chat()` is explicitly not a Pipeline citizen — the SDK docs say *"Not integrated with Pipeline / Step / Asset / manifest."* Left as a bare call, the Board's verdict would sit outside the provenance record, and the obvious challenge ("is the approval part of the thing you sealed?") would have no good answer.

The SDK documents the fix: *"To make a script-writing `chat()` call appear as a step in the manifest… wrap it in a small local `SyncProvider`."* [`BoardReviewProvider`](../backend/notary/board/review.py) is that pattern applied to compliance review. The verdict becomes a content-addressed asset (`text:<sha256>`) produced by a real step, so it is inside the manifest the signature covers.

**Settings that are not negotiable.**

- `temperature=0.0` — a compliance verdict that varies between identical runs is not a verdict.
- Unparseable output → `UNCERTAIN`, never `PASS`. The failure mode of a JSON bug must be "a human looks at it."
- A `FAIL` with no rationale degrades to `UNCERTAIN`, because a finding that cannot explain itself cannot drive a revision.

**Cost accounting.** `chat()` always returns `cost_usd=None` (documented). Notary derives cost from `tokens_in`/`tokens_out` against the model's price in [`review.py`](../backend/notary/board/review.py).

**Alternative considered.** OpenAI `gpt-4o` is a drop-in via `NOTARY_BOARD_VISION_MODEL` and is the better choice if you want the strongest available judgement. Qwen-VL is the default because it keeps the whole generation-and-review path on GMI Cloud credits, which matters for a hackathon deployment a stranger can click.

---

## The half of the Board with no model at all

The most important entry in this document is the one with an empty Provider column.

Aspect ratio, clip duration, brand-palette adherence, prohibited terms, and mandatory disclosure presence are **computed**, in [`backend/notary/board/deterministic.py`](../backend/notary/board/deterministic.py). No inference, no network, no spend.

Palette adherence converts sampled pixels from sRGB into CIE L\*a\*b\* and measures CIE76 ΔE against the nearest brand colour, excluding near-neutral pixels by a chroma floor. Sampling is stride-based rather than random, so the same file always yields the same number.

Three reasons this is not laziness but the correct design:

- **It is more accurate.** Asking a vision model whether a 1:1 video is 16:9 is slower, costlier, and worse than dividing two integers.
- **It is evidence rather than opinion.** A measured rejection is reproducible by anyone holding the file, with no API key and no trust in Notary. That is a materially stronger claim than "our model said so," and it is what makes the rejection defensible in an audit.
- **It bounds the blast radius of model error.** Half the rubric cannot be wrong, so the part that can be wrong is contained and clearly labelled.

CIE76 was chosen over CIEDE2000 deliberately: it is a plain Euclidean distance in Lab, so a reviewer can recompute it in a spreadsheet to audit a rejection. At brand-palette tolerances the two metrics agree on the decision, and auditability wins the tie.

---

## Provider configuration

```bash
NOTARY_GMICLOUD_API_KEY=...
NOTARY_LUMA_API_KEY=...
NOTARY_OPENAI_API_KEY=...            # only if using GPT-4o for the Board

NOTARY_IMAGE_MODEL=seedream-5.0-lite
NOTARY_VIDEO_MODEL=kling-image2video-v2.1-master
NOTARY_VIDEO_FALLBACK_MODEL=ray-2
NOTARY_BOARD_VISION_MODEL=qwen2.5-vl-72b-instruct
```

Missing an optional provider package degrades to "no fallback available" rather than breaking startup — see [`factory.py`](../backend/notary/pipeline/factory.py). Missing the primary provider in `live` mode fails at startup, not on first request, because a misconfiguration discovered four minutes into a render is the worst possible time to discover it.
