import type { InvoiceDetail, QueueItem } from "../shared/api/types";

export const queue: QueueItem[] = [
  { number: "OKA-2609-0202", firm: "okafor", firm_name: "Okafor Brandt LLP", matter: "KES-2025-003", matter_name: "Halcyon", period_start: "2026-09-01",
    period_end: "2026-09-30", total: "29068.54", status: "awaiting_review", total_adjustment: "-210.00", degraded: false, pending: 2, top_severity: 700, at_stake: "583.50" },
  { number: "HAR-2608-0180", firm: "harlow", firm_name: "Harlow & Pike LLP", matter: "KES-2025-001", matter_name: "Brenner", period_start: "2026-08-01",
    period_end: "2026-08-31", total: "12000.00", status: "settled", total_adjustment: "0.00", degraded: true, pending: 0, top_severity: 0, at_stake: "0" },
];

export const invoice: InvoiceDetail = {
  invoice: { number: "OKA-2609-0202", firm: "okafor", matter: "KES-2025-003", period_start: "2026-09-01", period_end: "2026-09-30", total: "1584.00",
    status: "awaiting_review", total_adjustment: "-210.00", degraded: false },
  lines: [
    { line_no: "2", type: "F", date: "2026-09-02", units: "1.20", unit_cost: "220.00", total: "264.00", task_code: "L320", expense_code: "",
      activity_code: "A110", timekeeper_id: "OKA010", timekeeper_name: "Viktor Novak", timekeeper_class: "LA", description: "Bates label production documents",
      adjustment: "0" },
    { line_no: "20", type: "F", date: "2026-09-25", units: "1.00", unit_cost: "210.00", total: "210.00", task_code: "L120", expense_code: "",
      activity_code: "A104", timekeeper_id: "OKA013", timekeeper_name: "Pedro Ferreira", timekeeper_class: "OT", description: "Analyze liability exposure",
      adjustment: "-210.00" },
  ],
  findings: [
    { id: "OKA-2609-0202:2:clerical_work", invoice: "OKA-2609-0202", line_no: "2", kind: "clerical_work", source: "model", decided_by: "person",
      action: "reject", adjustment: "-264.00", clause: { document: "kestrel/guidelines@v2", clause_id: "4.1", quote: "Administrative and clerical work is overhead." },
      evidence: [{ source: "model", text: "Bates label production documents" }], confidence: 0.9, severity: 237.6, rationale: "Administrative work.",
      status: "proposed", explanation: "" },
    { id: "OKA-2609-0202:20:nonbillable_role", invoice: "OKA-2609-0202", line_no: "20", kind: "nonbillable_role", source: "rule", decided_by: "rule",
      action: "reject", adjustment: "-210.00", clause: { document: "kestrel/guidelines@v2", clause_id: "2.2", quote: "Time recorded by summer associates is not billable." },
      evidence: [], confidence: 1, severity: 0, rationale: "Summer Associate time is not billable.", status: "auto_applied",
      explanation: "Line 20 is recorded by a timekeeper whose time is not billable." },
  ],
  appeals: [],
  matter: "Halcyon Shipping - pallet collapse",
  firm_name: "Okafor Brandt LLP",
  currency: "EUR",
};

export function mockFetch(routes: Record<string, unknown | ((init?: RequestInit) => unknown)>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const key = Object.keys(routes).find((k) => url === k || url.startsWith(k));
    if (!key) return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    const value = routes[key];
    const body = typeof value === "function" ? (value as (i?: RequestInit) => unknown)(init) : value;
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}
