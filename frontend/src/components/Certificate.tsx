import { useState } from "react";
import { api } from "../api";
import type { Certificate, VerificationReport } from "../types";
import { Findings } from "./Findings";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field-row">
      <span className="field-row__key">{label}</span>
      <span className="field-row__value">{children}</span>
    </div>
  );
}

function daysUntil(iso: string): number {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

export function CertificatePanel({ certificate }: { certificate: Certificate }) {
  const [report, setReport] = useState<VerificationReport | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runVerify = async () => {
    setVerifying(true);
    setError(null);
    setReport(null);
    try {
      const result = await api.verify(certificate.certificate_id);
      setReport(result.report);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Verification failed.");
    } finally {
      setVerifying(false);
    }
  };

  const remaining = daysUntil(certificate.retention_until);
  const signed = certificate.signature !== null;

  return (
    <div className="stack">
      {/* The seal. Brass appears here and nowhere else in the interface. */}
      <div className="seal">
        <p className="seal__label">
          {signed ? "Sealed · Trust Mode 2" : "Sealed · Trust Mode 1"}
        </p>
        <p className="seal__retention">
          {remaining > 0 ? (
            <>
              Under Backblaze B2 Object Lock ({certificate.object_lock_mode}) for
              another <strong>{remaining} day{remaining === 1 ? "" : "s"}</strong>,
              until {new Date(certificate.retention_until).toLocaleDateString()}.
              Neither the video nor this verdict can be altered or deleted before
              then — by anyone, including the account owner.
            </>
          ) : (
            <>Retention lapsed. Still verifiable, no longer immutable.</>
          )}
        </p>
      </div>

      {certificate.asset_url && (
        <video className="frame" src={certificate.asset_url} controls playsInline />
      )}

      <section className="panel">
        <div className="panel__head">
          <h2 className="eyebrow">Provenance</h2>
          <span className="eyebrow">{certificate.certificate_id}</span>
        </div>
        <div className="panel__body">
          <Field label="Provider">{certificate.provider}</Field>
          <Field label="Model">
            <span className="mono">{certificate.model}</span>
          </Field>
          <Field label="Prompt">{certificate.prompt}</Field>
          <Field label="Asset SHA-256">
            <p className="hash">{certificate.sha256}</p>
          </Field>
          <Field label="Manifest hash">
            <p className="hash">{certificate.manifest_hash}</p>
          </Field>
          {certificate.signature && (
            <>
              <Field label="Signature">
                <p className="hash">{certificate.signature.signature}</p>
              </Field>
              <Field label="Signing key">
                <span className="mono">
                  {certificate.signature.algorithm} · {certificate.signature.key_id}
                </span>
              </Field>
              <Field label="Public key">
                <p className="hash">{certificate.signature.public_key}</p>
              </Field>
            </>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2 className="eyebrow">Verify against live storage</h2>
          <button className="btn" onClick={runVerify} disabled={verifying}>
            {verifying ? "Hashing…" : "Verify now"}
          </button>
        </div>
        <div className="panel__body">
          <p style={{ marginTop: 0, color: "var(--ink-dim)", fontSize: "var(--step--1)" }}>
            Fetches the object from Backblaze B2 right now, recomputes its
            SHA-256 over the bytes returned, and checks the Ed25519 signature
            against the canonical manifest hash. Nothing stored is trusted.
          </p>

          {error && (
            <div className="notice notice--warn" role="alert">
              {error}
            </div>
          )}

          {report && (
            <>
              {report.checks.map((check) => (
                <div
                  key={check.name}
                  className={`check check--${check.passed ? "pass" : "fail"}`}
                >
                  <span className="check__mark" aria-hidden="true">
                    {check.passed ? "✓" : "✕"}
                  </span>
                  <div>
                    <p className="check__name">{check.name.replace(/_/g, " ")}</p>
                    <p className="check__detail">{check.detail}</p>
                    {check.observed && check.name === "asset_integrity" && (
                      <p className="hash">{check.observed}</p>
                    )}
                  </div>
                </div>
              ))}
              <p
                className="eyebrow"
                style={{ marginTop: "var(--gap)", color: "var(--ink-dim)" }}
              >
                {report.bytes_hashed.toLocaleString()} bytes re-hashed from{" "}
                {new URL(report.source).host}
              </p>
            </>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2 className="eyebrow">Sealed verdict</h2>
          <span className="eyebrow">{certificate.verdict.decision}</span>
        </div>
        <div className="panel__body">
          <Findings findings={certificate.verdict.criteria} />
        </div>
      </section>
    </div>
  );
}
