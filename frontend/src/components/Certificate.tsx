import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { api } from "../api";
import type { Certificate, VerificationReport } from "../types";
import { Findings } from "./Findings";
import { Lineage } from "./Screens";
import { Button, IconCheck, IconClose, IconShield, Panel, Pill, useToast } from "./ui";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <span className="label">{label}</span>
      <span className="field__v">{children}</span>
    </div>
  );
}

const daysUntil = (iso: string) =>
  Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);

export function CertificateView({
  certificate,
  onBack,
}: {
  certificate: Certificate;
  onBack: () => void;
}) {
  const toast = useToast();
  const [report, setReport] = useState<VerificationReport | null>(null);
  const [verifying, setVerifying] = useState(false);

  const runVerify = async () => {
    setVerifying(true);
    setReport(null);
    try {
      const result = await api.verify(certificate.certificate_id);
      setReport(result.report);
      toast(result.summary, result.passed ? "success" : "error");
    } catch (exc) {
      toast(exc instanceof Error ? exc.message : "Verification failed.", "error");
    } finally {
      setVerifying(false);
    }
  };

  const remaining = daysUntil(certificate.retention_until);
  const signed = certificate.signature !== null;

  return (
    <div className="stack">
      <div className="row">
        <Button variant="ghost" onClick={onBack}>
          ← Library
        </Button>
      </div>

      {/* The seal. The only place in the interface where amber fills anything —
          spending the one warm colour on the one irreversible act. */}
      <motion.div
        className="seal"
        initial={{ opacity: 0, scale: 0.99 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: "spring", stiffness: 300, damping: 30 }}
      >
        <p className="seal__label label">
          <IconShield />
          {signed ? "Sealed · Trust Mode 2 · Ed25519" : "Sealed · Trust Mode 1"}
        </p>
        <p className="dim" style={{ lineHeight: "var(--lh-body)", maxWidth: "62ch" }}>
          {remaining > 0 ? (
            <>
              Under Backblaze B2 Object Lock ({certificate.object_lock_mode}) for
              another <strong style={{ color: "var(--text)" }}>{remaining} day
              {remaining === 1 ? "" : "s"}</strong>, until{" "}
              {new Date(certificate.retention_until).toLocaleDateString()}. Neither
              the media nor this verdict can be altered or deleted before then —
              by anyone, including the account owner.
            </>
          ) : (
            <>Retention has lapsed. Still verifiable, no longer immutable.</>
          )}
        </p>
      </motion.div>

      <div className="split">
        <div className="stack">
          <div className="media">
            <video
              src={api.assetUrl(certificate.certificate_id)}
              poster={certificate.thumbnail_url ? api.posterUrl(certificate.certificate_id) : undefined}
              controls
              playsInline
              preload="metadata"
            />
          </div>

          <Panel title="Lineage" flush>
            <Lineage nodes={certificate.lineage} />
          </Panel>
        </div>

        <div className="stack">
          <Panel
            title="Verify against live storage"
            actions={
              <Button variant="primary" onClick={runVerify} disabled={verifying}>
                {verifying ? "Hashing…" : "Verify now"}
              </Button>
            }
            flush
          >
            <p
              className="dim"
              style={{
                padding: "var(--s3) var(--s4)",
                fontSize: "var(--t-micro)",
                lineHeight: "var(--lh-body)",
                borderBottom: report ? "1px solid var(--hairline)" : undefined,
              }}
            >
              Fetches the object from Backblaze B2 right now, recomputes SHA-256
              over the bytes that actually arrive, and checks the Ed25519
              signature against the canonical manifest hash. Nothing stored is
              trusted.
            </p>

            <AnimatePresence>
              {report && (
                <motion.ul
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ type: "spring", stiffness: 300, damping: 34 }}
                >
                  {report.checks.map((check, i) => (
                    <motion.li
                      key={check.name}
                      className="finding"
                      data-outcome={check.passed ? "pass" : "fail"}
                      initial={{ opacity: 0, y: -3 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: i * 0.06 }}
                    >
                      <span className="finding__mark">
                        {check.passed ? <IconCheck /> : <IconClose />}
                      </span>
                      <span className="finding__label">
                        {check.name.replace(/_/g, " ")}
                      </span>
                      <span />
                      <p className="finding__rationale">{check.detail}</p>
                    </motion.li>
                  ))}
                  <li style={{ padding: "var(--s3) var(--s4)" }}>
                    <p className="label">
                      {report.bytes_hashed.toLocaleString()} bytes re-hashed
                    </p>
                  </li>
                </motion.ul>
              )}
            </AnimatePresence>
          </Panel>

          <Panel title="Provenance" flush>
            <div className="fields">
              <Field label="Certificate">
                <span className="mono">{certificate.certificate_id}</span>
              </Field>
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
          </Panel>

          <Panel
            title="Sealed verdict"
            actions={<Pill accent>{certificate.verdict.decision}</Pill>}
            flush
          >
            <Findings findings={certificate.verdict.criteria} />
          </Panel>
        </div>
      </div>
    </div>
  );
}
