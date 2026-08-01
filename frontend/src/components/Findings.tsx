import { motion } from "framer-motion";
import type { Finding, Measurement } from "../types";
import { Gauge } from "./ui";

/* ==========================================================================
   The findings ledger

   The one screen element that had to be invented rather than borrowed.

   Half of Notary's findings are computed from pixels — aspect ratio, clip
   duration, colour distance in CIE Lab, exact-match lexemes. Those are facts:
   reproducible by anyone holding the file, with no trust in Notary required.
   The other half are a vision model's opinions, which are useful and may be
   wrong.

   Rendering both as a uniform checklist would be a lie of presentation. So the
   two are set in different registers:

     MEASURED   mono label, an observed value against its limit, and a bar
                showing the distance to that limit. Reads as instrument.
     JUDGED     sans label, a confidence meter, prose rationale. Reads as
                estimate — quieter, because an opinion should not shout as
                loudly as a measurement.

   A reviewer can tell facts from opinions without reading a word.
   ========================================================================== */

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

type Reading = { value: string; limit: string; ratio: number };

/**
 * Turn a measurement into an instrument reading.
 *
 * `ratio` is how far the observed value sits toward its limit, so the bar
 * encodes headroom. Each criterion measures a different quantity, so the
 * mapping is per-criterion rather than a generic dump of the dict — a reviewer
 * should read "111.5 / limit 18" and understand the rejection without prose.
 */
function readingFor(criterion: string, m: Measurement | null): Reading | null {
  if (!m) return null;
  const n = (k: string) => (typeof m[k] === "number" ? (m[k] as number) : null);

  switch (criterion) {
    case "aspect_ratio": {
      const observed = n("observed_ratio");
      const drift = n("relative_drift");
      const tol = n("tolerance") ?? 0.02;
      if (observed == null) return null;
      return {
        value: observed.toFixed(3),
        limit: `±${(tol * 100).toFixed(0)}%`,
        ratio: drift != null ? drift / tol : 0,
      };
    }
    case "duration": {
      const observed = n("observed_seconds");
      const delta = n("delta_seconds");
      const tol = n("tolerance_seconds") ?? 0.5;
      if (observed == null) return null;
      return {
        value: `${observed.toFixed(2)}s`,
        limit: `±${tol}s`,
        ratio: delta != null ? delta / tol : 0,
      };
    }
    case "palette_adherence": {
      const coverage = n("coverage");
      const required = n("required_coverage") ?? 0;
      const de = n("mean_delta_e");
      if (coverage == null) return null;
      return {
        value: `${Math.round(coverage * 100)}%`,
        limit: de != null ? `ΔE ${de.toFixed(1)}` : `min ${Math.round(required * 100)}%`,
        ratio: required > 0 ? coverage / required : 1,
      };
    }
    case "banned_lexemes": {
      const matches = n("matches");
      const checked = n("terms_checked") ?? 0;
      if (matches == null) return null;
      return {
        value: `${matches}`,
        limit: `of ${checked}`,
        ratio: matches > 0 ? 1 : 0.04,
      };
    }
    case "mandatory_disclosure": {
      const missing = n("missing");
      const required = n("required") ?? 0;
      if (missing == null) return null;
      return {
        value: `${required - missing}/${required}`,
        limit: "present",
        ratio: required > 0 ? (required - missing) / required : 1,
      };
    }
    default:
      return null;
  }
}

function Row({ finding, index }: { finding: Finding; index: number }) {
  const measured = finding.kind === "deterministic";
  const reading = measured ? readingFor(finding.criterion, finding.measurement) : null;
  const label = LABEL[finding.criterion] ?? finding.criterion.replace(/_/g, " ");
  const showRationale = finding.rationale && finding.outcome !== "pass";

  return (
    <motion.li
      className="finding"
      data-outcome={finding.outcome}
      data-kind={finding.kind}
      initial={{ opacity: 0, y: -3 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        type: "spring",
        stiffness: 420,
        damping: 36,
        delay: Math.min(index * 0.024, 0.24),
      }}
    >
      <span className="finding__mark" aria-hidden="true">
        {MARK[finding.outcome] ?? "·"}
      </span>

      <span className="finding__label">
        {label}
        {finding.severity === "advisory" && (
          <span className="finding__advisory">advisory</span>
        )}
      </span>

      {reading ? (
        <span
          className="readout-cell"
          title={`${reading.value} against ${reading.limit}`}
        >
          <span className="readout-cell__value">{reading.value}</span>
          <span className="readout-cell__limit">{reading.limit}</span>
          <Gauge ratio={reading.ratio} />
        </span>
      ) : finding.confidence != null ? (
        <span className="confidence" title={`Model confidence ${finding.confidence}`}>
          <Gauge ratio={finding.confidence} />
          {finding.confidence.toFixed(2)}
        </span>
      ) : (
        <span className="confidence confidence--none" title="No confidence reported">
          —
        </span>
      )}

      {showRationale && <p className="finding__rationale">{finding.rationale}</p>}

      <span className="sr-only">
        {measured ? "Measured" : "Reviewed"}: {finding.outcome}
      </span>
    </motion.li>
  );
}

export function Findings({ findings }: { findings: Finding[] }) {
  const measured = findings.filter((f) => f.kind === "deterministic");
  const judged = findings.filter((f) => f.kind === "perceptual");

  if (findings.length === 0) {
    return (
      <div style={{ padding: "var(--s5) var(--s4)" }} className="stack--tight">
        {[0, 1, 2].map((i) => (
          <div key={i} className="row" style={{ flexWrap: "nowrap" }}>
            <span style={{ width: 14 }} />
            <div style={{ flex: 1 }}>
              <div className="skeleton" style={{ height: 9, width: `${58 - i * 9}%` }} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div>
      {measured.length > 0 && (
        <section className="ledger__group">
          <div className="ledger__legend">
            <h3 className="label">Measured</h3>
            <p className="ledger__legend-note">computed from the file · reproducible</p>
          </div>
          <ul>
            {measured.map((f, i) => (
              <Row key={f.criterion} finding={f} index={i} />
            ))}
          </ul>
        </section>
      )}

      {judged.length > 0 && (
        <section className="ledger__group">
          <div className="ledger__legend">
            <h3 className="label">Reviewed</h3>
            <p className="ledger__legend-note">model judgement · uncertainty escalates</p>
          </div>
          <ul>
            {judged.map((f, i) => (
              <Row key={f.criterion} finding={f} index={measured.length + i} />
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
  const word =
    decision === "verified"
      ? "Verified"
      : decision === "rejected"
        ? "Rejected"
        : "Escalated";

  return (
    <motion.div
      className="disposition"
      data-decision={decision}
      role="status"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2 }}
    >
      <span className="label">Disposition</span>
      <span className="disposition__verdict">
        <span className="dot" style={{ background: "currentColor" }} />
        {word}
      </span>
      <p className="disposition__detail">{detail}</p>
    </motion.div>
  );
}
