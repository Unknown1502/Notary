# How Notary uses Backblaze B2 and Genblaze

The submission asks for this explicitly, so this document answers it directly and in detail: what each is used for, which specific primitives, and — the part that matters — why neither is swappable for something else.

---

# Part 1 — Genblaze

## Primitives used

| Primitive | Where | What it does for Notary |
|---|---|---|
| `Pipeline(..., chain=True)` | [`pipeline/factory.py`](../backend/notary/pipeline/factory.py) | Keyframe image chained into video; the manifest records the dependency |
| `AgentLoop` + `CallableEvaluator` | [`pipeline/runner.py`](../backend/notary/pipeline/runner.py) | The generate → review → revise cycle, with automatic parent linking |
| `ModerationHook` | [`board/moderation.py`](../backend/notary/board/moderation.py) | Deterministic compliance screen, findings land in step metadata |
| `SyncProvider` (subclassed) | [`board/review.py`](../backend/notary/board/review.py) | Wraps the vision `chat()` call so the verdict becomes a manifest step |
| `chat()` | [`genblaze_compat.py`](../backend/notary/genblaze_compat.py) | The perceptual review call itself |
| `from_result()` / `parent_run_id` | via `AgentLoop`, and explicitly on failover | Lineage across revisions and provider switches |
| `ObjectStorageSink` + `S3StorageBackend.for_backblaze()` | [`pipeline/factory.py`](../backend/notary/pipeline/factory.py) | Persists generated assets to the B2 workbench during a run |
| `Manifest` + canonical hash | [`provenance/`](../backend/notary/provenance/) | The provenance record; its reserved `signature` field carries Trust Mode 2 |
| `Modality` | throughout | Step typing |
| `ProviderError` + `error_code` | [`board/review.py`](../backend/notary/board/review.py), runner | Distinguishes a provider fault from a quality failure |

---

## 1. `AgentLoop` is the Board's control flow

This is the deepest use of the SDK in the project. `AgentLoop` runs generate → evaluate → refine until an evaluator passes, and per the SDK docs *"every iteration after the first calls `Pipeline.from_result(prev)` automatically, so each manifest carries `parent_run_id` pointing back to the previous attempt."*

That one behaviour replaces an entire subsystem Notary would otherwise have written: the revision cap, the parent linking, per-iteration streaming, and cost aggregation. What Notary supplies is the only thing the SDK cannot know — **what "good" means**:

```python
evaluator = BoardEvaluator(review_fn=self._review_iteration, max_iterations=3)
loop = AgentLoop(pipeline_factory, CallableEvaluator(evaluator), max_iterations=3)
result = loop.run(sink=sink, timeout=600)
```

The evaluator returns `passed` plus `feedback`, and that feedback string is handed to the next iteration's pipeline factory. That *is* the verdict-conditioned revision: iteration N+1 is built from the specific reasons iteration N failed, not a reroll of the same prompt.

**One subtlety that decides correctness.** `AgentLoop` stops on evaluator success or on the iteration cap. An escalation is neither — it means "stop, but do not certify." So [`BoardEvaluator`](../backend/notary/board/evaluator.py) reports `passed=False` for an escalation and the runner inspects the verdict after the loop to distinguish "passed" from "gave up." Reporting an escalation as `passed=True` to end the loop early would ship an unreviewed asset, which is the exact failure this product exists to prevent.

## 2. `ModerationHook` puts the compliance screen inside the manifest

The SDK runs moderation at two lifecycle points, and a failure populates `step.metadata["moderation"]` with `stage`, `reason`, and `flagged_categories`. **Step metadata is part of the manifest** — which is why the deterministic screen is a hook rather than a function called afterwards.

```python
class BrandGuardrailHook(ModerationHook):
    def check_prompt(self, prompt, params=None):    # before any provider is billed
        ...  # prohibited terms, mandatory disclosure
    def check_output(self, asset=None, step=None):  # after generation, before caching
        ...  # aspect ratio, duration, palette dE
```

