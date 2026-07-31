import { useCallback, useEffect, useMemo, useState } from "react";
import { api, useReviewStream } from "./api";
import { CertificatePanel } from "./components/Certificate";
import { Disposition, Findings } from "./components/Findings";
import { Library, Lineage, Queue } from "./components/Panels";
import { Trust } from "./components/Trust";
import type {
  Certificate,
  Finding,
  Health,
  LibraryAsset,
  LineageNode,
  QueueItem,
  Recording,
  StreamEvent,
} from "./types";

type Tab = "review" | "queue" | "library" | "trust";

/**
 * Fold the event stream into the state the review screen renders.
 *
 * Events arrive incrementally and a criterion can be superseded (a revision
 * re-reviews everything), so findings are keyed by criterion and the latest
 * wins. Replaying the whole reduction on every event keeps this correct on
 * reconnect, where the server re-sends the backlog.
 */
function reduceStream(events: StreamEvent[]) {
  const findings = new Map<string, Finding>();
  const lineage: LineageNode[] = [];
  let decision: string | null = null;
  let detail = "";
  let takeNumber = 0;
  let assetUrl: string | null = null;
  let certificateId: string | null = null;
  let fallback: { from: string; to: string; code: string } | null = null;
  let sealed: Record<string, unknown> | null = null;
  let replayed = false;
  let outcome: string | null = null;

  for (const event of events) {
    if (event.replayed) replayed = true;

    switch (event.type) {
      case "take.started":
      case "revision.started": {
        const n = Number(event.take_number ?? takeNumber + 1);
        if (n > takeNumber) {
          takeNumber = n;
          // A new take invalidates the previous take's findings.
          findings.clear();
          decision = null;
        }
        break;
      }
      case "board.criterion": {
        const criterion = String(event.criterion);
        findings.set(criterion, {
          criterion,
          outcome: event.outcome as Finding["outcome"],
          kind: event.kind as Finding["kind"],
          severity: event.severity as Finding["severity"],
          rationale: String(event.rationale ?? ""),
          confidence:
            typeof event.confidence === "number" ? event.confidence : null,
          measurement:
            (event.measurement as Finding["measurement"]) ?? null,
          evidence_frame: (event.evidence_frame as string | null) ?? null,
        });
        break;
      }
      case "board.verdict": {
        decision = String(event.decision);
        detail = String(event.summary ?? "");
        if (event.asset_url) assetUrl = String(event.asset_url);
        lineage.push({
          run_id: String(event.run_id ?? `take-${event.take_number}`),
          parent_run_id: null,
          take_number: Number(event.take_number ?? takeNumber),
          status: decision,
          decision: decision as LineageNode["decision"],
          used_fallback: fallback !== null,
          created_at: new Date(event.timestamp * 1000).toISOString(),
        });
        break;
      }
      case "fallback.fired":
        fallback = {
          from: String(event.from_model),
          to: String(event.to_model),
          code: String(event.error_code),
        };
        break;
      case "certification.sealed":
        sealed = event as Record<string, unknown>;
        certificateId = String(event.certificate_id);
        break;
      case "escalated":
        decision = "escalated";
        detail = String(event.reason ?? "");
        break;
      case "session.completed":
        outcome = String(event.outcome ?? "");
        if (event.summary) detail = String(event.summary);
        break;
    }
  }

  return {
    findings: [...findings.values()],
    lineage,
    decision,
    detail,
    takeNumber,
    assetUrl,
    certificateId,
    fallback,
    sealed,
    replayed,
    outcome,
  };
}

