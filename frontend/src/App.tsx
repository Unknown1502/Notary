import { useCallback, useEffect, useMemo, useState } from "react";
import { api, useReviewStream } from "./api";
import { CertificateView } from "./components/Certificate";
import { Evidence } from "./components/Evidence";
import { Disposition, Findings } from "./components/Findings";
import { Library, Lineage, Queue, Recordings } from "./components/Screens";
import { Shell, type Tab } from "./components/Shell";
import {
  Button,
  Notice,
  Panel,
  Pill,
  ToastHost,
  useTheme,
  useToast,
} from "./components/ui";
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

/**
 * Fold the event stream into the state the review screen renders.
 *
 * Replaying the whole reduction on every event keeps this correct across
 * reconnects, where the server re-sends the backlog. A new take clears prior
 * findings, because a revision re-reviews everything.
 */
function reduceStream(events: StreamEvent[]) {
  const findings = new Map<string, Finding>();
  const lineage: LineageNode[] = [];
  let decision: string | null = null;
  let detail = "";
  let takeNumber = 0;
  let assetUrl: string | null = null;
  let frameUrl: string | null = null;
  let certificateId: string | null = null;
  let fallback: { from: string; to: string; code: string } | null = null;
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
          confidence: typeof event.confidence === "number" ? event.confidence : null,
          measurement: (event.measurement as Finding["measurement"]) ?? null,
          evidence_frame: (event.evidence_frame as string | null) ?? null,
        });
        break;
      }
      case "board.verdict": {
        decision = String(event.decision);
        detail = String(event.summary ?? "");
        if (event.asset_url) assetUrl = String(event.asset_url);
        // Replayed runs ship the reviewed frame rather than a video: no
        // provider was called, so there is no clip — but the frame the checks
        // actually measured does exist, and showing it is what makes the
        // numbers on screen accountable.
        if (event.frame_url) frameUrl = String(event.frame_url);
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
    frameUrl,
    certificateId,
    fallback,
    replayed,
    outcome,
  };
}

