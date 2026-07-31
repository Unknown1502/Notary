# SDK spikes

Questions about the Genblaze API that had to be settled before designing around them, because building on an unverified assumption is the fastest way to lose a day. Each records what was found, where it came from, and how the code handles it.

Everything here is isolated in [`backend/notary/genblaze_compat.py`](../backend/notary/genblaze_compat.py) — the rest of the codebase never imports genblaze directly, so if a finding changes, exactly one file changes.

---

## 1. What does `Pipeline.run()` return?

**Question.** The README shows two shapes: an object with `.run` / `.manifest`, and a `(run, manifest)` tuple.

**Status.** Ambiguous in the docs. Not resolved by reading alone.

**Resolution.** Do not bet on either. `normalize_run_result()` accepts both and everything downstream consumes a `RunOutcome` dataclass:

```python
def normalize_run_result(result):
    if isinstance(result, tuple) and len(result) == 2:
        run, manifest = result
        return RunOutcome(run=run, manifest=manifest, raw=result)
    return RunOutcome(run=_first_attr(result, "run"),
                      manifest=_first_attr(result, "manifest"), raw=result)
```

Costs about twenty lines and removes the question permanently.

---

## 2. Can `fallback_models` name a different provider?

**Question.** Does a fallback entry route across provider boundaries, or only to another model on the same provider?

**Status.** **Undocumented.** `docs/features/retry-policy.md` covers transient retry within the submit/poll/fetch lifecycle and says nothing about fallback routing.

**Resolution.** Do not depend on the answer. Two layers:

1. `fallback_models=["ray-2"]` on the video step, so the SDK handles whatever it handles.
2. [`runner._run_with_failover()`](../backend/notary/pipeline/runner.py) catches a *terminal* `ProviderError` classified as a provider fault and launches a fresh run on the second provider, explicitly linked to the failed parent.

Cross-provider failover therefore works either way. The explicit path turned out to be better regardless: it makes the provider switch a **visible edge in the lineage graph** instead of an invisible retry, which is exactly what an audit asks about.

---

## 3. Does `chat()` accept image input?

**Question.** The Board needs vision. Is multimodal input supported, and is the `client=` escape hatch required?

**Status.** **Not documented.** The signature is confirmed:

```python
chat(model, messages=None, *, prompt=None, system=None, tools=None,
     temperature=None, max_tokens=None, api_key=None, client=None,
     retry_on_rate_limit=False, retry_policy=None, **kwargs) -> ChatResponse
```

`messages` accepts `list[ChatMessage] | list[dict]`, and the rate-limiting docs reference "vision calls over video frames" — so it is clearly an anticipated use case, just unspecified.

**Resolution.** Emit the OpenAI-style content-parts shape, which every provider Notary targets for vision speaks:

```python
[{"role": "system", "content": system_prompt},
 {"role": "user", "content": [{"type": "text", "text": prompt},
                              {"type": "image_url", "image_url": {"url": data_url}}]}]
```

`vision_chat()` threads `client` through rather than hiding it, so if a provider rejects this shape the documented escape hatch is one argument away.

**Also confirmed:** `chat()` always returns `cost_usd=None`. Notary derives cost from `tokens_in`/`tokens_out`.

---

## 4. Does `verify()` re-hash the bytes?

**Question.** For a live "recompute against the file in B2" demo, does `Manifest.verify()` check the manifest's internal consistency or re-hash the asset?

**Status.** **Internal consistency.** It is a Mode 1 integrity check: `assert manifest.verify()`.

**Resolution.** That is not the interesting claim, so Notary does the stronger one itself. [`provenance/verify.py`](../backend/notary/provenance/verify.py) streams the object from B2 *at verification time*, recomputes SHA-256 over the returned bytes, and separately checks the Ed25519 signature against the recomputed canonical hash.

The report is a list of independent checks rather than one boolean, because partial failures carry the diagnosis:

- bytes match + signature invalid → the record was re-signed or forged
- bytes differ + signature valid → the asset was swapped under a real certificate
- retention lapsed → still verifiable, no longer immutable

---

## 5. Does `AgentLoop` fit reject → revise?

**Question.** Can it model the Board's cycle with parent-linked runs, or does that need hand-rolling?

**Status.** **Yes, and better than expected.** From `docs/features/agents.md`:

- `AgentLoop(build_pipeline, CallableEvaluator(judge), max_iterations=3)`
- *"Every iteration after the first calls `Pipeline.from_result(prev)` automatically, so each manifest carries `parent_run_id` pointing back to the previous attempt."*
- The factory receives an `AgentContext` with iteration count, prior results, and last feedback
- `loop.stream()` / `loop.astream()` for events; cost aggregated across iterations

**Resolution.** Build the Board on it. This deleted the revision cap, parent linking, streaming plumbing, and cost aggregation Notary would otherwise have written.

**The one trap.** `AgentLoop` stops on evaluator success or the iteration cap. An escalation is neither — "stop, but do not certify." The evaluator returns `passed=False` for an escalation and the runner inspects the verdict afterwards to tell "passed" from "gave up." Returning `passed=True` to end the loop early would ship an unreviewed asset.

---

## 6. Is there a moderation primitive?

**Question.** Not on the original list; found while reading `docs/features/`.

**Status.** **Yes**, and it changed the design. `ModerationHook` runs `check_prompt()` pre-step and `check_output()` post-step. A failure sets `step.status=FAILED`, `error_code=INVALID_INPUT`, and populates `step.metadata["moderation"]` with `stage`, `reason`, `flagged_categories`.

**Why it mattered.** Step metadata is part of the manifest. The original plan wrote a sidecar `verdict.json` and hoped that answered "is the approval in the sealed record?" Running the deterministic screen as a hook means the compliance finding is bound by the same canonical hash as the asset — a materially stronger answer, using a primitive that already existed.

The SDK ships the framework, not the policy: *"a framework for custom screening, not a built-in rubric."* The rubric stays Notary's.

---

## 7. Can a `chat()` call become a manifest step?

**Question.** Not on the original list. The Board's verdict sitting outside provenance was the biggest hole in the design.

**Status.** **Yes, and it is documented.** `docs/features/llm-calls.md` confirms `chat()` is *"Not integrated with Pipeline / Step / Asset / manifest"* — and then gives the remedy: *"To make a script-writing `chat()` call appear as a step in the manifest… wrap it in a small local `SyncProvider`"*, storing the response as an Asset with content addressing (`url=f"text:{digest}"`).

**Resolution.** [`BoardReviewProvider`](../backend/notary/board/review.py) is that pattern applied to compliance review. The verdict became a first-class manifest step, which is the difference between the Board being a bolt-on and being part of the provenance record.

---

## 8. Is Trust Mode 2 implementable?

**Question.** Not on the original list. Found in `docs/features/trust-modes.md`.

**Status.** **Yes.** Mode 2 (Ed25519 signing) is roadmap, but: *"The `signature` and `encryption_scheme` fields on `Manifest` are reserved (excluded from the canonical hash) for forward compatibility."*

**Resolution.** Implemented — see [TRUST-MODEL.md](TRUST-MODEL.md). The exclusion from the canonical hash is what makes it clean: the signature can be written into the manifest without invalidating the hash it commits to.

---

## Verifying these yourself

```bash
pip install genblaze-core genblaze-s3
python - <<'PY'
import genblaze_core as g
print([n for n in dir(g) if not n.startswith("_")])
import inspect; print(inspect.signature(g.chat))
PY
```

Findings 1–3 are defensive (the code works either way). Findings 5–8 are load-bearing and were confirmed against the SDK documentation before the architecture was committed to.
