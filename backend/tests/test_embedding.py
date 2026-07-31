"""Tests for manifest embedding.

These exist because the README claims the manifest is embedded in the .mp4.
A claim in documentation is worth nothing unless something fails when it stops
being true, so this file is what makes that sentence load-bearing.

The two properties that matter:

  * **Self-consistency.** An embedded manifest commits to the hash of its own
    media (excluding the manifest box), so extract-and-verify detects any
    later alteration of the video.
  * **Player safety.** Embedding must not corrupt the container. The structural
    tests assert that every original box survives, in order, and that the file
    still opens with `ftyp` — which is what a player checks.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from notary.provenance import embedding as emb


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def minimal_mp4(media: bytes = b"\x11" * 512) -> bytes:
    """A structurally valid ISO-BMFF skeleton: ftyp + moov + mdat."""
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    moov = box(b"moov", box(b"mvhd", b"\x00" * 100))
    mdat = box(b"mdat", media)
    return ftyp + moov + mdat


@pytest.fixture
def manifest() -> dict:
    return {
        "schema": "notary.manifest/v1",
        "run_id": "run-test-001",
        "provider": "gmicloud",
        "model": "kling-image2video-v2.1-master",
        "prompt": "a calm coastal morning",
    }


def sign_manifest_for(data: bytes, manifest: dict) -> dict:
    """Bind a manifest to the media it will be embedded in."""
    return {**manifest, "asset_sha256": emb.media_digest(data)}


# --------------------------------------------------------------------------
# Container sanity
# --------------------------------------------------------------------------


def test_fixture_is_a_valid_mp4():
    data = minimal_mp4()
    assert emb.looks_like_mp4(data)
    assert [t for _, _, t in emb.iter_boxes(data)] == [b"ftyp", b"moov", b"mdat"]


def test_refuses_to_embed_into_a_non_mp4(manifest):
    with pytest.raises(emb.EmbeddingError):
        emb.embed_bytes(b"this is not an mp4 at all", manifest)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_embed_then_extract_round_trips(manifest):
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))

    recovered = emb.extract_bytes(embedded)
    assert recovered is not None
    assert recovered.manifest["run_id"] == "run-test-001"
    assert recovered.manifest["model"] == "kling-image2video-v2.1-master"


def test_file_with_no_manifest_extracts_none():
    assert emb.extract_bytes(minimal_mp4()) is None


def test_embedding_preserves_every_original_box(manifest):
    """Player safety: the container must still parse, unchanged, in order."""
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))

    original_boxes = [t for _, _, t in emb.iter_boxes(data)]
    new_boxes = [t for _, _, t in emb.iter_boxes(embedded)]

    assert new_boxes == original_boxes + [b"uuid"]
    assert embedded.startswith(data), "original bytes must be untouched"
    assert emb.looks_like_mp4(embedded)


def test_stripping_restores_the_original_bytes_exactly(manifest):
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))
    assert emb.strip_bytes(embedded) == data


def test_re_embedding_replaces_rather_than_accumulates(manifest):
    """Otherwise a revised manifest would leave the old one in the file."""
    data = minimal_mp4()
    once = emb.embed_bytes(data, sign_manifest_for(data, manifest))
    twice = emb.embed_bytes(once, sign_manifest_for(data, {**manifest, "run_id": "run-2"}))

    uuid_boxes = [t for _, _, t in emb.iter_boxes(twice) if t == b"uuid"]
    assert len(uuid_boxes) == 1

    recovered = emb.extract_bytes(twice)
    assert recovered is not None
    assert recovered.manifest["run_id"] == "run-2"
    assert emb.strip_bytes(twice) == data


def test_media_digest_is_stable_across_embedding(manifest):
    """The value a manifest commits to must not change by being committed."""
    data = minimal_mp4()
    before = emb.media_digest(data)
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))
    assert emb.media_digest(embedded) == before
    assert before == hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Verification — the tamper-evidence claim
# --------------------------------------------------------------------------


def test_untampered_file_verifies(manifest):
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))

    passed, detail = emb.verify_bytes(embedded)
    assert passed, detail
    assert "matches its media" in detail


def test_altered_video_fails_verification(manifest):
    """The headline claim: change the picture, the embedded manifest catches it."""
    data = minimal_mp4(media=b"\x11" * 512)
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))

    tampered = embedded.replace(b"\x11" * 32, b"\x22" * 32, 1)
    assert tampered != embedded

    passed, detail = emb.verify_bytes(tampered)
    assert not passed
    assert "altered after the manifest was embedded" in detail


def test_swapped_manifest_fails_verification(manifest):
    """A manifest lifted from a different asset must not verify here."""
    data = minimal_mp4()
    other = minimal_mp4(media=b"\x99" * 512)

    embedded = emb.embed_bytes(data, sign_manifest_for(other, manifest))

    passed, detail = emb.verify_bytes(embedded)
    assert not passed
    assert "hashes to" in detail


def test_missing_manifest_reports_absence_not_success():
    passed, detail = emb.verify_bytes(minimal_mp4())
    assert not passed
    assert "no embedded Notary manifest" in detail


def test_manifest_without_declared_hash_fails_closed(manifest):
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, manifest)  # no asset_sha256

    passed, detail = emb.verify_bytes(embedded)
    assert not passed
    assert "does not declare asset_sha256" in detail


def test_corrupt_manifest_payload_is_reported_not_raised(manifest):
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))

    corrupted = embedded[:-20] + b"\xff" * 20

    passed, _ = emb.verify_bytes(corrupted)
    assert not passed


def test_truncated_file_does_not_crash_the_box_walk(manifest):
    """A partial download must degrade to 'no manifest', never an exception."""
    data = minimal_mp4()
    embedded = emb.embed_bytes(data, sign_manifest_for(data, manifest))

    for cut in (4, 9, 40, len(embedded) - 5):
        passed, _ = emb.verify_bytes(embedded[:cut])
        assert passed is False


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------


def test_file_helpers_round_trip(tmp_path, manifest):
    path = tmp_path / "asset.mp4"
    data = minimal_mp4()
    path.write_bytes(data)

    emb.embed_file(path, sign_manifest_for(data, manifest))

    recovered = emb.extract_file(path)
    assert recovered is not None
    assert recovered.manifest["run_id"] == "run-test-001"

    passed, detail = emb.verify_file(path)
    assert passed, detail
