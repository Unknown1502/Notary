import { useState } from "react";
import { motion } from "framer-motion";
import { api } from "../api";
import type { LibraryAsset, LineageNode, QueueItem, Recording } from "../types";
import { Findings } from "./Findings";
import { Button, Empty, IconShield, Notice, Panel, Pill, useToast } from "./ui";

/* ==========================================================================
   Lineage

   Takes are numbered because they are genuinely a sequence: take 02 exists
   *because* take 01 was rejected, and the order carries the causation. This is
   the one place in the interface where numbering earns itself.
   ========================================================================== */

export function Lineage({ nodes }: { nodes: LineageNode[] }) {
  if (nodes.length === 0) {
    return (
      <p className="muted" style={{ padding: "var(--s4)", fontSize: "var(--t-micro)" }}>
        No takes yet.
      </p>
    );
  }

  return (
    <ol className="lineage">
      {nodes.map((node, i) => {
        const state =
          node.decision === "verified"
            ? "verified"
            : node.decision === "escalated"
              ? "escalated"
              : node.decision === "rejected"
                ? "rejected"
                : "pending";

        const title =
          node.decision === "verified"
            ? "Certified"
            : node.decision === "rejected"
              ? "Rejected — revised below"
              : node.decision === "escalated"
                ? "Escalated to a human"
                : "Generating";

        return (
          <motion.li
            key={node.run_id}
            className="lineage__row"
            data-state={state}
            initial={{ opacity: 0, x: -4 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ type: "spring", stiffness: 400, damping: 34, delay: i * 0.04 }}
          >
            <span className="lineage__idx">{node.take_number}</span>
            <div>
              <p className="lineage__title">
                {title}
                {node.used_fallback && (
                  <span className="finding__advisory">fallback provider</span>
                )}
              </p>
              <p className="lineage__meta">{node.run_id}</p>
            </div>
          </motion.li>
        );
      })}
    </ol>
  );
}

/* ==========================================================================
   Recordings index — the entry point in replay mode
   ========================================================================== */