`check_prompt` is a real cost control, not a formality: a brief missing its mandatory safety disclosure cannot produce a compliant asset no matter how good the render is, so failing there saves a full Kling render. The seeded pharma run demonstrates exactly this path.

The SDK ships the *framework*, not the policy — its docs are explicit that it is *"a framework for custom screening, not a built-in rubric."* The compliance rubric is Notary's; the manifest integration is free.

## 3. Wrapping `chat()` as a provider — the verdict in the record

`chat()` is documented as **not** a Pipeline citizen: *"Not integrated with Pipeline / Step / Asset / manifest."* That is a real problem for a product whose thesis is that approvals should be part of the sealed record.

The SDK documents the remedy, and Notary follows it: *"To make a script-writing `chat()` call appear as a step in the manifest (so provenance covers the words as well as the downstream media), wrap it in a small local `SyncProvider`."*

```python
class BoardReviewProvider(SyncProvider):
    name = "notary-board"

    def generate(self, step):
        output = run_vision_review(self.brief, self.frame_paths, model=step.model)
        body = json.dumps({...}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode()).hexdigest()
        step.metadata["board_review"] = {"model": ..., "tokens_in": ..., "outcomes": {...}}
        return Asset(url=f"text:{digest}", mime_type="application/json", sha256=digest)
```

Now the verdict is a content-addressed asset produced by a step with a recorded model and parameters, inside the run whose manifest gets signed. **The approval is in the record, not beside it.**

## 4. `Manifest`'s reserved `signature` field — implementing Trust Mode 2

Covered fully in [TRUST-MODEL.md](TRUST-MODEL.md). The short version: the SDK reserves `signature` on `Manifest` and excludes it from the canonical hash *"for forward compatibility"* with Trust Mode 2, which is on its roadmap. Notary implements it with Ed25519, the algorithm the SDK names as the intended default.

## 5. Why an orchestration SDK is genuinely the right tool

A single-model wrapper cannot express what Notary does. The workflow is:

> screen this brief before spending anything → render a keyframe → chain it into video → fail over to a second provider if the first stalls → measure the output → review it with a vision model whose verdict joins the provenance record → if it fails, revise using the written reasons and re-run with the parent linked → cap the attempts → and if the machine is not confident, stop and ask a person.

Every arrow in that sentence is an SDK primitive. That chain *is* the product.

---

# Part 2 — Backblaze B2

## Layout

```
notary-vault/          ← Object Lock enabled AT CREATION, COMPLIANCE mode
  vault/{tenant}/{campaign}/{asset_id}/
      asset.mp4          media with the manifest embedded (ISO-BMFF uuid box)
      manifest.json      provenance, canonical-hashed
      verdict.json       full Board finding + human sign-off
      certificate.json   signed envelope binding all of it
      thumbnail.jpg

notary-workbench/      ← no lock, Lifecycle Rule expires after N days
  workbench/{tenant}/{run_id}/
      take-01.mp4, take-01-thumb.jpg
      take-01/frame-00.jpg … frame-04.jpg
      take-01/verdict.json       ← outlives the media it describes

  cache/{sha[0:2]}/{sha[2:4]}/{sha}.ext    content-addressable
  replay/{session_id}/events.ndjson        captured SSE streams
```

## Three key strategies, three reasons

**Vault — hierarchical.** The query that matters is an audit request. `vault/acme-pharma/cmp-q3/` returns every certified asset in a campaign; one level deeper returns one asset's *complete* sealed record — media, provenance, verdict, and signature — as a single prefix. Producing a legal response is a listing, not a join.

**Workbench — run-scoped.** Everything for a run expires together, which makes the Lifecycle Rule a prefix age rule that cannot orphan bytes. Note that rejected takes lose their media and **keep their `verdict.json`**: the expensive thing is the video, the accountable thing is the reasoning, and they have different lifetimes.

