import { useEffect, useState } from "react";
import { api } from "../api";
import type { EvaluationReport } from "../types";

function pct(value: number | null): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

/**
 * The evidence panel.
 *
 * Most review products assert that their classifier works. This screen shows
 * the measurement, and — more importantly — shows what was NOT measured. The
 * "not evaluated" block is deliberately given the same visual weight as the
 * scores, because a reviewer deciding whether to trust this system needs the
 * gap more than they need the wins.
 */
export function Trust() {
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

  if (error) {
    return (
      <div className="notice notice--warn" role="alert">
        {error}
      </div>
    );
  }

  if (!report) {
    return <p className="eyebrow">Loading evaluation…</p>;
  }

  if (!report.available) {
    return (
      <div className="empty">
        <p className="empty__title">No evaluation report</p>
        <p>{report.reason}</p>
      </div>
    );
  }

  const safety = report.invariants.no_unsafe_certification;
  const budget = report.invariants.exhausted_budget_never_approves;
  const bothHold = safety.holds && budget.holds;

  return (
    <div className="stack">
      {/* The strongest claim goes first. */}
      <section className={bothHold ? "seal" : "panel"}>
        <p className="seal__label">Exhaustive safety proof</p>
        <p style={{ margin: "0 0 var(--gap-sm)", fontSize: "var(--step-1)" }}>
          <strong className="mono">
            {(safety.combinations_checked + budget.combinations_checked).toLocaleString()}
          </strong>{" "}
          decision combinations enumerated —{" "}
          <strong>{bothHold ? "no unsafe certification is reachable" : "VIOLATIONS FOUND"}</strong>
        </p>
        <p className="seal__retention">
          The decision function has a finite input space, so it is not sampled,
          it is enumerated completely. Across every combination of criterion
          outcome, check kind, severity, confidence band, and remaining revision
          budget, there is no input on which Notary certifies an asset that a
          blocking criterion failed or could not resolve.
        </p>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2 className="eyebrow">Invariants</h2>
        </div>
        <div className="panel__body">
          {[
            {
              label: "A blocking FAIL or UNCERTAIN can never yield VERIFIED",
              data: safety,
            },
            {
              label: "An exhausted revision budget can never yield VERIFIED",
              data: budget,
            },
          ].map((row) => (
            <div
              key={row.label}
              className={`check check--${row.data.holds ? "pass" : "fail"}`}
            >
              <span className="check__mark" aria-hidden="true">
                {row.data.holds ? "✓" : "✕"}
              </span>
              <div>
                <p className="check__name">{row.data.holds ? "holds" : "violated"}</p>
                <p className="check__detail">{row.label}</p>
                <p className="lineage__meta">
                  {row.data.combinations_checked.toLocaleString()} combinations checked
                </p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2 className="eyebrow">Measured checks — scored accuracy</h2>
          <span className="eyebrow">
            {report.corpus.total} samples · {report.corpus.near_boundary} near threshold
          </span>
        </div>
        <div className="panel__body">
          <p
            style={{
              marginTop: 0,
              fontSize: "var(--step--1)",
              color: "var(--ink-dim)",
            }}
          >
            Scored with <span className="mono">fail</span> as the positive class,
            because a missed violation is the error with consequences. Ground
            truth is constructed rather than labelled — a frame built from a
            known on-palette fraction has a known correct answer. Samples
            concentrate near the decision threshold, which is the only region a
            threshold classifier can be wrong in.
          </p>

          <div style={{ overflowX: "auto" }}>
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: "var(--step--1)",
              }}
            >
              <thead>
                <tr>
                  {["Criterion", "n", "Precision", "Recall", "Accuracy"].map((h) => (
                    <th
                      key={h}
                      className="eyebrow"
                      style={{
                        textAlign: h === "Criterion" ? "left" : "right",
                        padding: "0.4rem 0.5rem",
                        borderBottom: "1px solid var(--rule-bright)",
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(report.deterministic).map(([name, entry]) => (
                  <tr key={name}>
                    <td
                      className="mono"
                      style={{
                        padding: "0.45rem 0.5rem",
                        borderBottom: "1px solid rgba(38,46,57,.55)",
                      }}
                    >
                      {name}
                    </td>
                    {[
                      String(entry.overall.n),
                      pct(entry.overall.precision),
                      pct(entry.overall.recall),
                      pct(entry.overall.accuracy),
                    ].map((value, index) => (
                      <td
                        key={index}
                        className="mono"
                        style={{
                          textAlign: "right",
                          padding: "0.45rem 0.5rem",
                          borderBottom: "1px solid rgba(38,46,57,.55)",
                          color: "var(--pass)",
                        }}
                      >
                        {value}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p
            style={{
              fontSize: "var(--step--1)",
              color: "var(--ink-faint)",
              marginBottom: 0,
            }}
          >
            These are high because these checks are arithmetic, not inference.
            That is the argument for computing what can be computed.
          </p>
        </div>
      </section>

      {/* Equal weight to the gap. This is the point of the screen. */}
      <section className="panel" style={{ borderColor: "var(--escalate)" }}>
        <div className="panel__head">
          <h2 className="eyebrow" style={{ color: "var(--escalate)" }}>
            Not evaluated
          </h2>
        </div>
        <div className="panel__body">
          <div className="row" style={{ marginBottom: "var(--gap-sm)" }}>
            {report.not_evaluated.perceptual_criteria.map((c) => (
              <span key={c} className="mode-pill">
                {c}
              </span>
            ))}
          </div>
          <p style={{ marginTop: 0 }}>{report.not_evaluated.reason}</p>
          <p style={{ marginBottom: 0, color: "var(--ink-dim)" }}>
            {report.not_evaluated.consequence}
          </p>
        </div>
      </section>
    </div>
  );
}
