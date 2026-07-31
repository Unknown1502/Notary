import { useState } from "react";
import { api } from "../api";
import type { LibraryAsset, LineageNode, QueueItem } from "../types";
import { Findings } from "./Findings";

/**
 * The lineage thread.
 *
 * Takes are numbered because they genuinely are a sequence — take 02 exists
 * *because* take 01 was rejected, and the order carries the causation. This is
 * the one place numbering earns itself.
 */
export function Lineage({ nodes }: { nodes: LineageNode[] }) {
  if (nodes.length === 0) return null;

  return (
    <ol className="lineage">
      {nodes.map((node) => {
        const state =
          node.decision === "verified"
            ? "certified"
            : node.decision === "escalated"
              ? "escalated"
              : node.decision === "rejected"
                ? "rejected"
                : "pending";

        return (
          <li key={node.run_id} className={`lineage__node lineage__node--${state}`}>
            <span className="lineage__index">{node.take_number}</span>
            <div>
              <p className="lineage__title">
                {node.decision === "verified"
                  ? "Certified"
                  : node.decision === "rejected"
                    ? "Rejected — revised below"
                    : node.decision === "escalated"
                      ? "Escalated to a human"
                      : "Generating"}
                {node.used_fallback && (
                  <span className="eyebrow" style={{ marginLeft: ".5rem" }}>
                    fallback provider
                  </span>
                )}
              </p>
              <p className="lineage__meta">
                {node.run_id}
                {node.parent_run_id && ` ← ${node.parent_run_id}`}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function Library({
  assets,
  onOpen,
}: {
  assets: LibraryAsset[];
  onOpen: (id: string) => void;
}) {
  if (assets.length === 0) {
    return (
      <div className="empty">
        <p className="empty__title">Nothing certified yet</p>
        <p>
          Certified assets appear here with their lineage and provenance
          certificate. Run a review, or replay a recorded one, to populate it.
        </p>
      </div>
    );
  }

  return (
    <div className="grid">
      {assets.map((asset) => (
        <article key={asset.certificate_id} className="panel">
          {asset.thumbnail_url ? (
            <img
              className="frame"
              src={asset.thumbnail_url}
              alt=""
              style={{ borderRadius: "var(--radius) var(--radius) 0 0", border: 0 }}
            />
          ) : (
            <video className="frame" src={asset.asset_url} muted playsInline />
          )}
          <div className="panel__body">
            <p className="eyebrow">{asset.campaign_id}</p>
            <p style={{ margin: ".35rem 0", fontWeight: 500 }}>{asset.prompt}</p>
            <p className="lineage__meta">
              {asset.model} · {asset.takes} take{asset.takes === 1 ? "" : "s"} ·
              {asset.trust_mode === 2 ? " signed" : " unsigned"}
            </p>
            <div className="row" style={{ marginTop: "var(--gap-sm)" }}>
              <button
                className="btn btn--ghost"
                onClick={() => onOpen(asset.certificate_id)}
              >
                Certificate
              </button>
              {asset.is_sealed && <span className="eyebrow">sealed</span>}
            </div>
          </div>
        </article>
      ))}
    </div>
  );
}

/**
 * The human queue — the honest centre of the product.
 *
 * Everything here is a take the Board declined to decide. The copy says so
 * plainly, because a reviewer needs to know that nothing in this list went out.
 */
export function Queue({
  items,
  onResolved,
}: {
  items: QueueItem[];
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("compliance@acme.example");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const decide = async (sessionId: string, decision: "approved" | "rejected") => {
    setBusy(sessionId);
    setError(null);
    try {
      await api.signoff(sessionId, { reviewer, decision, note });
      setNote("");
      onResolved();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Sign-off failed.");
    } finally {
      setBusy(null);
    }
  };

  if (items.length === 0) {
    return (
      <div className="empty">
        <p className="empty__title">Queue is clear</p>
        <p>
          Nothing is waiting on a human. Takes land here when the Board cannot
          clear them with confidence — an ambiguous finding, a low-confidence
          failure, or an exhausted revision budget.
        </p>
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="notice notice--warn">
        {items.length} take{items.length === 1 ? "" : "s"} waiting on a human.
        None of these have been published.
      </div>

      <label className="field">
        <span className="field__label">Reviewing as</span>
        <input
          className="input"
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
        />
        <span className="field__hint">
          This name is written into the verdict before it is sealed, so the
          sign-off is inside the immutable record.
        </span>
      </label>

      {error && (
        <div className="notice notice--warn" role="alert">
          {error}
        </div>
      )}

      {items.map((item) => (
        <article key={item.session_id} className="panel">
          <div className="panel__head">
            <h3 className="eyebrow">{item.campaign_title}</h3>
            <span className="eyebrow">{item.compliance_profile}</span>
          </div>
          <div className="panel__body">
            <div className="docket">
              <div>
                {item.asset_url && (
                  <video className="frame" src={item.asset_url} controls playsInline />
                )}
                <p className="lineage__meta" style={{ marginTop: ".5rem" }}>
                  take {item.take_number} · waiting since{" "}
                  {new Date(item.waiting_since).toLocaleTimeString()}
                </p>
              </div>
              <div>
                <p style={{ marginTop: 0 }}>
                  <strong>Why it stopped here:</strong> {item.reason}
                </p>
                {item.verdict && <Findings findings={item.verdict.criteria} />}
              </div>
            </div>

            <label className="field" style={{ marginTop: "var(--gap)" }}>
              <span className="field__label">Note</span>
              <textarea
                className="textarea"
                value={note}
                placeholder="What did you decide, and why?"
                onChange={(e) => setNote(e.target.value)}
              />
            </label>

            <div className="row">
              <button
                className="btn btn--approve"
                disabled={busy === item.session_id}
                onClick={() => decide(item.session_id, "approved")}
              >
                {busy === item.session_id ? "Sealing…" : "Approve and seal"}
              </button>
              <button
                className="btn btn--reject"
                disabled={busy === item.session_id}
                onClick={() => decide(item.session_id, "rejected")}
              >
                Reject
              </button>
            </div>
          </div>
        </article>
      ))}
    </div>
  );
}
