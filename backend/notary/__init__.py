"""Notary — an AI Creative Review Board for generative media.

Every generated take is screened against brand and compliance criteria before
it can ship. Objectively measurable failures are caught by computation, not by
a model. Perceptual judgement runs through a vision model wrapped as a real
pipeline step, so its verdict lands in the provenance manifest. Clear failures
are revised with the reasons they failed. Anything ambiguous goes to a human.

What clears is sealed into Backblaze B2 under Object Lock and signed with
Ed25519 — Genblaze Trust Mode 2, which the SDK defines and reserves a manifest
field for but has not yet shipped.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