export default function App() {
  const [tab, setTab] = useState<Tab>("review");
  const [health, setHealth] = useState<Health | null>(null);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [certificate, setCertificate] = useState<Certificate | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { events, connected } = useReviewStream(sessionId);
  const state = useMemo(() => reduceStream(events), [events]);

  const refresh = useCallback(async () => {
    try {
      const [q, l] = await Promise.all([api.queue(), api.library()]);
      setQueue(q.items);
      setAssets(l.assets);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Could not reach the API.");
    }
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setError("Backend unreachable."));
    api.recordings().then((r) => setRecordings(r.recordings)).catch(() => undefined);
    refresh();
  }, [refresh]);

  // A completed run changes the queue and the library, so re-read them.
  useEffect(() => {
    if (state.outcome) refresh();
  }, [state.outcome, refresh]);

  const openCertificate = useCallback(async (id: string) => {
    try {
      const result = await api.certificate(id);
      setCertificate(result.certificate);
      setTab("library");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Could not load certificate.");
    }
  }, []);

  const startReplay = async (id: string) => {
    setCertificate(null);
    try {
      const result = await api.replay(id);
      setSessionId(result.session_id);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Could not start replay.");
    }
  };

  const mode = health?.runtime.mode ?? "…";
  const isLive = mode === "live";

  return (
    <div className="shell">
      <header className="masthead">
        <h1 className="wordmark">Notary</h1>
        <p className="masthead__tagline">
          Every clip goes before the Board. Every approval is provable.
        </p>
        <span
          className={`mode-pill mode-pill--${
            state.replayed ? "replay" : isLive ? "live" : "replay"
          }`}
        >
          <span className="mode-pill__dot" />
          {state.replayed ? "replay" : mode}
        </span>
        {health && (
          <span className="mode-pill">
            <span className="mode-pill__dot" />
            trust mode {health.runtime.trust_mode}
          </span>
        )}
      </header>

      {error && (
        <div className="notice notice--warn" role="alert" style={{ marginBottom: "1rem" }}>
          {error}
        </div>
      )}

      <nav className="nav" role="tablist">
        {(
          [
            ["review", "Review"],
            ["queue", "Human queue"],
            ["library", "Certified library"],
            ["trust", "Evidence"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className="nav__tab"
            onClick={() => setTab(key)}
          >
            {label}
            {key === "queue" && queue.length > 0 && (
              <span className="nav__count">{queue.length}</span>
            )}
          </button>
        ))}
      </nav>

      {tab === "review" && (
        <div className="stack">
          {!sessionId && (
            <>
              <div className="notice">
                {isLive
                  ? "Live generation is enabled. A full run takes several minutes; the recorded runs below reach the same screens immediately."
                  : "This deployment is in replay mode. Each run below is the captured event stream of a real review, played back through the same API and the same interface."}
              </div>

              {recordings.length === 0 ? (
                <div className="empty">
                  <p className="empty__title">No recorded runs available</p>
                  <p>Run scripts/seed_demo.py to generate demo recordings.</p>
                </div>
              ) : (
                <div className="grid">
                  {recordings.map((rec) => (
                    <article key={rec.session_id} className="panel">
                      <div className="panel__body">
                        <p className="eyebrow">
                          {rec.certified
                            ? "certified"
                            : rec.escalated
                              ? "escalated"
                              : "rejected"}
                        </p>
                        <h3 style={{ margin: ".35rem 0", fontSize: "var(--step-1)" }}>
                          {rec.title || rec.session_id}
                        </h3>
                        <p className="lineage__meta">
                          {rec.verdicts.length} take
                          {rec.verdicts.length === 1 ? "" : "s"} ·{" "}
                          {rec.event_count} events
                          {rec.took_fallback && " · provider fallback"}
                        </p>
                        <button
                          className="btn"
                          style={{ marginTop: "var(--gap-sm)" }}
                          onClick={() => startReplay(rec.session_id)}
                        >
                          Watch the review
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </>
          )}

          {sessionId && (
            <>
              <div className="row">
                <button
                  className="btn btn--ghost"
                  onClick={() => {
                    setSessionId(null);
                    setCertificate(null);
                  }}
                >
                  ← All runs
                </button>
                {connected && <span className="eyebrow">streaming</span>}
                {state.takeNumber > 0 && (
                  <span className="eyebrow">
                    take {String(state.takeNumber).padStart(2, "0")}
                  </span>
                )}
              </div>

              {state.fallback && (
                <div className="notice notice--warn">
                  Provider fault <span className="mono">{state.fallback.code}</span> on{" "}
                  <span className="mono">{state.fallback.from}</span>. Failed over to{" "}
                  <span className="mono">{state.fallback.to}</span> on a
                  parent-linked run — a provider fault earns a different provider,
                  never a rewritten prompt.
                </div>
              )}

              <div className="docket">
                <section className="stack">
                  {state.assetUrl ? (
                    <video className="frame" src={state.assetUrl} controls playsInline />
                  ) : (
                    <div className="frame" aria-hidden="true" />
                  )}
                  <section className="panel">
                    <div className="panel__head">
                      <h2 className="eyebrow">Lineage</h2>
                    </div>
                    <div className="panel__body">
                      <Lineage nodes={state.lineage} />
                    </div>
                  </section>
                </section>

                <section className="panel">
                  <div className="panel__head">
                    <h2 className="eyebrow">Findings</h2>
                    <span className="eyebrow">{state.findings.length} criteria</span>
                  </div>
                  <div className="panel__body">
                    <Findings findings={state.findings} />
                    {state.decision && (
                      <Disposition decision={state.decision} detail={state.detail} />
                    )}
                    {state.certificateId && (
                      <button
                        className="btn"
                        style={{ marginTop: "var(--gap)" }}
                        onClick={() => openCertificate(state.certificateId!)}
                      >
                        Open certificate
                      </button>
                    )}
                  </div>
                </section>
              </div>
            </>
          )}
        </div>
      )}

      {tab === "queue" && <Queue items={queue} onResolved={refresh} />}

      {tab === "trust" && <Trust />}

      {tab === "library" &&
        (certificate ? (
          <div className="stack">
            <button className="btn btn--ghost" onClick={() => setCertificate(null)}>
              ← Library
            </button>
            <CertificatePanel certificate={certificate} />
          </div>
        ) : (
          <Library assets={assets} onOpen={openCertificate} />
        ))}
    </div>
  );
}
