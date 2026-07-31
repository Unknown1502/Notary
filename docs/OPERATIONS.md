# Operations

## Modes

| Mode | Generates | Reads B2 | Credentials | Use |
|---|---|---|---|---|
| `replay` | No | No | None | Default. Public demo, CI, frontend work. |
| `hybrid` | No | Yes | B2 only | Verify real sealed artifacts without paying for a render to reach them. |
| `live` | Yes | Yes | B2 + providers | Production. |

Mode is visible at `GET /api/health` and in the UI header. A replayed run is labelled as one everywhere it appears.

## First run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"
python scripts/seed_demo.py
uvicorn notary.main:app --app-dir backend --reload
```

Zero credentials. If `pytest` fails on an unrelated plugin import, a clean venv is the fix — a broken global plugin can break collection.

## Going live

```bash
cp .env.example .env          # fill in credentials
python scripts/bootstrap_b2.py    # buckets, Object Lock, lifecycle, CORS
python scripts/generate_key.py    # Ed25519 signing key
NOTARY_MODE=live uvicorn notary.main:app --app-dir backend
```

Order matters. `bootstrap_b2.py` must run before the first live review because **Object Lock cannot be retrofitted** — it verifies this and hard-fails if the vault bucket does not have it enabled.

`ffmpeg` and `ffprobe` must be on PATH. Without them, frame extraction fails, visual criteria report `UNCERTAIN`, and everything escalates to a human — degraded but never silently passing.

## Retention

`NOTARY_VAULT_RETENTION_DAYS` defaults to **7**. Compliance-mode objects cannot be deleted before retention lapses by anyone, including the account owner. A 10-year default on a dev bucket produces permanently undeletable test garbage within an hour. The bootstrap script demands confirmation above 30 days.

Production values are a legal question, not an engineering one — pharma promotional records are commonly retained for years.

## Key custody

**Development (what ships here).** Unencrypted PEM at `keys/notary-ed25519.pem`, gitignored, `chmod 600` where the OS allows.

**Production.** The key belongs in a KMS/HSM and should never touch a filesystem. `SigningIdentity` in [`provenance/signing.py`](../backend/notary/provenance/signing.py) is the only thing that touches private key material, so a KMS backend replaces one class:

```python
class KmsSigningIdentity:
    def sign(self, canonical_hash: str) -> bytes: ...   # remote signing
```

Anyone holding the private key can issue certificates in this deployment's name. Rotation invalidates prior signatures, so real rotation needs a key directory with overlapping validity and `key_id` resolution at verification time — not implemented.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `NOTARY_MODE` | `replay` | `live` fails fast if credentials are missing |
| `NOTARY_B2_KEY_ID` / `_APPLICATION_KEY` | — | Required outside replay |
| `NOTARY_B2_ENDPOINT` | `s3.us-west-004…` | Must match your bucket region |
| `NOTARY_B2_BUCKET_VAULT` | `notary-vault` | **Object Lock at creation** |
| `NOTARY_B2_BUCKET_WORKBENCH` | `notary-workbench` | No lock |
| `NOTARY_B2_PUBLIC_VAULT_BASE` | — | Friendly URL base for credential-free playback |
| `NOTARY_VAULT_RETENTION_DAYS` | `7` | Irreversible |
| `NOTARY_WORKBENCH_EXPIRY_DAYS` | `3` | Lifecycle Rule |
| `NOTARY_MAX_BOARD_ITERATIONS` | `3` | Draft + 2 revisions |
| `NOTARY_REQUIRE_SIGNING` | `true` | Fail closed |
| `NOTARY_CORS_ORIGINS` | localhost | Must include the deployed frontend origin |

## Deploying

Backend is one process; run it behind a proxy that does **not** buffer SSE:

```nginx
location /api/reviews/ {
    proxy_pass http://backend;
    proxy_buffering off;          # required, or the stream arrives all at once
    proxy_read_timeout 600s;      # a render can take minutes
    proxy_set_header Connection '';
    proxy_http_version 1.1;
}
```

Frontend is static: `npm run build` and serve `dist/`. Set `VITE_API_BASE` if it is not same-origin.

For a public demo, deploy in `replay` or `hybrid`. `live` on a public URL means strangers spend your provider credits.

## Scaling

The current shape is one node. Two things are node-local:

- **The event bus** is in-process, so a client on instance B would not see events from instance A. `EventBus`'s publish/subscribe interface is a drop-in for Redis pub/sub.
- **In-flight sessions** are in memory. Durable state is entirely in B2 — `store.rehydrate_from_b2()` rebuilds the certificate index on startup by listing the vault, so a restart loses only runs that were mid-flight, and those fail visibly rather than hanging.

Longer term, B2 Event Notifications should drive certification fan-out instead of in-process orchestration.

## Runbook

**Certification fails with `InvalidBucketState`.** Object Lock is not enabled on the vault bucket and cannot be added. Create a new bucket and re-run `bootstrap_b2.py`.

**Video will not play in the browser, no server errors.** CORS. Re-run `bootstrap_b2.py`, and check `NOTARY_CORS_ORIGINS` includes the frontend origin.

**Every visual criterion reports UNCERTAIN.** `ffmpeg` is missing. `GET /api/health` reports `ffmpeg_available`.

**Everything escalates.** Expected when frames are unavailable, the vision model is unreachable, or its output will not parse. `parse_failed` appears in step metadata. This is the system failing safe.

**Certification aborts with `SigningUnavailable`.** No signing key and `require_signing=true`. Run `scripts/generate_key.py`, or set `NOTARY_REQUIRE_SIGNING=false` to accept Mode 1 certificates.

**The stream stalls, then delivers everything at once.** Proxy buffering. See the nginx block above.

**The library is empty after a restart in live mode.** Rehydration failed. Check credentials and the `vault/` prefix; `GET /api/stats` reports `rehydrated`.

## Costs

Per certified take, roughly: one storyboard image, one video render, and one vision review over 5 downscaled frames. Rejected takes cost the same as accepted ones, which is why `check_prompt()` screening before any provider call is a real control and not a formality — a brief missing its mandatory disclosure is rejected for free.

Vision cost is derived from token counts because `chat()` always reports `cost_usd=None`.
