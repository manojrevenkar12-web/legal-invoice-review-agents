// What a law firm sees: its invoices, the outcome of each line with the explanation it can act on, and an appeal
// form for any adjustment. It never sees internal evidence, model notes or other firms.

import { useState } from "react";
import { NavLink, useParams } from "react-router-dom";
import { useAppeal, useInvoice, useMe, useQueue } from "../../shared/api/hooks";
import type { Finding, InvoiceDetail } from "../../shared/api/types";
import { FIRM_STATUS as STATUS, kindLabel, money, month } from "../../shared/ui/format";
import { Amount } from "../../shared/ui/Ledger";
import { EmptyState, ErrorState, Skeleton } from "../../shared/ui/states";
import "./firm.css";

export function FirmPage() {
  const { number } = useParams();
  const me = useMe();
  const list = useQueue();
  const currency = me.data?.currency ?? "EUR";
  return (
    <main className="page firm">
      <header className="page-head">
        <h1>Your invoices to {me.data?.tenant_name}</h1>
        <p>Adjusted lines show the client's clause and the reason. If a reason is wrong, appeal it here; a reviewer decides.</p>
      </header>
      <div className="firm-grid">
        <nav aria-label="Invoices" className="firm-list">
          {list.isPending ? <Skeleton lines={6} /> : list.isError ? <ErrorState title="Invoices did not load" error={list.error} onRetry={() => list.refetch()} /> :
            list.data.length === 0 ? <EmptyState title="No invoices yet">Submitted invoices appear here.</EmptyState> :
              list.data.map((i) => (
                <NavLink key={i.number} to={`/firm/${encodeURIComponent(i.number)}`} className={({ isActive }) => `firm-item${isActive ? " is-active" : ""}`}>
                  <span>{month(i.period_start)}</span>
                  <small>{i.number}, {STATUS[i.status] ?? i.status}</small>
                </NavLink>
              ))}
        </nav>
        {number ? <FirmInvoice number={number} currency={currency} /> : <EmptyState title="Choose an invoice" />}
      </div>
    </main>
  );
}

function FirmInvoice({ number, currency }: { number: string; currency: string }) {
  const q = useInvoice(number);
  if (q.isPending) return <Skeleton lines={10} />;
  if (q.isError) return <ErrorState title="The invoice did not load" error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  const adjusted = d.findings.filter((f) => f.line_no && Number(f.adjustment) !== 0);
  return (
    <section className="firm-invoice">
      <h2>{d.matter}</h2>
      <p>
        Billed {money(d.invoice.total, currency)}, adjusted {money(d.invoice.total_adjustment ?? 0, currency)}. {STATUS[d.invoice.status] ?? d.invoice.status}.
      </p>
      {adjusted.length === 0 ? (
        <EmptyState title="No adjustments so far">Lines are paid as billed unless a reviewer decides otherwise.</EmptyState>
      ) : (
        <ol className="firm-adjustments">
          {adjusted.map((f) => <Adjusted key={f.id} f={f} detail={d} currency={currency} />)}
        </ol>
      )}
    </section>
  );
}

function Adjusted({ f, detail, currency }: { f: Finding; detail: InvoiceDetail; currency: string }) {
  const appeal = useAppeal(detail.invoice.number);
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState("");
  const line = detail.lines.find((l) => l.line_no === f.line_no);
  const filed = detail.appeals?.some((a) => a.finding_id === f.id);
  return (
    <li className="firm-adjustment">
      <header>
        <strong>Line {f.line_no}: {kindLabel(f.kind)}</strong>
        {line && <Amount billed={line.total} adjustment={line.adjustment} currency={currency} />}
      </header>
      <p className="firm-explanation">{f.explanation}</p>
      {filed || appeal.isSuccess ? (
        <p className="firm-filed">Appeal received. A reviewer will decide.{appeal.data?.reply ? ` ${appeal.data.reply}` : ""}</p>
      ) : open ? (
        <form className="firm-appeal" onSubmit={(e) => { e.preventDefault(); appeal.mutate({ finding_id: f.id, message: msg }); }}>
          <label htmlFor={`appeal-${f.id}`}>Why is this adjustment wrong?</label>
          <textarea id={`appeal-${f.id}`} rows={3} value={msg} onChange={(e) => setMsg(e.target.value)} />
          <div className="finding-actions">
            <button type="submit" className="button button-primary" disabled={msg.trim().length < 5 || appeal.isPending}>Send appeal</button>
            <button type="button" className="button" onClick={() => setOpen(false)}>Cancel</button>
          </div>
          {appeal.isError && <ErrorState title="The appeal was not sent" error={appeal.error} />}
        </form>
      ) : (
        <button type="button" className="button" onClick={() => setOpen(true)}>Appeal this adjustment</button>
      )}
    </li>
  );
}
