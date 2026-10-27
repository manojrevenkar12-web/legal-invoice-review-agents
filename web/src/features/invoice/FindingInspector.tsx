// The evidence for one line: what each finding rests on (the entry's own words, the client's clause in the
// document's typeface, the classifier and the model), who decides it, and the reviewer's accept or dismiss.

import { AnimatePresence, motion as m } from "framer-motion";
import { useDecide, useDecideAppeal } from "../../shared/api/hooks";
import type { Finding, InvoiceDetail } from "../../shared/api/types";
import { motion } from "../../shared/styles/theme";
import { kindLabel, money } from "../../shared/ui/format";
import { EmptyState, ErrorState } from "../../shared/ui/states";
import "./invoice.css";

const SOURCE: Record<string, string> = {
  line: "From the entry",
  rule: "From the records",
  nlp: "Classifier",
  model: "Model, quoting the entry",
  guard: "Instruction detector",
  cross_line: "Other lines",
};

export function FindingInspector({ detail, line }: { detail: InvoiceDetail; line: string | null }) {
  const decide = useDecide(detail.invoice.number);
  const findings = detail.findings.filter((f) => (line ? f.line_no === line : f.status === "proposed"));
  const entry = line ? detail.lines.find((l) => l.line_no === line) : null;
  const open = findings.filter((f) => f.status === "proposed");
  const title = line ? `Line ${line}` : "Open findings";
  return (
    <aside className="inspector" aria-label="Findings">
      <header className="inspector-head">
        <h2>{title}</h2>
        {open.length > 1 && (
          <div className="inspector-bulk">
            <button type="button" className="button button-deduct" disabled={decide.isPending}
                    onClick={() => decide.mutate(Object.fromEntries(open.map((f) => [f.id, { decision: "accept" as const }])))}>
              Accept {open.length}
            </button>
            <button type="button" className="button button-kept" disabled={decide.isPending}
                    onClick={() => decide.mutate(Object.fromEntries(open.map((f) => [f.id, { decision: "reject" as const }])))}>
              Dismiss {open.length}
            </button>
          </div>
        )}
      </header>
      {entry && <p className="inspector-entry">{entry.description}</p>}
      {decide.isError && <ErrorState title="Your decision was not saved" error={decide.error} onRetry={() => decide.reset()} />}
      {findings.length === 0 ? (
        <EmptyState title={line ? "Nothing to decide on this line" : "Nothing waits for you on this invoice"}>
          {line ? "The line is paid as billed unless a rule applied to it." : "Select a line to see what the review found."}
        </EmptyState>
      ) : (
        <AnimatePresence mode="popLayout" initial={false}>
          <ol className="inspector-list">
            {findings.map((f) => (
              <m.li key={f.id} layout initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }}
                    transition={{ duration: motion.base, ease: motion.ease }}>
                <FindingCard f={f} currency={detail.currency} busy={decide.isPending}
                             onDecide={(decision) => decide.mutate({ [f.id]: { decision } })} />
              </m.li>
            ))}
          </ol>
        </AnimatePresence>
      )}
      <Appeals detail={detail} line={line} />
    </aside>
  );
}

function FindingCard({ f, currency, busy, onDecide }: { f: Finding; currency: string; busy: boolean; onDecide: (d: "accept" | "reject") => void }) {
  const who =
    f.status === "auto_applied" ? "Applied by an approved rule" :
    f.status === "proposed" ? "Waiting for your decision" :
    f.status === "accepted" ? `Accepted by ${f.decision?.by ?? "a reviewer"}` :
    f.status === "rejected" ? `Dismissed by ${f.decision?.by ?? "a reviewer"}` : f.status;
  const amount = Number(f.adjustment);
  return (
    <article className={`finding status-${f.status}`}>
      <header className="finding-head">
        <h3>{kindLabel(f.kind)}</h3>
        {amount !== 0 && <span className="finding-amount">{money(amount, currency)}</span>}
      </header>
      <p className="finding-who">
        {who}
        {f.confidence != null && f.status === "proposed" ? `, confidence ${Math.round(f.confidence * 100)}%` : ""}
      </p>
      {f.clause && (
        <blockquote className="clause">
          <p>{f.clause.quote}</p>
          <footer>Clause {f.clause.clause_id}</footer>
        </blockquote>
      )}
      {f.evidence.length > 0 && (
        <dl className="evidence">
          {f.evidence.map((e, i) => (
            <div key={i}>
              <dt>{SOURCE[e.source] ?? e.source}</dt>
              <dd>{e.text}</dd>
            </div>
          ))}
        </dl>
      )}
      {f.rationale && <p className="finding-rationale">{f.rationale}</p>}
      {f.status === "proposed" && (
        <div className="finding-actions">
          <button type="button" className="button button-deduct" disabled={busy} onClick={() => onDecide("accept")}>
            {f.action === "flag" ? "Confirm" : amount ? `Accept ${money(amount, currency)}` : "Accept"}
          </button>
          <button type="button" className="button" disabled={busy} onClick={() => onDecide("reject")}>
            Dismiss
          </button>
        </div>
      )}
      {f.explanation && f.status !== "proposed" && f.action !== "flag" && (
        <details className="finding-letter">
          <summary>What the firm reads</summary>
          <p>{f.explanation}</p>
        </details>
      )}
    </article>
  );
}

function Appeals({ detail, line }: { detail: InvoiceDetail; line: string | null }) {
  const decideAppeal = useDecideAppeal(detail.invoice.number);
  const appeals = detail.appeals.filter((a) => !line || detail.findings.find((f) => f.id === a.finding_id)?.line_no === line);
  if (!appeals.length) return null;
  return (
    <section className="appeals" aria-label="Appeals from the firm">
      <h3>Appeals from the firm</h3>
      {appeals.map((a) => (
        <article key={a.id} className="appeal">
          <p className="appeal-message">{a.message}</p>
          {a.recommendation ? (
            <p className="appeal-rec">
              Suggested: <strong>{a.recommendation.recommendation}</strong>. {a.recommendation.reason}
            </p>
          ) : (
            <p className="appeal-rec">No suggestion: read the appeal and decide.</p>
          )}
          {a.outcome === "open" || a.outcome === "awaiting_decision" ? (
            <div className="finding-actions">
              <button type="button" className="button" disabled={decideAppeal.isPending} onClick={() => decideAppeal.mutate({ id: a.id, decision: "uphold" })}>
                Uphold the adjustment
              </button>
              <button type="button" className="button button-kept" disabled={decideAppeal.isPending}
                      onClick={() => decideAppeal.mutate({ id: a.id, decision: "reverse" })}>
                Reverse it
              </button>
            </div>
          ) : (
            <p className="finding-who">Outcome: {a.outcome}</p>
          )}
        </article>
      ))}
    </section>
  );
}
