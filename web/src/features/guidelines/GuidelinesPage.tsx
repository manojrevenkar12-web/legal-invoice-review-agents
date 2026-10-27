// Guideline versions as legal operations sees them: every compiled rule beside the sentence it came from, what the
// verifier refused, where the second extractor disagrees, what changed since the last version, and the approval.

import { useState } from "react";
import { useApprove, useGuidelines, useMe } from "../../shared/api/hooks";
import type { GuidelineVersion, RuleRow } from "../../shared/api/types";
import { day, kindLabel, plain } from "../../shared/ui/format";
import { EmptyState, ErrorState, Skeleton } from "../../shared/ui/states";
import { AskPanel } from "../ask/AskPanel";
import "./guidelines.css";

const pct = (x: unknown) => `${Math.round(Number(x) * 100)}%`;

export function describe(r: RuleRow): string {
  switch (r.kind) {
    case "billing_increment": return `Time recorded in ${plain(r.hours)} h increments`;
    case "max_daily_hours": return `At most ${plain(r.hours)} h per timekeeper per day`;
    case "travel_time": return Number(r.rate_factor) === 0 ? "Travel time is not billable" : `Travel time at ${pct(r.rate_factor)} of the rate`;
    case "internal_conference": return r.policy === "not_billable" ? "Internal conferences are not billable" : "Only the most senior attendee may bill an internal conference";
    case "block_billing": return r.action === "reject" ? "Block-billed entries are rejected" : `Block-billed entries reduced by ${pct(r.reduce_factor)}`;
    case "vague_description": return "Vague descriptions are rejected";
    case "clerical_work": return "Administrative work is not billable";
    case "nonbillable_role": return `Not billable: ${(r.roles as string[]).join(", ")}`;
    case "nonreimbursable_expense": return `Not reimbursed: ${(r.codes as string[]).join(", ")}`;
    case "expense_unit_cap": return `${r.code} at most ${Number(r.max_unit_cost).toFixed(2)} per unit`;
    case "submission_deadline": return `Invoices within ${r.days} days of the period end`;
    case "max_attendees": return `At most ${r.max_timekeepers} timekeepers bill one ${r.event}`;
    case "research_cap": return `Research above ${plain(r.hours_per_month)} h a month is not paid`;
    case "rate_cap": return "Only approved rates";
    case "budget": return "Fees over budget need prior approval";
    default: return kindLabel(r.kind);
  }
}

export function GuidelinesPage() {
  const q = useGuidelines();
  const me = useMe();
  const [chosen, setChosen] = useState<number | null>(null);
  if (q.isPending) return <main className="page"><Skeleton lines={12} label="Loading guidelines" /></main>;
  if (q.isError) return <main className="page"><ErrorState title="Guidelines did not load" error={q.error} onRetry={() => q.refetch()} /></main>;
  if (!q.data.length) {
    return (
      <main className="page">
        <EmptyState title="No guidelines yet">Upload the client's outside-counsel billing guidelines to compile them into rules for approval.</EmptyState>
      </main>
    );
  }
  const v = q.data.find((x) => x.version === chosen) ?? q.data[q.data.length - 1];
  return (
    <main className="page guidelines">
      <header className="page-head">
        <h1>Billing guidelines</h1>
        <p>{me.data?.tenant_name}. Each rule is checked against the sentence it quotes before anyone can approve it.</p>
      </header>
      <nav className="versions" aria-label="Versions">
        {q.data.map((x) => (
          <button key={x.version} type="button" className={`version${x.version === v.version ? " is-active" : ""}`} onClick={() => setChosen(x.version)}>
            <span>Version {x.version}</span>
            <small>{x.status === "approved" ? `in force from ${day(x.effective_from)}` : "draft, not used"}</small>
          </button>
        ))}
      </nav>
      <Version v={v} canApprove={me.data?.role === "legal_ops" || me.data?.role === "admin"} />
      <AskPanel />
    </main>
  );
}

function Version({ v, canApprove }: { v: GuidelineVersion; canApprove: boolean }) {
  const approve = useApprove();
  const rep = v.compile_report;
  return (
    <section className="version-body" aria-label={`Version ${v.version}`}>
      <div className="version-status">
        {v.status === "approved" ? (
          <p>Approved by {v.approved_by} on {day(v.approved_at)}. Compiled by {v.payload.compiled_by}.</p>
        ) : (
          <p>Draft compiled by {v.payload.compiled_by}. Not used for any review until approved.</p>
        )}
        {v.status !== "approved" && canApprove && (
          <button type="button" className="button button-primary" disabled={approve.isPending} onClick={() => approve.mutate(v.version)}>
            Approve version {v.version}
          </button>
        )}
        {approve.isError && <ErrorState title="Approval failed" error={approve.error} />}
      </div>
      {rep && rep.changes.length > 0 && (
        <section>
          <h2>Changed since the previous version</h2>
          <ul className="changes">
            {rep.changes.map((c, i) => (
              <li key={i}>
                <strong>{c.change === "added" ? "Added" : c.change === "removed" ? "Removed" : "Changed"}:</strong>{" "}
                {c.was && <span className="was">{describe({ clause_id: "", quote: "", ...c.was } as RuleRow)}</span>}
                {c.was && c.now && " "}
                {c.now && <span className="now">{describe({ clause_id: "", quote: "", ...c.now } as RuleRow)}</span>}
                {c.clause && <span className="change-clause"> (clause {c.clause})</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
      <table className="rules">
        <caption className="visually-hidden">Rules in version {v.version}</caption>
        <thead>
          <tr><th scope="col">Rule</th><th scope="col">Clause</th><th scope="col">The sentence it rests on</th></tr>
        </thead>
        <tbody>
          {v.payload.rules.map((r) => (
            <tr key={`${r.kind}-${r.clause_id}-${String(r.event ?? r.code ?? "")}`}>
              <td>{describe(r)}</td>
              <td>{r.clause_id}</td>
              <td className="quote">{r.quote}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rep && rep.rejected.length > 0 && (
        <section>
          <h2>Refused by the verifier</h2>
          <ul className="refused">
            {rep.rejected.map((x, i) => (
              <li key={i}><strong>{kindLabel(String(x.proposal.kind))}</strong> (clause {String(x.proposal.clause_id)}): {x.problems.join("; ")}</li>
            ))}
          </ul>
        </section>
      )}
      {rep && (
        <p className="crosscheck">
          {rep.disagreements_with_patterns.length === 0
            ? "The pattern extractor found the same rules."
            : `The pattern extractor differs on ${rep.disagreements_with_patterns.length} rule(s); check them before approving.`}
        </p>
      )}
    </section>
  );
}
