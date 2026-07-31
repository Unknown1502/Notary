import type { Finding, Measurement } from "../types";

const MARK: Record<string, string> = {
  pass: "✓",
  fail: "✕",
  uncertain: "?",
  not_applicable: "–",
};

const LABEL: Record<string, string> = {
  aspect_ratio: "Aspect ratio",
  duration: "Duration",
  palette_adherence: "Brand palette",
  banned_lexemes: "Prohibited terms",
  mandatory_disclosure: "Safety disclosure",
  logo_presence: "Logo present",
  logo_legibility: "Logo legibility",
  visual_artifacts: "Generation artifacts",
  tone_alignment: "Tone",
  prohibited_imagery: "Prohibited depiction",
};

/**
 * Render a measurement as an instrument reading: observed against its limit.
 *
 * Each criterion measures a different quantity, so the readout is chosen per
 * criterion rather than dumping the raw dict. A reviewer should be able to
 * read "dE 111.5 / limit 18" and understand the rejection without prose.
 */
function readout(criterion: string, m: Measurement | null) {
  if (!m) return null;
  const num = (k: string) => (typeof m[k] === "number" ? (m[k] as number) : null);

  switch (criterion) {
    case "aspect_ratio": {
      const observed = num("observed_ratio");
      const expected = num("expected_ratio");
      if (observed == null || expected == null) return null;
      return { value: observed.toFixed(3), limit: `target ${expected.toFixed(3)}` };
    }
    case "duration": {
      const observed = num("observed_seconds");
      const expected = num("expected_seconds");
      const tol = num("tolerance_seconds");
      if (observed == null || expected == null) return null;
      return {
        value: `${observed.toFixed(2)}s`,
        limit: `target ${expected}s ±${tol ?? 0.5}`,
      };
    }
    case "palette_adherence": {
      const coverage = num("coverage");
      const required = num("required_coverage");
      const meanDe = num("mean_delta_e");
      if (coverage == null) return null;
      return {
        value: `${Math.round(coverage * 100)}%`,
        limit:
          meanDe != null
            ? `min ${Math.round((required ?? 0) * 100)}% · dE ${meanDe.toFixed(1)}`
            : `min ${Math.round((required ?? 0) * 100)}%`,
      };
    }
    case "banned_lexemes": {
      const matches = num("matches");
      const checked = num("terms_checked");
      if (matches == null) return null;
      return { value: `${matches}`, limit: `of ${checked ?? 0} terms` };
    }
    case "mandatory_disclosure": {
      const missing = num("missing");
      const required = num("required");
      if (missing == null) return null;
      return { value: `${(required ?? 0) - missing}/${required ?? 0}`, limit: "present" };
    }
    default:
      return null;
  }
}

function FindingRow({ finding }: { finding: Finding }) {
  const measured = finding.kind === "deterministic";
  const reading = measured ? readout(finding.criterion, finding.measurement) : null;
  const label = LABEL[finding.criterion] ?? finding.criterion.replace(/_/g, " ");

  return (
    <li
      className={`finding finding--${finding.outcome} finding--${
        measured ? "measured" : "reviewed"
      }`}
    >
      <span className="finding__mark" aria-hidden="true">
        {MARK[finding.outcome] ?? "·"}
      </span>

      <span className="finding__label">
        {label}
        {finding.severity === "advisory" && (
          <span className="eyebrow" style={{ marginLeft: ".5rem" }}>
            advisory
          </span>
        )}
      </span>

      {reading ? (
        <span className="readout">
          <span className="readout__value">{reading.value}</span>
          <span className="readout__limit">{reading.limit}</span>
        </span>
      ) : finding.confidence != null ? (
        <span className="confidence" title={`Model confidence ${finding.confidence}`}>
          <span className="confidence__track">
            <span
              className="confidence__fill"
              style={{ width: `${Math.round(finding.confidence * 100)}%` }}
            />
          </span>
          {finding.confidence.toFixed(2)}
        </span>
      ) : (
        <span className="confidence">—</span>
      )}

      {finding.rationale && finding.outcome !== "pass" && (
        <p className="finding__rationale">{finding.rationale}</p>
      )}
    </li>
  );
}

export function Findings({ findings }: { findings: Finding[] }) {
  const measured = findings.filter((f) => f.kind === "deterministic");
  const reviewed = findings.filter((f) => f.kind === "perceptual");

  if (findings.length === 0) {
    return (
      <p className="eyebrow" style={{ padding: "1rem 0" }}>
        Awaiting the first finding…
      </p>
    );
  }

  return (
    <div>
      {measured.length > 0 && (
        <section className="findings__group">
          <div className="findings__legend">
            <h3 className="eyebrow">Measured</h3>
            <p className="findings__legend-note">
              computed from the file · reproducible
            </p>
          </div>
          <ul className="lineage" style={{ listStyle: "none" }}>
            {measured.map((f) => (
              <FindingRow key={f.criterion} finding={f} />
            ))}
          </ul>
        </section>
      )}

      {reviewed.length > 0 && (
        <section className="findings__group">
          <div className="findings__legend">
            <h3 className="eyebrow">Reviewed</h3>
            <p className="findings__legend-note">
              model judgement · uncertainty escalates
            </p>
          </div>
          <ul className="lineage" style={{ listStyle: "none" }}>
            {reviewed.map((f) => (
              <FindingRow key={f.criterion} finding={f} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export function Disposition({
  decision,
  detail,
}: {
  decision: string;
  detail: string;
}) {
  const verdictWord =
    decision === "verified"
      ? "Verified"
      : decision === "rejected"
        ? "Rejected"
        : "Escalated";

  return (
    <div className={`disposition disposition--${decision}`} role="status">
      <span className="eyebrow">Disposition</span>
      <span className="disposition__verdict">{verdictWord}</span>
      <p className="disposition__detail">{detail}</p>
    </div>
  );
}