export function Recordings({
  recordings,
  onPlay,
}: {
  recordings: Recording[];
  onPlay: (id: string) => void;
}) {
  if (recordings.length === 0) {
    return (
      <Empty title="No recorded runs">
        Each recording is the captured event stream of a real review. Generate
        some with <code className="mono">scripts/seed_demo.py</code>.
      </Empty>
    );
  }

  return (
    <Panel flush title="Recorded reviews">
      <table className="table">
        <thead>
          <tr>
            <th>Campaign</th>
            <th>Outcome</th>
            <th className="table__num">Takes</th>
            <th className="table__num">Events</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {recordings.map((r) => (
            <tr key={r.session_id}>
              <td>
                <div style={{ fontWeight: 450 }}>{r.title || r.session_id}</div>
                <div className="mono muted" style={{ fontSize: "var(--t-micro)" }}>
                  {r.session_id}
                </div>
              </td>
              <td>
                <div className="row" style={{ gap: "var(--s1)" }}>
                  <Pill accent={r.certified}>
                    {r.certified ? "certified" : r.escalated ? "escalated" : "rejected"}
                  </Pill>
                  {r.took_fallback && <Pill>fallback</Pill>}
                </div>
              </td>
              <td className="table__num">{r.verdicts.length}</td>
              <td className="table__num">{r.event_count}</td>
              <td style={{ textAlign: "right" }}>
                <Button variant="outline" size="sm" onClick={() => onPlay(r.session_id)}>
                  Watch review
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

/* ==========================================================================
   Certified library
   ========================================================================== */

export function Library({
  assets,
  onOpen,
}: {
  assets: LibraryAsset[];
  onOpen: (id: string) => void;
}) {
  if (assets.length === 0) {
    return (
      <Empty title="Nothing certified yet">
        Certified assets appear here with their lineage and provenance
        certificate. In hybrid mode this list is rebuilt by listing the B2 vault
        — there is no database, so an empty list means an empty bucket.
      </Empty>
    );
  }

  return (
    <div className="grid">
      {assets.map((asset, i) => (
        <motion.button
          key={asset.certificate_id}
          className="card"
          onClick={() => onOpen(asset.certificate_id)}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ type: "spring", stiffness: 380, damping: 32, delay: i * 0.03 }}
        >
          <div className="card__media">
            {asset.thumbnail_url ? (
              <img src={asset.thumbnail_url} alt="" loading="lazy" />
            ) : (
              <video src={asset.asset_url} muted playsInline preload="metadata" />
            )}
          </div>
          <div className="card__body">
            <div className="row" style={{ gap: "var(--s1)" }}>
              <span className="label">{asset.campaign_id}</span>
              {asset.is_sealed && (
                <Pill accent>
                  <IconShield /> sealed
                </Pill>
              )}
            </div>
            <p style={{ fontWeight: 450, lineHeight: "var(--lh-snug)" }}>
              {asset.prompt}
            </p>
            <p className="mono muted" style={{ fontSize: "var(--t-micro)", marginTop: "auto" }}>
              {asset.model} · {asset.takes} take{asset.takes === 1 ? "" : "s"} ·{" "}
              {asset.trust_mode === 2 ? "signed" : "unsigned"}
            </p>
          </div>
        </motion.button>
      ))}
    </div>
  );
}

/* ==========================================================================
   Human queue — the honest centre of the product

   Everything here is a take the Board declined to decide. The copy says so
   plainly, because a reviewer needs to know nothing in this list went out.
   ========================================================================== */

export function Queue({
  items,
  onResolved,
}: {
  items: QueueItem[];
  onResolved: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("compliance@acme.example");
  const [notes, setNotes] = useState<Record<string, string>>({});

  const decide = async (sessionId: string, decision: "approved" | "rejected") => {
    setBusy(sessionId);
    try {
      const result = await api.signoff(sessionId, {
        reviewer,
        decision,
        note: notes[sessionId] ?? "",
      });
      toast(
        result.message,
        decision === "approved" ? "success" : "neutral",
      );
      onResolved();
    } catch (exc) {
      toast(exc instanceof Error ? exc.message : "Sign-off failed.", "error");
    } finally {
      setBusy(null);
    }
  };

  if (items.length === 0) {
    return (
      <Empty title="Queue is clear">
        Nothing is waiting on a human. Takes land here when the Board cannot
        clear them with confidence — an ambiguous finding, a low-confidence
        failure, or an exhausted revision budget.
      </Empty>
    );
  }

  return (
    <div className="stack">
      <Notice tone="accent">
        {items.length} take{items.length === 1 ? "" : "s"} waiting on a human.
        None of these have been published.
      </Notice>

      <Panel title="Reviewing as">
        <div className="field-group">
          <input
            className="input"
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            aria-label="Reviewer identity"
          />
          <p className="hint">
            Written into the verdict before it is sealed, so the sign-off is
            inside the immutable record rather than beside it.
          </p>
        </div>
      </Panel>

      {items.map((item) => (
        <Panel
          key={item.session_id}
          title={item.campaign_title}
          actions={<Pill>{item.compliance_profile}</Pill>}
          flush
        >
          <div className="split" style={{ padding: "var(--s4)" }}>
            <div className="stack--tight">
              <div className="media">
                {item.asset_url ? (
                  <video src={item.asset_url} controls playsInline preload="metadata" />
                ) : (
                  <div className="media media--empty">no preview</div>
                )}
              </div>
              <p className="mono muted" style={{ fontSize: "var(--t-micro)" }}>
                take {String(item.take_number).padStart(2, "0")} · waiting since{" "}
                {new Date(item.waiting_since).toLocaleTimeString()}
              </p>
            </div>

            <div className="stack--tight">
              <div>
                <p className="label">Why it stopped here</p>
                <p className="dim" style={{ lineHeight: "var(--lh-body)", marginTop: 4 }}>
                  {item.reason}
                </p>
              </div>
              {item.verdict && <Findings findings={item.verdict.criteria} />}
            </div>
          </div>

          <div style={{ padding: "0 var(--s4) var(--s4)" }} className="stack--tight">
            <textarea
              className="textarea"
              placeholder="What did you decide, and why?"
              value={notes[item.session_id] ?? ""}
              onChange={(e) =>
                setNotes((n) => ({ ...n, [item.session_id]: e.target.value }))
              }
              aria-label="Sign-off note"
            />
            <div className="row">
              <Button
                variant="accent"
                disabled={busy === item.session_id}
                onClick={() => decide(item.session_id, "approved")}
              >
                {busy === item.session_id ? "Sealing…" : "Approve and seal"}
              </Button>
              <Button
                variant="danger"
                disabled={busy === item.session_id}
                onClick={() => decide(item.session_id, "rejected")}
              >
                Reject
              </Button>
            </div>
          </div>
        </Panel>
      ))}
    </div>
  );
}
