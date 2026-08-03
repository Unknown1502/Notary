export type Outcome = "pass" | "fail" | "uncertain" | "not_applicable";
export type CheckKind = "deterministic" | "perceptual";
export type Decision = "verified" | "rejected" | "escalated";

export interface Measurement {
  [key: string]: number | string | boolean;
}

export interface Finding {
  criterion: string;
  outcome: Outcome;
  kind: CheckKind;
  severity: "blocking" | "advisory";
  rationale: string;
  confidence: number | null;
  measurement: Measurement | null;
  evidence_frame: string | null;
}

export interface StreamEvent {
  type: string;
  session_id: string;
  sequence: number;
  timestamp: number;
  replayed?: boolean;
  [key: string]: unknown;
}

export interface LineageNode {
  run_id: string;
  parent_run_id: string | null;
  take_number: number;
  status: string;
  decision: Decision | null;
  used_fallback: boolean;
  created_at: string;
}

export interface QueueItem {
  session_id: string;
  reason: string;
  campaign_title: string;
  tenant: string;
  compliance_profile: string;
  waiting_since: string;
  take_number: number;
  asset_url: string | null;
  thumbnail_url: string | null;
  verdict: { criteria: Finding[]; summary: string; decision: Decision } | null;
}

export interface LibraryAsset {
  certificate_id: string;
  asset_id: string;
  campaign_id: string;
  tenant: string;
  asset_url: string;
  thumbnail_url: string | null;
  /**
   * App-mediated media URLs. `asset_url` and `thumbnail_url` are sealed into
   * the certificate and cannot be amended, so they record where a copy lived
   * at certification -- not somewhere this browser can fetch. Always render
   * from these instead.
   */
  playback_url: string;
  poster_url: string | null;
  model: string;
  provider: string;
  certified_at: string;
  retention_until: string;
  trust_mode: number;
  is_sealed: boolean;
  decision: Decision;
  takes: number;
  prompt: string;
}

export interface SignatureBlock {
  algorithm: string;
  key_id: string;
  public_key: string;
  signature: string;
  signed_at: string;
  canonical_hash: string;
}

export interface Certificate {
  certificate_id: string;
  asset_id: string;
  campaign_id: string;
  tenant: string;
  run_id: string;
  asset_url: string;
  thumbnail_url: string | null;
  sha256: string;
  manifest_hash: string;
  signature: SignatureBlock | null;
  provider: string;
  model: string;
  prompt: string;
  parameters: Record<string, unknown>;
  verdict: { criteria: Finding[]; summary: string; decision: Decision };
  lineage: LineageNode[];
  certified_at: string;
  retention_until: string;
  object_lock_mode: string;
}

export interface VerificationCheck {
  name: string;
  passed: boolean;
  detail: string;
  expected: string | null;
  observed: string | null;
}

export interface VerificationReport {
  certificate_id: string;
  verified_at: string;
  checks: VerificationCheck[];
  bytes_hashed: number;
  source: string;
}

export interface Recording {
  session_id: string;
  title: string;
  recorded_at: string;
  source_mode: string;
  event_count: number;
  wall_clock_seconds: number;
  verdicts: (string | null)[];
  took_fallback: boolean;
  escalated: boolean;
  certified: boolean;
}

export interface ConfusionMatrix {
  n: number;
  tp: number;
  fp: number;
  tn: number;
  fn: number;
  correct_abstain: number;
  incorrect_abstain: number;
  precision: number | null;
  recall: number | null;
  accuracy: number | null;
  f1: number | null;
}

export interface InvariantResult {
  combinations_checked: number;
  holds: boolean;
  violations: unknown[];
}

export type EvaluationReport =
  | { available: false; reason: string }
  | {
      available: true;
      corpus: {
        total: number;
        near_boundary: number;
        boundary_fraction: number;
        by_criterion: Record<string, number>;
        by_expected: Record<string, number>;
      };
      deterministic: Record<
        string,
        { overall: ConfusionMatrix; near_boundary: ConfusionMatrix; failures: unknown[] }
      >;
      invariants: {
        no_unsafe_certification: InvariantResult;
        exhausted_budget_never_approves: InvariantResult;
      };
      not_evaluated: {
        perceptual_criteria: string[];
        reason: string;
        consequence: string;
      };
    };

export interface Health {
  status: string;
  runtime: {
    mode: string;
    generates_for_real: boolean;
    reads_real_storage: boolean;
    retention_days: number;
    trust_mode: number;
    signing_key_id: string;
    vault_bucket: string | null;
  };
  genblaze: { genblaze_available: boolean; import_error: string | null };
  ffmpeg_available: boolean;
  recordings: number;
  stats: Record<string, number | boolean>;

  /** Live B2 posture. Absent when storage is unconfigured (replay mode). */
  storage?: {
    available: boolean;
    reason?: string;
    retention_days?: number;
    buckets?: Record<string, { name: string; object_lock?: string }>;
  };
}
