// Shapes returned by the API (src/lira/service/app.py). Money arrives as decimal strings.

export type Role = "reviewer" | "legal_ops" | "firm" | "admin";

export interface Me {
  tenant: string;
  tenant_name: string;
  currency: string;
  role: Role;
  name: string;
  firm: string | null;
}

export interface QueueItem {
  number: string;
  firm: string;
  firm_name: string;
  matter_name: string;
  matter: string;
  period_start: string | null;
  period_end: string | null;
  total: string;
  status: "queued" | "awaiting_review" | "settled" | "rejected_format" | string;
  total_adjustment: string | null;
  degraded: boolean;
  pending: number;
  top_severity: number;
  at_stake: string;
}

export interface ClauseRef {
  document: string;
  clause_id: string;
  quote: string;
}

export interface Evidence {
  source: "line" | "rule" | "nlp" | "model" | "guard" | "cross_line";
  text: string;
}

export interface Finding {
  id: string;
  invoice: string;
  line_no: string | null;
  kind: string;
  source: string;
  decided_by: "rule" | "person";
  action: "reject" | "reduce" | "flag";
  adjustment: string;
  clause: ClauseRef | null;
  evidence: Evidence[];
  confidence: number | null;
  severity: number;
  rationale: string;
  status: "auto_applied" | "proposed" | "accepted" | "rejected" | "withdrawn";
  explanation: string;
  decision?: { decision: string; by: string; note?: string | null } | null;
}

export interface Line {
  line_no: string;
  type: "F" | "E" | "IF" | "IE";
  date: string;
  units: string;
  unit_cost: string;
  total: string;
  task_code: string;
  expense_code: string;
  activity_code: string;
  timekeeper_id: string;
  timekeeper_name: string;
  timekeeper_class: string;
  description: string;
  adjustment: string;
}

export interface Appeal {
  id: number;
  finding_id: string;
  message: string;
  intent: string | null;
  recommendation: { recommendation: string; reason: string; appeal_quote: string } | null;
  outcome: string;
}

export interface InvoiceDetail {
  invoice: {
    number: string;
    firm: string;
    matter: string;
    period_start: string | null;
    period_end: string | null;
    total: string;
    status: string;
    total_adjustment: string | null;
    degraded: boolean;
  };
  lines: Line[];
  findings: Finding[];
  appeals: Appeal[];
  matter: string;
  firm_name: string;
  currency: string;
}

export interface RuleRow {
  kind: string;
  clause_id: string;
  quote: string;
  [param: string]: unknown;
}

export interface GuidelineVersion {
  version: number;
  status: "draft" | "approved" | "superseded";
  effective_from: string;
  approved_by: string | null;
  approved_at: string | null;
  payload: { rules: RuleRow[]; document: string; compiled_by: string };
  compile_report: {
    rejected: { proposal: RuleRow; problems: string[] }[];
    disagreements_with_patterns: unknown[];
    changes: { change: string; rule: string[]; was?: Record<string, unknown>; now?: Record<string, unknown>; clause?: string }[];
    model: string;
  } | null;
}

export interface Answer {
  question: string;
  answer: string;
  citations: string[];
  answerable: boolean;
  verified: boolean;
  problems: string[];
  retrieved: string[];
  latency_ms: number;
}
