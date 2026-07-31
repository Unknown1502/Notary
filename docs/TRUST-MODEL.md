# Trust model

What Notary proves, what it does not, and where it breaks. Written so a reviewer does not have to find the gaps themselves.

---

## Genblaze's three modes

From the SDK's `docs/features/trust-modes.md`:

| Mode | Claim | SDK | Notary |
|---|---|---|---|
| **1 — Integrity** | The manifest is unchanged; assets match their committed hashes | Shipped | ✅ Used |
| **2 — Authenticated integrity** | *Who* attested to it. Signing "via a pluggable interface with Ed25519 as the default" | Roadmap | ✅ **Implemented** |
| **3 — Standards-verifiable** | C2PA, verifiable by external tooling (Adobe, Microsoft) | Roadmap | ❌ Not implemented |

Mode 1 explicitly cannot prove *"That a specific party produced the manifest. Anyone with the SDK can build a self-consistent manifest from arbitrary inputs."* And the attack is spelled out: *"A tamperer can modify the asset, recompute the manifest, re-embed, and produce a manifest that verifies against itself."*

## Implementing Mode 2

The SDK left the socket open:

> The `signature` and `encryption_scheme` fields on `Manifest` are reserved (excluded from the canonical hash) for forward compatibility.

A signature cannot be part of what it signs — writing it into the manifest would change the manifest and invalidate the signature. Excluding the field from the canonical hash solves that, so the protocol needs no wrapper format:

1. Genblaze computes `canonical_hash` over the manifest, signature excluded
2. Notary signs those bytes with Ed25519
3. The signature block goes into the reserved field
4. `canonical_hash` is unchanged, because the field is excluded
5. A verifier recomputes the hash, then checks the signature over it

**What is signed, precisely.** The lowercase hex digest, as ASCII bytes. Hex rather than raw-decoded bytes so a third-party verifier in another language has an unambiguous spec. Implementation in [`provenance/signing.py`](../backend/notary/provenance/signing.py).

---

## The four claims, ranked by strength

**1. Measured findings are reproducible.** Strongest, and it requires no trust in Notary at all. Palette ΔE, aspect ratio, duration, prohibited terms are computed from the file; anyone holding it can recompute them. Independent of signatures, storage, and our honesty.

**2. The bytes are unchanged since certification.** `POST /api/certificates/{id}/verify` streams the object from B2 *now*, recomputes SHA-256 over what comes back, and compares. Nothing stored is trusted.

**3. The sealed record cannot be altered before retention lapses.** Object Lock COMPLIANCE is enforced by storage, not application logic — not alterable by an admin, the account owner, or someone with stolen keys.

**4. This record was attested by the holder of key `notary-dev-2026`.** Ed25519 over the canonical hash. Weakest of the four, because it is only as good as key custody.

---

## What is explicitly not claimed

- **Not that the media depicts anything real.** Provenance records what produced a file, not whether the world matches it.
- **Not that the model behaved correctly.** The manifest records which model ran, not that its output was good.
- **Not that the perceptual verdicts are correct.** They are model judgements. That is why uncertainty escalates.
- **Not C2PA.** Mode 3 is roadmap and appears nowhere in the interface.
- **Not that "approved" means legally compliant.** The Board screens mechanical failures. It does not adjudicate whether a claim is substantiated.

## The regenerate-a-manifest question

The obvious challenge: *"couldn't you just produce a different asset with its own valid manifest?"*

Yes — and that is true of Mode 1 for everyone. Two things constrain it:

**Object Lock** means the *existing* sealed record cannot be altered or replaced within its retention window. An attacker can create a new record; they cannot revise this one.

**The signature** means a forged record cannot be attributed to us without the private key. A self-consistent manifest is easy; one that verifies against our published public key is not.

Neither alone is sufficient. Object Lock without signing preserves a record whose authorship is unprovable. Signing without Object Lock proves authorship of something that could be swapped. Together they are coherent, which is why the product uses both.

---

## Threat model

| Threat | Result | Why |
|---|---|---|
| Edit the video in the vault | **Blocked** | Object Lock refuses the write |
| Delete a certified asset early | **Blocked** | Compliance-mode retention; no one can override |
| Swap the asset and re-hash a new manifest | **Detected** | Signature fails against the published key |
| Alter `verdict.json` after approval | **Blocked** | Sealed under the same retention as the asset |
| Replay a valid signature onto different content | **Detected** | Signature binds the manifest hash, which binds the asset digest |
| Present a Mode 1 certificate as Mode 2 | **Detected** | `trust_mode` is derived from signature presence; verification reports the absence explicitly |
| **Steal the signing key** | **Not mitigated** | Full compromise. See below. |
| Compromise the running service before sealing | **Not mitigated** | Anything upstream of the seal can be manipulated |
| Vision model is simply wrong | **Partly mitigated** | Measured criteria are unaffected; uncertainty escalates; but a confidently wrong pass can certify |

## The key is the weak point, and it is a file

The private key lives at `keys/notary-ed25519.pem`, unencrypted, gitignored. Anyone who reads it can issue certificates in this deployment's name, indistinguishably.

That is acceptable for a hackathon build and **not acceptable in production**, where the key belongs in a KMS or HSM and should never touch a filesystem. Notary is structured for that swap — `SigningIdentity` is the only thing that touches private key material, so a KMS backend replaces one class.

Related gaps, named rather than hidden: there is no key rotation (rotating invalidates prior signatures, so real deployment needs overlapping validity and a key directory), no revocation, and no transparency log. Mode 2 authenticates a key, not an organisation. Binding a key to a legal entity is what certificate authorities and C2PA exist for, and it is Mode 3's job.

## Fail closed

`NOTARY_REQUIRE_SIGNING=true` (the default) makes certification **abort** if the key is unavailable, rather than emit an unsigned certificate. A Mode 1 certificate rendered by a UI advertising Mode 2 would train users to trust a badge that sometimes means nothing, which is worse than no certificate.

---

## Verifying independently

Every certificate publishes what a third party needs, so nobody has to take our word for it:

```
Asset integrity:  download asset_url, sha256 it, compare to `sha256`
Signature:        base64-decode signature.public_key -> raw 32-byte Ed25519 key
                  base64-decode signature.signature
                  verify over the ASCII bytes of lowercase `manifest_hash`
```

Both are doable with standard tooling and without Notary. That is the point of publishing them.
