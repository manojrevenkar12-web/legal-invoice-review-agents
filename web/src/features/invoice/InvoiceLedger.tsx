// One invoice as a reviewer's ledger: every line with its margin mark, its amount struck and adjusted where a
// finding applies, and the findings named under the entry. Selecting a line opens it in the inspector.

import { motion as m } from "framer-motion";
import { useMemo } from "react";
import type { Finding, InvoiceDetail, Line } from "../../shared/api/types";
import { motion } from "../../shared/styles/theme";
import { dayShort, kindLabel, money, month, STATUS } from "../../shared/ui/format";
import { Amount, type Decider, Mark } from "../../shared/ui/Ledger";
import "./invoice.css";

export function decider(findings: Finding[]): Decider {
  const active = findings.filter((f) => f.action !== "flag" || f.status === "proposed");
  if (active.some((f) => f.status === "proposed")) return "open";
  if (active.some((f) => f.status === "accepted" || f.status === "rejected")) return "person";
  if (active.some((f) => f.status === "auto_applied")) return "rule";
  return "none";
}

export function InvoiceLedger({ detail, selected, onSelect }: { detail: InvoiceDetail; selected: string | null; onSelect: (line: string | null) => void }) {
  const byLine = useMemo(() => {
    const out = new Map<string, Finding[]>();
    for (const f of detail.findings) if (f.line_no) out.set(f.line_no, [...(out.get(f.line_no) ?? []), f]);
    return out;
  }, [detail.findings]);
  const inv = detail.invoice;
  const adjusted = Number(inv.total_adjustment ?? 0);
  const atStake = detail.findings.filter((f) => f.status === "proposed").reduce((s, f) => s - Number(f.adjustment), 0);
  return (
    <section className="ledger" aria-label={`Invoice ${inv.number}`}>
      <header className="ledger-head">
        <div className="ledger-title">
          <h1>{detail.firm_name}</h1>
          <p>
            {detail.matter}, {month(inv.period_start)}. Invoice {inv.number}. {STATUS[inv.status] ?? inv.status}
            {inv.degraded ? ". Reviewed without the language model: every judgement was left to you." : "."}
          </p>
        </div>
        <dl className="ledger-sums">
          <div>
            <dt>Billed</dt>
            <dd>{money(inv.total, detail.currency)}</dd>
          </div>
          <div>
            <dt>Adjusted so far</dt>
            <dd className={adjusted ? "is-deduct" : ""}>{money(adjusted, detail.currency)}</dd>
          </div>
          <div>
            <dt>Open with you</dt>
            <dd className="is-person">{money(-atStake, detail.currency)}</dd>
          </div>
        </dl>
      </header>
      <InvoiceLevel findings={detail.findings.filter((f) => !f.line_no && f.action === "flag")} />
      <div className="ledger-table" role="table" aria-label="Invoice lines">
        <div className="ledger-row ledger-columns" role="row">
          <span role="columnheader" aria-label="Decided by" />
          <span role="columnheader">Line</span>
          <span role="columnheader">Timekeeper</span>
          <span role="columnheader">Entry</span>
          <span role="columnheader" className="num">Amount</span>
        </div>
        {detail.lines.map((li, i) => (
          <Row key={li.line_no} line={li} findings={byLine.get(li.line_no) ?? []} index={i} currency={detail.currency}
               selected={selected === li.line_no} onSelect={() => onSelect(selected === li.line_no ? null : li.line_no)} />
        ))}
      </div>
    </section>
  );
}

function Row({ line, findings, index, currency, selected, onSelect }: {
  line: Line; findings: Finding[]; index: number; currency: string; selected: boolean; onSelect: () => void;
}) {
  const shown = findings.filter((f) => f.status !== "rejected" || f.decided_by === "person");
  return (
    <m.div
      role="row"
      tabIndex={0}
      aria-selected={selected}
      className={`ledger-row${selected ? " is-selected" : ""}${findings.some((f) => f.status === "proposed") ? " is-open" : ""}`}
      onClick={onSelect}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect())}
      initial={{ opacity: 0, y: motion.rowRise }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index, 40) * motion.rowStagger, duration: motion.base, ease: motion.ease }}
    >
      <span role="cell"><Mark who={decider(findings)} /></span>
      <span role="cell" className="ledger-line">
        {line.line_no}
        <small>{dayShort(line.date)}</small>
      </span>
      <span role="cell" className="ledger-tk">
        {line.type === "E" ? <em>Expense {line.expense_code}</em> : line.timekeeper_name}
        <small>{line.type === "E" ? "" : `${line.timekeeper_class} ${line.task_code}/${line.activity_code}`}</small>
      </span>
      <span role="cell" className="ledger-entry">
        {line.description}
        {shown.length > 0 && (
          <span className="ledger-findings">
            {shown.map((f) => (
              <span key={f.id} className={`finding-tag status-${f.status}`}>{kindLabel(f.kind)}</span>
            ))}
          </span>
        )}
      </span>
      <span role="cell" className="num ledger-amount">
        <Amount billed={line.total} adjustment={line.adjustment} currency={currency} />
        <small>{line.type === "E" ? `${line.units} at ${line.unit_cost}` : `${line.units} h at ${line.unit_cost}`}</small>
      </span>
    </m.div>
  );
}

function InvoiceLevel({ findings }: { findings: Finding[] }) {
  if (!findings.length) return null;
  return (
    <ul className="ledger-notes" aria-label="Invoice-level findings">
      {findings.map((f) => (
        <li key={f.id} className={`status-${f.status}`}>
          <strong>{kindLabel(f.kind)}.</strong> {f.rationale}
        </li>
      ))}
    </ul>
  );
}
