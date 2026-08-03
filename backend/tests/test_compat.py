"""Guards on the SDK bridge.

`genblaze_compat` re-exports names it never uses itself, which makes every one
of them look unused to a linter. Ruff's F401 autofix acted on that and deleted
the import list -- leaving `GENBLAZE_AVAILABLE` True while `Pipeline`,
`AgentLoop`, `Modality` and the storage classes stayed bound to None.

The failure mode is what makes it worth a test. Nothing raised at import.
Replay mode, the whole suite, ruff and the type checker all stayed green,
because none of them touch those names. Live mode died with "genblaze AgentLoop
is unavailable" on a machine where the SDK was installed and working -- an
error that points at the SDK rather than at the wrapper that dropped it.
"""

from __future__ import annotations

import notary.genblaze_compat as gc

# Names the rest of the codebase imports from the bridge. If the SDK reports
# itself available, every one of these must be a real object.
SDK_EXPORTS = [
    "AgentContext",
    "AgentLoop",
    "Asset",
    "CallableEvaluator",
    "EvaluationResult",
    "Manifest",
    "Modality",
    "ModerationHook",
    "ModerationResult",
    "Mp4Handler",
    "Pipeline",
    "ProviderError",
    "Step",
    "SyncProvider",
]

STORAGE_EXPORTS = ["ObjectStorageSink", "S3StorageBackend"]


def test_declared_exports_all_exist():
    """__all__ is what stops ruff stripping the imports, so it must be honest."""
    missing = [name for name in gc.__all__ if not hasattr(gc, name)]
    assert not missing, f"__all__ names nothing: {missing}"


def test_sdk_names_are_bound_when_the_sdk_is_available():
    if not gc.GENBLAZE_AVAILABLE:
        return  # replay-only environment; nothing to assert

    unbound = [name for name in SDK_EXPORTS if getattr(gc, name, None) is None]
    assert not unbound, (
        f"GENBLAZE_AVAILABLE is True but these are None: {unbound}. "
        "The import list was probably stripped as unused -- check __all__."
    )


def test_storage_names_are_bound_when_s3_is_available():
    """S3_AVAILABLE must never be True while the classes it gates are None.

    That exact combination produced a sink preflight failure reading
    \"'NoneType' object has no attribute 'for_backblaze'\", which reads like a
    credentials problem and is not one.
    """
    if not gc.S3_AVAILABLE:
        return

    unbound = [name for name in STORAGE_EXPORTS if getattr(gc, name, None) is None]
    assert not unbound, f"S3_AVAILABLE is True but these are None: {unbound}"


def test_availability_flags_agree_with_reality():
    """The flag is what every caller branches on, so it must not overclaim."""
    if gc.GENBLAZE_AVAILABLE:
        assert gc.GENBLAZE_IMPORT_ERROR is None
        assert gc.Pipeline is not None
    else:
        assert gc.GENBLAZE_IMPORT_ERROR


def test_every_sdk_export_is_declared():
    """A name imported but left out of __all__ is one ruff may delete next."""
    undeclared = [n for n in SDK_EXPORTS + STORAGE_EXPORTS if n not in gc.__all__]
    assert not undeclared, f"not protected by __all__: {undeclared}"
