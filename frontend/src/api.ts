import { useCallback, useEffect, useRef, useState } from "react";
import type { StreamEvent } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* response had no JSON body; the status line is the best we have */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<import("./types").Health>("/api/health"),
  profiles: () => request<{ profiles: unknown[] }>("/api/profiles"),
  recordings: () =>
    request<{ recordings: import("./types").Recording[] }>("/api/demo/recordings"),
  replay: (id: string, speed = 3) =>
    request<{ session_id: string; stream_url: string }>(
      `/api/demo/replay/${id}`,
      { method: "POST", body: JSON.stringify({ speed }) },
    ),
  evaluation: () => request<import("./types").EvaluationReport>("/api/evaluation"),
  queue: () =>
    request<{ items: import("./types").QueueItem[]; depth: number }>("/api/queue"),
  signoff: (sessionId: string, body: unknown) =>
    request<{ certificate_id: string | null; message: string }>(
      `/api/queue/${sessionId}/signoff`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  library: () =>
    request<{ assets: import("./types").LibraryAsset[] }>("/api/library"),
  certificate: (id: string) =>
    request<{ certificate: import("./types").Certificate; trust_mode_label: string }>(
      `/api/certificates/${id}`,
    ),
  verify: (id: string) =>
    request<{
      report: import("./types").VerificationReport;
      passed: boolean;
      summary: string;
    }>(`/api/certificates/${id}/verify`, { method: "POST" }),
  /**
   * Media lives behind the app, never at the URL inside the certificate.
   * Those URLs are sealed under Object Lock: `asset_url` is whatever origin
   * issued the certificate (for a locally-issued one, the *viewer's* own
   * localhost) and `thumbnail_url` is a bare URL into a private bucket. Both
   * are the historical record, not something a browser can fetch. These
   * endpoints mint a fresh presigned URL per request instead.
   */
  assetUrl: (id: string) => `/api/certificates/${id}/asset`,
  posterUrl: (id: string) => `/api/certificates/${id}/thumbnail`,
  submit: (body: unknown) =>
    request<{ session_id: string; stream_url: string }>("/api/reviews", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

/**
 * Subscribe to a review's event stream.
 *
 * Live runs and replays use the identical endpoint and contract, so this hook
 * has no idea which it is watching — the only difference is the `replayed`
 * flag the server sets on each event, which the UI surfaces rather than hides.
 */
export function useReviewStream(sessionId: string | null) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!sessionId) return;

    setEvents([]);
    setError(null);

    const source = new EventSource(`${BASE}/api/reviews/${sessionId}/stream`);
    sourceRef.current = source;

    source.onopen = () => setConnected(true);

    const handle = (event: MessageEvent) => {
      try {
        const parsed = JSON.parse(event.data) as StreamEvent;
        if (parsed.type === "heartbeat") return;
        setEvents((prior) => [...prior, parsed]);
        if (parsed.type === "session.completed" || parsed.type === "session.failed") {
          source.close();
          setConnected(false);
        }
      } catch {
        /* a malformed frame is dropped rather than tearing down the stream */
      }
    };

    // Named SSE events don't reach onmessage, so every type is bound explicitly.
    const types = [
      "session.started", "take.started", "step.started", "step.progress",
      "step.completed", "step.failed", "fallback.fired", "board.convened",
      "board.criterion", "board.verdict", "revision.started", "escalated",
      "certification.started", "certification.sealed", "session.completed",
      "session.failed",
    ];
    types.forEach((type) => source.addEventListener(type, handle as EventListener));
    source.onmessage = handle;

    source.onerror = () => {
      // EventSource reconnects on its own; only report a hard close.
      if (source.readyState === EventSource.CLOSED) {
        setConnected(false);
        setError("Stream disconnected.");
      }
    };

    return () => {
      source.close();
      sourceRef.current = null;
      setConnected(false);
    };
  }, [sessionId]);

  const reset = useCallback(() => setEvents([]), []);
  return { events, connected, error, reset };
}