**Cache — content-addressable.** Identical bytes deduplicate to one object, so re-running an unchanged brief costs a HEAD request instead of a render. Two-level fan-out keeps any single prefix bounded.

## Object Lock: the feature the product is built on

```python
self.client.create_bucket(Bucket=vault, ObjectLockEnabledForBucket=True)

self.client.put_object(
    Bucket=vault, Key=key, Body=body,
    ObjectLockMode="COMPLIANCE",
    ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=retention),
)
```

Compliance-mode retention is enforced **by the storage layer, not by application logic**. Once written, an object cannot be overwritten or deleted before its retention date by anyone — not an admin, not the account owner, not an attacker holding stolen application keys.

That is the difference between *"we keep a record"* and *"the record cannot be revised after the fact,"* and only the second one is an audit trail. An application-layer convention cannot make that claim, because the application is exactly what an insider would modify.

It also pairs precisely with the signature to close the trust gap. Genblaze's own docs note that Mode 1 integrity can be defeated by regenerating a self-consistent manifest. Object Lock stops the sealed record being *replaced*; the Ed25519 signature stops a forged record being *attributed to us*. Neither alone is sufficient; together they are coherent.

**Two operational facts that bite.** Object Lock cannot be retrofitted — [`bootstrap_b2.py`](../scripts/bootstrap_b2.py) enables it at creation and hard-fails if verification shows it off. And retention defaults to **7 days** in development on purpose: a long retention on a dev bucket produces permanently undeletable test garbage within an hour, and the bootstrap script demands confirmation above 30 days.

## Where the SDK stops and boto3 starts

Genblaze's `ObjectStorageSink` handles the happy path — generated assets and manifests persisted to B2 as a pipeline runs — and Notary uses it for exactly that. But certification needs an Object Lock retention header on PutObject, which the sink does not expose, so [`storage/b2.py`](../backend/notary/storage/b2.py) drops to the S3 API for the seal, plus bucket bootstrap, lifecycle, and CORS.

Using the SDK where it fits and the underlying API where it does not is the honest answer, and it is why the immutability claim holds.

## Promotion is read-then-write, deliberately

`copy_into_vault` streams the bytes through the process rather than issuing a server-side `CopyObject`. That is slower and chosen anyway: it lets Notary compute the SHA-256 it is about to certify **from the exact bytes it seals**. Certifying a digest computed earlier at a different layer would leave a gap between what was hashed and what was stored.

## B2 as the system of record — and the proof

Notary has **no database.** The manifest plus the sealed vault objects are the truth; the in-process store is a cache over it.

That is a claim, so it is tested rather than asserted: on startup, [`store.rehydrate_from_b2()`](../backend/notary/store.py) rebuilds the entire certificate index by listing `vault/` and parsing the `certificate.json` files already there. The library survives a cold restart with no database because every certificate is read back out of the bucket it was sealed into.

## Serving

Certified media is served from durable B2 URLs straight to `<video>` — no signing proxy on the playback path. Credential-free means publicly readable, which is right for published marketing creative and wrong for anything sensitive; `presigned_url()` is the alternative and a real deployment with confidential assets should use a private bucket with signed URLs. Knowing which one you are running is the point.

CORS is configured by the bootstrap script with `GET`/`HEAD` and exposed `Content-Range`/`Accept-Ranges`, because without it browser video playback from B2 fails with an opaque error and no server-side symptom.

## Also used

- **Versioning** on brand-kit sources, so a run resolves the exact inputs it used.
- **Lifecycle Rules** expiring `workbench/` and aborting incomplete multipart uploads after a day.
- **`replay/`** holding captured SSE streams, which is what makes the deployed app usable by a stranger without a four-minute wait.

## Not implemented

**B2 Event Notifications** would turn certification into an event-driven fan-out — sealing an object triggers a downstream worker rather than the in-process orchestration used today. That is the correct architecture at scale and it is on the roadmap, not in the build. It is listed here rather than implied.
