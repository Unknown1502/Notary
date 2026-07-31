"""Session, queue, and certificate lookup.

Deliberately thin. The Genblaze manifest plus the sealed vault objects are the
system of record — every certificate, verdict, and lineage edge can be
reconstructed by listing `vault/{tenant}/` and reading the JSON that is already
there. This module is a cache and an index over that truth, not a second copy
of it.

That is a real architectural claim, not a shortcut, and it is the reason
Notary needs no database: B2 holds the durable state, and losing this process's
memory loses nothing that cannot be re-read from storage. `rehydrate_from_b2()`
is that path, and it runs at startup.

The one piece of state that is genuinely process-local is the in-flight session
(a run currently streaming). Losing it on restart is acceptable and visible —
the SSE stream terminates and the client sees a failed session rather than a
silent hang.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from .config import get_settings
from .models import (
    BoardDecision,
    Certificate,
    HumanSignoff,
    ReviewSession,
    TakeStatus,
)
from .storage import get_storage, tenant_prefix

log = logging.getLogger(__name__)


class Store:
    """Thread-safe in-memory index with B2 rehydration."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, ReviewSession] = {}
        self._certificates: dict[str, Certificate] = {}
        self._queue: dict[str, str] = {}  # session_id -> reason
        self._rehydrated = False

    # ------------------------------------------------------------- sessions

    def put_session(self, session: ReviewSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> ReviewSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self, *, limit: int = 50) -> list[ReviewSession]:
        with self._lock:
            items = sorted(
                self._sessions.values(), key=lambda s: s.started_at, reverse=True
            )
        return items[:limit]

    # --------------------------------------------------------- human queue

    def enqueue(self, session_id: str, reason: str) -> None:
        with self._lock:
            self._queue[session_id] = reason
        log.info("escalated %s to the human queue: %s", session_id, reason)

    def dequeue(self, session_id: str) -> None:
        with self._lock:
            self._queue.pop(session_id, None)

    def queue_items(self) -> list[dict[str, Any]]:
        with self._lock:
            pairs = list(self._queue.items())
            sessions = {sid: self._sessions.get(sid) for sid, _ in pairs}

        items: list[dict[str, Any]] = []
        for session_id, reason in pairs:
            session = sessions.get(session_id)
            if session is None:
                continue
            take = session.current_take
            items.append(
                {
                    "session_id": session_id,
                    "reason": reason,
                    "campaign_title": session.brief.title,
                    "tenant": session.brief.tenant,
                    "compliance_profile": session.brief.compliance_profile,
                    "waiting_since": session.started_at.isoformat(),
                    "take_number": take.take_number if take else 0,
                    "asset_url": take.asset_url if take else None,
                    "thumbnail_url": take.thumbnail_url if take else None,
                    "verdict": (
                        take.verdict.model_dump(mode="json")
                        if take and take.verdict
                        else None
                    ),
                }
            )
        items.sort(key=lambda i: i["waiting_since"])
        return items

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def apply_signoff(
        self, session_id: str, signoff: HumanSignoff
    ) -> ReviewSession | None:
        """Record a human decision on an escalated take.

        The sign-off is attached to the verdict *before* certification, so the
        reviewer's name and note are inside the document that gets sealed. An
        approval recorded after sealing would be an approval outside the
        immutable record, which defeats the purpose.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            take = session.current_take
            if take is None or take.verdict is None:
                return None

            take.verdict.human_review = signoff
            if signoff.decision == "approved":
                take.verdict.decision = BoardDecision.VERIFIED
                take.status = TakeStatus.REVIEWING
            else:
                take.verdict.decision = BoardDecision.REJECTED
                take.status = TakeStatus.REJECTED
                session.status = TakeStatus.REJECTED
                session.finished_at = datetime.now(UTC)

            self._queue.pop(session_id, None)
            return session

    # -------------------------------------------------------- certificates

    def put_certificate(self, certificate: Certificate) -> None:
        with self._lock:
            self._certificates[certificate.certificate_id] = certificate

    def get_certificate(self, certificate_id: str) -> Certificate | None:
        with self._lock:
            cert = self._certificates.get(certificate_id)
        if cert is not None:
            return cert
        return self._load_certificate_from_b2(certificate_id)

    def list_certificates(
        self, *, tenant: str | None = None, campaign_id: str | None = None
    ) -> list[Certificate]:
        with self._lock:
            items = list(self._certificates.values())
        if tenant:
            items = [c for c in items if c.tenant == tenant]
        if campaign_id:
            items = [c for c in items if c.campaign_id == campaign_id]
        items.sort(key=lambda c: c.certified_at, reverse=True)
        return items

    def _load_certificate_from_b2(self, certificate_id: str) -> Certificate | None:
        settings = get_settings()
        if not settings.reads_real_storage:
            return None
        storage = get_storage()
        if not storage.available:
            return None
        try:
            for obj in storage.list_prefix(
                settings.b2_bucket_vault, "vault/", max_keys=2000
            ):
                if not obj.key.endswith("certificate.json"):
                    continue
                payload = storage.get_json(settings.b2_bucket_vault, obj.key)
                if payload.get("certificate_id") == certificate_id:
                    cert = Certificate.model_validate(payload)
                    self.put_certificate(cert)
                    return cert
        except Exception as exc:  # noqa: BLE001
            log.warning("certificate lookup in B2 failed: %s", exc)
        return None

    def rehydrate_from_b2(self, *, tenants: list[str] | None = None) -> int:
        """Rebuild the certificate index by listing the vault.

        Proves the "B2 is the system of record" claim rather than asserting it:
        the library survives a process restart with no database, because every
        certificate is read back out of the bucket it was sealed into.
        """
        settings = get_settings()
        if not settings.reads_real_storage:
            return 0

        storage = get_storage()
        if not storage.available:
            return 0

        loaded = 0
        prefixes = [tenant_prefix(t) for t in tenants] if tenants else ["vault/"]

        for prefix in prefixes:
            try:
                for obj in storage.list_prefix(
                    settings.b2_bucket_vault, prefix, max_keys=5000
                ):
                    if not obj.key.endswith("certificate.json"):
                        continue
                    try:
                        payload = storage.get_json(settings.b2_bucket_vault, obj.key)
                        cert = Certificate.model_validate(payload)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("skipping unreadable certificate %s: %s", obj.key, exc)
                        continue
                    self.put_certificate(cert)
                    loaded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("vault listing failed for %s: %s", prefix, exc)

        self._rehydrated = True
        log.info("rehydrated %d certificate(s) from B2", loaded)
        return loaded

    def stats(self) -> dict[str, Any]:
        with self._lock:
            certs = list(self._certificates.values())
            sessions = list(self._sessions.values())
            queue_depth = len(self._queue)

        certified = [s for s in sessions if s.status is TakeStatus.CERTIFIED]
        revised = [s for s in sessions if len(s.takes) > 1]
        fallbacks = [s for s in sessions if any(t.used_fallback for t in s.takes)]

        return {
            "certificates": len(certs),
            "signed_certificates": sum(1 for c in certs if c.signature is not None),
            "sessions": len(sessions),
            "certified_sessions": len(certified),
            "revision_rate": round(len(revised) / len(sessions), 3) if sessions else 0.0,
            "fallback_rate": round(len(fallbacks) / len(sessions), 3) if sessions else 0.0,
            "escalation_queue_depth": queue_depth,
            "total_cost_usd": round(sum(s.total_cost_usd for s in sessions), 4),
            "rehydrated": self._rehydrated,
        }


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
