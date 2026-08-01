import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { api } from "../api";
import type { EvaluationReport } from "../types";
import { Empty, Notice, Panel, Skeleton, Stat } from "./ui";

const pct = (v: number | null) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;

/* ==========================================================================
   Evidence

   Most review products assert that their classifier works. This screen shows
   the measurement — and gives the *unmeasured* half equal visual weight,
   because a reviewer deciding whether to trust this system needs the gap more
   than they need the wins.
   ========================================================================== */

export function Evidence() {
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evaluation()
      .then(setReport)
      .catch((exc) =>
        setError(exc instanceof Error ? exc.message : "Could not load evaluation."),
      );
  }, []);

  if (error) return <Notice tone="fail">{error}</Notice>;

  if (!report) {
    return (
      <div className="stack">
        <Skeleton height={92} />
        <Skeleton height={220} />
      </div>
    );
  }

  if (!report.available) {
    return (
      <Empty title="No evaluation report">
        {report.reason} Run <code className="mono">scripts/evaluate_board.py</code>{" "}
        to generate one.
      </Empty>
    );
  }

  const safety = report.invariants.no_unsafe_certification;
  const budget = report.invariants.exhausted_budget_never_approves;
  const total = safety.combinations_checked + budget.combinations_checked;
  const holds = safety.holds && budget.holds;

  return (
    <div className="stack">
      {/* The strongest claim leads, stated once, at size. */}
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 320, damping: 32 }}
      >
        <Panel>
          <p className="label">Exhaustive safety proof</p>
          <p
            style={{
              fontSize: "var(--t-display)",
              fontWeight: 600,
              letterSpacing: "var(--track-display)",
              margin: "var(--s2) 0",
            }}
          >
            <span className="num" style={{ color: holds ? "var(--pass)" : "var(--fail)" }}>
              {total.toLocaleString()}
            </span>{" "}
            <span style={{ fontWeight: 450 }}>
              combinations · {holds ? "no unsafe path exists" : "VIOLATIONS FOUND"}
            </span>
          </p>
          <p className="dim measure" style={{ lineHeight: "var(--lh-body)" }}>
            The decision function has a finite input space, so it is not
            sampled — it is enumerated completely. Across every combination of
            criterion outcome, check kind, severity, confidence band and
            remaining revision budget, there is no input on which Notary
            certifies an asset that a blocking criterion failed or could not
            resolve.
          </p>
        </Panel>
      </motion.div>

      <div className="stats">
        <Stat label="Corpus" value={report.corpus.total} />
        <Stat label="Near threshold" value={report.corpus.near_boundary} />
        <Stat
          label="Safety"
          value={<span style={{ color: holds ? "var(--pass)" : "var(--fail)" }}>
            {holds ? "holds" : "violated"}
          </span>}
        />
        <Stat label="Unscored criteria" value={report.not_evaluated.perceptual_criteria.length} />
      </div>

      <Panel title="Measured checks — scored accuracy" flush>
        <p
          className="dim"
          style={{
            padding: "var(--s3) var(--s4)",
            fontSize: "var(--t-micro)",
            lineHeight: "var(--lh-body)",
            borderBottom: "1px solid var(--hairline)",
          }}
        >
          Scored with <span className="mono">fail</span> as the positive class,
          because a missed violation is the error with consequences. Ground truth
          is constructed rather than labelled — a frame built from a known
          on-palette fraction has a known correct answer — and samples
          concentrate near the decision threshold, the only region a threshold
          classifier can be wrong in.
        </p>

        <div style={{ overflowX: "auto" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Criterion</th>
                <th className="table__num">n</th>
                <th className="table__num">Precision</th>
                <th className="table__num">Recall</th>
                <th className="table__num">Accuracy</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(report.deterministic).map(([name, entry]) => (
                <tr key={name}>
                  <td className="mono" style={{ fontSize: "var(--t-micro)" }}>
                    {name}
                  </td>
                  <td className="table__num dim">{entry.overall.n}</td>
                  <td className="table__num" style={{ color: "var(--pass)" }}>
                    {pct(entry.overall.precision)}
                  </td>
                  <td className="table__num" style={{ color: "var(--pass)" }}>
                    {pct(entry.overall.recall)}
                  </td>
                  <td className="table__num" style={{ color: "var(--pass)" }}>
                    {pct(entry.overall.accuracy)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p
          className="muted"
          style={{
            padding: "var(--s3) var(--s4)",
            fontSize: "var(--t-micro)",
            borderTop: "1px solid var(--hairline)",
          }}
        >
          These are high because these checks are arithmetic, not inference.
          That is the argument for computing what can be computed.
        </p>
      </Panel>

      {/* Equal weight to the gap. This is the point of the screen. */}
      <Panel title="Not evaluated">
        <div className="stack--tight">
          <div className="row">
            {report.not_evaluated.perceptual_criteria.map((c) => (
              <span key={c} className="pill mono">
                {c}
              </span>
            ))}
          </div>
          <p className="dim measure" style={{ lineHeight: "var(--lh-body)" }}>
            {report.not_evaluated.reason}
          </p>
          <p className="measure" style={{ lineHeight: "var(--lh-body)" }}>
            {report.not_evaluated.consequence}
          </p>
        </div>
      </Panel>
    </div>
  );
}