function Console() {
  const toast = useToast();
  const { theme, toggle } = useTheme();

  const [tab, setTab] = useState<Tab>("review");
  const [health, setHealth] = useState<Health | null>(null);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [certificate, setCertificate] = useState<Certificate | null>(null);

  const { events, connected } = useReviewStream(sessionId);
  const state = useMemo(() => reduceStream(events), [events]);

  const refresh = useCallback(async () => {
    try {
      const [q, l] = await Promise.all([api.queue(), api.library()]);
      setQueue(q.items);
      setAssets(l.assets);
    } catch {
      /* transient; the health strip already reports connectivity */
    }
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => toast("Backend unreachable.", "error"));
    api
      .recordings()
      .then((r) => setRecordings(r.recordings))
      .catch(() => undefined);
    refresh();
  }, [refresh, toast]);

  useEffect(() => {
    if (state.outcome) refresh();
  }, [state.outcome, refresh]);

  const openCertificate = useCallback(
    async (id: string) => {
      try {
        const result = await api.certificate(id);
        setCertificate(result.certificate);
        setTab("library");
      } catch (exc) {
        toast(exc instanceof Error ? exc.message : "Could not load certificate.", "error");
      }
    },
    [toast],
  );

  const startReplay = async (id: string) => {
    setCertificate(null);
    try {
      const result = await api.replay(id);
      setSessionId(result.session_id);
    } catch (exc) {
      toast(exc instanceof Error ? exc.message : "Could not start replay.", "error");
    }
  };

  const crumb = {
    review: sessionId ? "Review · live" : "Review",
    queue: "Human queue",
    library: certificate ? "Certified · certificate" : "Certified",
    evidence: "Evidence",
  }[tab];

  return (
    <Shell
      tab={tab}
      onTab={(t) => {
        setTab(t);
        if (t !== "library") setCertificate(null);
      }}
      health={health}
      queueDepth={queue.length}
      theme={theme}
      onToggleTheme={toggle}
      crumb={<span>{crumb}</span>}
    >
      <div>
          {/* ------------------------------------------------------ review */}
          {tab === "review" && !sessionId && (
            <>
              <div className="page-head">
                <div>
                  <h1 className="page-title">Review</h1>
                  <p className="page-sub">
                    Every generated take is screened before it can ship. Measured
                    criteria are computed from the file; perceptual ones are a
                    model's judgement, and anything it cannot clear goes to a
                    human rather than out the door.
                  </p>
                </div>
              </div>

              <div className="stack">
                <Recordings recordings={recordings} onPlay={startReplay} />
              </div>
            </>
          )}

          {tab === "review" && sessionId && (
            <>
              <div className="page-head">
                <div>
                  <h1 className="page-title">
                    Take {String(state.takeNumber || 1).padStart(2, "0")}
                  </h1>
                  <p className="page-sub">
                    {state.detail || "The Board is reviewing this take."}
                  </p>
                </div>
                <div className="row">
                  {connected && (
                    <Pill>
                      <span className="dot dot--live" />
                      streaming
                    </Pill>
                  )}
                  {state.replayed && <Pill>replay</Pill>}
                  <Button variant="outline" onClick={() => setSessionId(null)}>
                    All reviews
                  </Button>
                </div>
              </div>

              <div className="stack">
                {state.fallback && (
                  <Notice tone="accent">
                    Provider fault <span className="mono">{state.fallback.code}</span> on{" "}
                    <span className="mono">{state.fallback.from}</span>. Failed over to{" "}
                    <span className="mono">{state.fallback.to}</span> on a parent-linked
                    run — a provider fault earns a different provider, never a
                    rewritten prompt.
                  </Notice>
                )}

                <div className="split">
                  <div className="stack">
                    <div className="media">
                      {state.assetUrl ? (
                        <video src={state.assetUrl} controls playsInline />
                      ) : state.frameUrl ? (
                        <img src={state.frameUrl} alt="Reviewed keyframe" />
                      ) : (
                        <div className="media media--empty">awaiting first take</div>
                      )}
                    </div>
                    {!state.assetUrl && state.frameUrl && (
                      <p className="hint">
                        Reviewed keyframe — the exact image the measured
                        criteria were computed from. No clip exists for a
                        replayed run because no provider was called.
                      </p>
                    )}
                    <Panel title="Lineage" flush>
                      <Lineage nodes={state.lineage} />
                    </Panel>
                  </div>

                  <Panel
                    title="Findings"
                    actions={<span className="label">{state.findings.length} criteria</span>}
                    flush
                  >
                    <Findings findings={state.findings} />
                    {state.decision && (
                      <Disposition decision={state.decision} detail={state.detail} />
                    )}
                    {state.certificateId && (
                      <div style={{ padding: "var(--s4)" }}>
                        <Button
                          variant="accent"
                          onClick={() => openCertificate(state.certificateId!)}
                        >
                          Open certificate
                        </Button>
                      </div>
                    )}
                  </Panel>
                </div>
              </div>
            </>
          )}

          {/* ------------------------------------------------------- queue */}
          {tab === "queue" && (
            <>
              <div className="page-head">
                <div>
                  <h1 className="page-title">Human queue</h1>
                  <p className="page-sub">
                    Takes the Board declined to decide. Nothing here has been
                    published, and a sign-off recorded here is sealed inside the
                    verdict rather than beside it.
                  </p>
                </div>
              </div>
              <Queue items={queue} onResolved={refresh} />
            </>
          )}

          {/* ----------------------------------------------------- library */}
          {tab === "library" && (
            certificate ? (
              <CertificateView
                certificate={certificate}
                onBack={() => setCertificate(null)}
              />
            ) : (
              <>
                <div className="page-head">
                  <div>
                    <h1 className="page-title">Certified</h1>
                    <p className="page-sub">
                      Assets sealed into the Object-Locked vault. This index is
                      rebuilt by listing the bucket — B2 is the system of record,
                      so there is no database to disagree with it.
                    </p>
                  </div>
                </div>
                <Library assets={assets} onOpen={openCertificate} />
              </>
            )
          )}

          {/* ---------------------------------------------------- evidence */}
          {tab === "evidence" && (
            <>
              <div className="page-head">
                <div>
                  <h1 className="page-title">Evidence</h1>
                  <p className="page-sub">
                    What is measured, what is proven, and — given the same
                    weight — what is not.
                  </p>
                </div>
              </div>
              <Evidence />
            </>
          )}
      </div>
    </Shell>
  );
}

export default function App() {
  return (
    <ToastHost>
      <Console />
    </ToastHost>
  );
}
