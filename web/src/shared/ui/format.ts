// Formatting shared by every screen: money, dates, finding names in reviewer's words.

const formatters = new Map<string, Intl.NumberFormat>();

export function money(value: string | number | null | undefined, currency: string): string {
  const n = typeof value === "number" ? value : Number(value ?? 0);
  let f = formatters.get(currency);
  if (!f) {
    f = new Intl.NumberFormat("en-GB", { style: "currency", currency, minimumFractionDigits: 2 });
    formatters.set(currency, f);
  }
  return f.format(n);
}

export function day(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function dayShort(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function month(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

export const KIND: Record<string, string> = {
  block_billing: "Several tasks in one entry",
  vague_description: "Description too vague",
  clerical_work: "Administrative work",
  internal_conference: "Internal conference",
  rate_cap: "Above approved rate",
  billing_increment: "Wrong time increment",
  max_daily_hours: "Over the daily hours limit",
  travel_time: "Travel time",
  nonbillable_role: "Timekeeper not billable",
  nonreimbursable_expense: "Expense not reimbursed",
  expense_unit_cap: "Expense above unit cap",
  submission_deadline: "Submitted late",
  max_attendees: "Too many attendees",
  research_cap: "Research above monthly limit",
  budget: "Over budget",
  task_code_mismatch: "Task code does not match",
  unusual_time: "Unusual hours for the task",
  prompt_injection: "Instruction hidden in the entry",
  unauthorized: "Timekeeper not authorised",
  format_error: "File problem",
};

export const kindLabel = (k: string) => KIND[k] ?? k.replace(/_/g, " ");

export const FIRM_STATUS: Record<string, string> = {
  awaiting_review: "With the client's reviewer",
  settled: "Settled",
  queued: "Received",
  rejected_format: "Returned: file problems",
};

export const plain = (x: unknown) => (typeof x === "number" || (typeof x === "string" && /^-?\d+(\.\d+)?$/.test(x)) ? String(Number(x)) : String(x));

export const STATUS: Record<string, string> = {
  awaiting_review: "Waiting for you",
  settled: "Settled",
  queued: "Being reviewed",
  rejected_format: "Returned to firm",
};
