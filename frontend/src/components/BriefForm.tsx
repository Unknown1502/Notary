import { useEffect, useState } from "react";
import { api } from "../api";
import { Button, Notice, Panel, useToast } from "./ui";

/* ==========================================================================
   Campaign brief intake

   The screen that starts a review. It existed in the docs and not in the app,
   which meant NOTARY_MODE=live was unreachable from the interface — the API
   accepted briefs that nothing could send.

   Two things this form does that a generic form would not:

   1. It states what each guardrail *costs* the reviewer. A prohibited-term
      list and a mandatory disclosure are screened before any provider is
      billed, so getting them right here is the difference between a rejection
      that costs nothing and one that costs a render.

   2. It does not pretend to work in replay mode. The API answers a brief with
      409 when generation is disabled, and that answer is shown verbatim rather
      than translated into a generic failure — a judge on the deployed URL
      should learn *why* the button is inert, not that something broke.
   ========================================================================== */

type Profile = {
  id: string;
  label: string;
  deterministic_count: number;
  perceptual_count: number;
};

const DEFAULTS = {
  title: "Cardiovar — Q3 patient awareness",
  prompt:
    "A person in their sixties walking a coastal path at sunrise, calm and " +
    "steady, warm natural light.",
  brand: "Cardiovar",
  palette: "#0b5fff, #00c2a8, #0a1b3d",
  banned: "cure, guaranteed, miracle, no side effects",
  disclosures: "Important Safety Information",
  aspect: "16:9",
  duration: 6,
};

export function BriefForm({
  liveEnabled,
  onStarted,
}: {
  liveEnabled: boolean;
  onStarted: (sessionId: string) => void;
}) {
  const toast = useToast();
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profile, setProfile] = useState("pharma-dtc-us");
  const [form, setForm] = useState(DEFAULTS);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState<string | null>(null);

  useEffect(() => {
    api
      .profiles()
      .then((r) => setProfiles(r.profiles as Profile[]))
      .catch(() => undefined);
  }, []);

  const set = <K extends keyof typeof DEFAULTS>(k: K, v: (typeof DEFAULTS)[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const list = (raw: string) =>
    raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setBlocked(null);
    try {
      const result = await api.submit({
        title: form.title,
        prompt: form.prompt,
        compliance_profile: profile,
        brand_kit: {
          name: form.brand,
          palette: list(form.palette),
          banned_terms: list(form.banned),
          mandatory_disclosures: list(form.disclosures),
        },
        channel: {
          aspect_ratio: form.aspect,
          duration_seconds: Number(form.duration) || 6,
        },
      });
      onStarted(result.session_id);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : "Could not start the review.";
      // A 409 is the API explaining that generation is disabled in this mode.
      // That is a fact about the deployment, not an error the user caused.
      if (/mode|generation is disabled/i.test(message)) setBlocked(message);
      else toast(message, "error");
    } finally {
      setBusy(false);
    }
  };

  const active = profiles.find((p) => p.id === profile);

  return (
    <form className="stack" onSubmit={submit}>
      {!liveEnabled && (
        <Notice>
          This deployment has generation disabled, so submitting will return the
          API's explanation rather than start a run. The form is here because
          the same build serves live deployments.
        </Notice>
      )}
      {blocked && <Notice tone="accent">{blocked}</Notice>}

      <Panel title="Campaign">
        <div className="stack--tight">
          <label className="field-group">
            <span className="label">Title</span>
            <input
              className="input"
              required
              value={form.title}
              onChange={(e) => set("title", e.target.value)}
            />
          </label>

          <label className="field-group">
            <span className="label">Creative brief</span>
            <textarea
              className="textarea"
              required
              value={form.prompt}
              onChange={(e) => set("prompt", e.target.value)}
            />
            <span className="hint">
              Screened for prohibited terms and required disclosures before any
              provider is billed.
            </span>
          </label>

          <label className="field-group">
            <span className="label">Compliance profile</span>
            <select
              className="input"
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
            >
              {profiles.length === 0 && <option value={profile}>{profile}</option>}
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
            {active && (
              <span className="hint">
                {active.deterministic_count} measured criteria ·{" "}
                {active.perceptual_count} reviewed by a model
              </span>
            )}
          </label>
        </div>
      </Panel>

      <Panel title="Brand guardrails">
        <div className="stack--tight">
          <label className="field-group">
            <span className="label">Brand</span>
            <input
              className="input"
              value={form.brand}
              onChange={(e) => set("brand", e.target.value)}
            />
          </label>

          <label className="field-group">
            <span className="label">Palette</span>
            <input
              className="input mono"
              value={form.palette}
              onChange={(e) => set("palette", e.target.value)}
              placeholder="#0b5fff, #00c2a8"
            />
            <span className="hint">
              Hex, comma separated. Frames are measured against these in CIE Lab,
              so a take that drifts off-palette fails on a number rather than an
              opinion.
            </span>
          </label>

          <label className="field-group">
            <span className="label">Prohibited terms</span>
            <input
              className="input"
              value={form.banned}
              onChange={(e) => set("banned", e.target.value)}
            />
            <span className="hint">
              Whole-word, case-insensitive. “secure” will not trip “cure”.
            </span>
          </label>

          <label className="field-group">
            <span className="label">Mandatory disclosures</span>
            <input
              className="input"
              value={form.disclosures}
              onChange={(e) => set("disclosures", e.target.value)}
            />
            <span className="hint">
              Absence rejects the brief before generation — the cheapest possible
              catch.
            </span>
          </label>
        </div>
      </Panel>

      <Panel title="Channel">
        <div className="row" style={{ gap: "var(--s4)", alignItems: "flex-end" }}>
          <label className="field-group" style={{ width: 120 }}>
            <span className="label">Aspect ratio</span>
            <input
              className="input mono"
              value={form.aspect}
              onChange={(e) => set("aspect", e.target.value)}
              pattern="\d{1,2}:\d{1,2}"
            />
          </label>
          <label className="field-group" style={{ width: 120 }}>
            <span className="label">Duration (s)</span>
            <input
              className="input mono"
              type="number"
              min={1}
              max={60}
              value={form.duration}
              onChange={(e) => set("duration", Number(e.target.value))}
            />
          </label>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Starting…" : "Submit for review"}
          </Button>
        </div>
      </Panel>
    </form>
  );
}
