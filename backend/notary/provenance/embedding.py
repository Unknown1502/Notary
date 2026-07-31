"""Embedding the provenance manifest inside the .mp4 itself.

Why this exists rather than a bare `Mp4Handler()` call
------------------------------------------------------
Genblaze ships `Mp4Handler().embed()` / `.extract()` / `.verify()`, and when
the SDK is importable this module delegates to it. But Notary must be able to
*prove* the embedding claim in replay mode, in CI, and on a machine with no
provider credentials — otherwise "the manifest is embedded in the file" is an
assertion resting on an untested code path, which is precisely the kind of
claim this project exists not to make.

So there is a complete, self-contained implementation here too. It is exercised
by the test suite on every run.

The container format
--------------------
An MP4 is a flat sequence of ISO base media boxes:

    [4-byte big-endian size][4-byte type][payload]  [size][type][payload]  ...

The `uuid` box is the format's standard extension mechanism: type `uuid`
followed by a 16-byte identifier chosen by the writer, then arbitrary payload.
A compliant parser that does not recognise the identifier skips the box by its
size field. Appending one at top level is therefore safe — players ignore it,
and the video plays exactly as before.

The circularity, and how it is resolved
---------------------------------------
A manifest that commits to the hash of its own file cannot be embedded in that
file: writing it changes the bytes, invalidating the hash it just committed to.

The resolution is that the embedded manifest commits to the hash of the media
**excluding the manifest box**. Verification strips the box, hashes what is
left, and compares. That yields a genuinely self-consistent embedded record:

    embed:   media_sha = sha256(original)         -> manifest.asset_sha256
             file      = original + uuid_box(manifest)
    verify:  stripped  = file - uuid_box
             sha256(stripped) == manifest.asset_sha256

The certificate separately records `sha256` of the *served* file, so a
downloader can hash what they fetched and match it. Two hashes, two questions,
both answerable — see docs/TRUST-MODEL.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Identifies a Notary manifest box. Fixed forever: changing it orphans every
# manifest previously embedded.
NOTARY_UUID = bytes.fromhex("6e6f74617279763100000000000000a1")

BOX_HEADER = 8
UUID_LEN = 16
_MAX_32BIT_BOX = 0xFFFFFFFF


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddedManifest:
    manifest: dict[str, Any]
    box_offset: int
    box_size: int

    @property
    def asset_sha256(self) -> str | None:
        value = self.manifest.get("asset_sha256")
        return value.lower() if isinstance(value, str) else None


# --------------------------------------------------------------------------
# Box walking
# --------------------------------------------------------------------------


def iter_boxes(data: bytes):
    """Yield (offset, size, type) for each top-level box.

    Stops rather than raising on a malformed tail: a truncated download should
    degrade to "no manifest found", not crash certification.
    """
    offset = 0
    total = len(data)

    while offset + BOX_HEADER <= total:
        (size,) = struct.unpack_from(">I", data, offset)
        box_type = data[offset + 4 : offset + 8]

        if size == 0:
            # Box extends to end of file.
            yield offset, total - offset, box_type
            return
        if size == 1:
            # 64-bit extended size follows the type field.
            if offset + 16 > total:
                return
            (size,) = struct.unpack_from(">Q", data, offset + 8)
        if size < BOX_HEADER or offset + size > total:
            log.debug("stopping box walk at malformed box @%d (size=%d)", offset, size)
            return

        yield offset, size, box_type
        offset += size


def looks_like_mp4(data: bytes) -> bool:
    """A real ISO-BMFF file opens with an `ftyp` box."""
    for _, _, box_type in iter_boxes(data):
        return box_type == b"ftyp"
    return False


def _find_manifest_box(data: bytes) -> tuple[int, int] | None:
    for offset, size, box_type in iter_boxes(data):
        if box_type != b"uuid":
            continue
        start = offset + BOX_HEADER
        if data[start : start + UUID_LEN] == NOTARY_UUID:
            return offset, size
    return None


# --------------------------------------------------------------------------
# Embed / extract / strip / verify — bytes level
# --------------------------------------------------------------------------


def embed_bytes(data: bytes, manifest: dict[str, Any]) -> bytes:
    """Append the manifest as a `uuid` box. Returns the new file bytes.

    Any previously embedded Notary box is removed first, so re-embedding
    replaces rather than accumulating.
    """
    if not looks_like_mp4(data):
        raise EmbeddingError(
            "payload does not start with an ftyp box; refusing to embed into "
            "something that is not an MP4"
        )

    data = strip_bytes(data)

    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")

    box_size = BOX_HEADER + UUID_LEN + len(payload)
    if box_size > _MAX_32BIT_BOX:
        raise EmbeddingError(f"manifest box too large ({box_size} bytes)")

    box = struct.pack(">I", box_size) + b"uuid" + NOTARY_UUID + payload
    return data + box


def strip_bytes(data: bytes) -> bytes:
    """Return the file with any Notary manifest box removed.

    This is what verification hashes, and it is why an embedded manifest can
    commit to its own media without circularity.
    """
    located = _find_manifest_box(data)
    if located is None:
        return data
    offset, size = located
    return data[:offset] + data[offset + size :]


def extract_bytes(data: bytes) -> EmbeddedManifest | None:
    located = _find_manifest_box(data)
    if located is None:
        return None

    offset, size = located
    start = offset + BOX_HEADER + UUID_LEN
    payload = data[start : offset + size]

    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmbeddingError(f"embedded manifest is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise EmbeddingError("embedded manifest is not a JSON object")

    return EmbeddedManifest(manifest=manifest, box_offset=offset, box_size=size)


def verify_bytes(data: bytes) -> tuple[bool, str]:
    """Check an embedded manifest against the media it is carried by.

    Returns (passed, human-readable detail). Never raises for ordinary
    failures — a missing or corrupt manifest is a verification result, not an
    exception.
    """
    try:
        embedded = extract_bytes(data)
    except EmbeddingError as exc:
        return False, str(exc)

    if embedded is None:
        return False, "no embedded Notary manifest found in this file"

    declared = embedded.asset_sha256
    if not declared:
        return False, "embedded manifest does not declare asset_sha256"

    observed = hashlib.sha256(strip_bytes(data)).hexdigest()
    if observed == declared:
        return True, (
            f"embedded manifest matches its media: sha256 {observed} over "
            f"{len(data) - embedded.box_size:,} bytes (manifest box excluded)"
        )

    return False, (
        f"embedded manifest declares {declared} but the media hashes to "
        f"{observed} — the video was altered after the manifest was embedded"
    )


# --------------------------------------------------------------------------
# File-level convenience
# --------------------------------------------------------------------------


def embed_file(path: Path, manifest: dict[str, Any], *, output: Path | None = None) -> Path:
    target = output or path
    target.write_bytes(embed_bytes(path.read_bytes(), manifest))
    return target


def extract_file(path: Path) -> EmbeddedManifest | None:
    return extract_bytes(path.read_bytes())


def verify_file(path: Path) -> tuple[bool, str]:
    return verify_bytes(path.read_bytes())


def media_digest(data: bytes) -> str:
    """SHA-256 of the media content, ignoring any embedded manifest box.

    Stable across embed/re-embed, which is what makes it usable as the value a
    manifest commits to.
    """
    return hashlib.sha256(strip_bytes(data)).hexdigest()


# --------------------------------------------------------------------------
# SDK delegation
# --------------------------------------------------------------------------


def embed_with_sdk(path: Path, manifest_obj: Any) -> bool:
    """Try Genblaze's `Mp4Handler().embed()`.

    Returns whether the SDK handled it. Notary always falls back to the local
    implementation, so a signature difference degrades to "still embedded, by
    us" rather than "silently not embedded" — which was the original defect
    this module was written to fix.
    """
    try:
        from genblaze_core import Mp4Handler  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return False

    try:
        Mp4Handler().embed(str(path), manifest_obj)
    except Exception as exc:  # noqa: BLE001
        log.info(
            "Mp4Handler().embed() unavailable or rejected the call (%s); "
            "using Notary's own uuid-box embedding",
            exc,
        )
        return False

    log.info("manifest embedded via genblaze Mp4Handler")
    return True
