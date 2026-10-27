// The reviewer's queue: invoices that wait for a decision, ranked by what is at stake; settled ones below.

import { NavLink } from "react-router-dom";
import { useQueue } from "../../shared/api/hooks";
import type { QueueItem } from "../../shared/api/types";
import { money, month } from "../../shared/ui/format";
import { Mark } from "../../shared/ui/Ledger";
import { EmptyState, ErrorState, Skeleton } from "../../shared/ui/states";
import "./queue.css";

export function QueueRail({ currency }: { currency: string }) {
  const q = useQueue();
  if (q.isPending) return <aside className="queue"><Skeleton lines={10} label="Loading invoices" /></aside>;
  if (q.isError) return <aside className="queue"><ErrorState title="The queue did not load" error={q.error} onRetry={() => q.refetch()} /></aside>;
  const open = q.data.filter((i) => i.status === "awaiting_review");
  const rest = q.data.filter((i) => i.status !== "awaiting_review");
  if (!q.data.length) {
    return (
      <aside className="queue">
        <EmptyState title="No invoices yet">Invoices appear here as firms submit LEDES files.</EmptyState>
      </aside>
    );
  }
  return (
    <aside className="queue" aria-label="Invoices">
      <Section title={`Waiting for you (${open.length})`} items={open} currency={currency} empty="Nothing waits for a decision." />
      <Section title={`Settled and in review (${rest.length})`} items={rest} currency={currency} />
    </aside>
  );
}

function Section({ title, items, currency, empty }: { title: string; items: QueueItem[]; currency: string; empty?: string }) {
  return (
    <section className="queue-section">
      <h2 className="queue-heading">{title}</h2>
      {items.length === 0 && empty ? <p className="queue-empty">{empty}</p> : null}
      <ol className="queue-list">
        {items.map((i) => (
          <li key={i.number}>
            <NavLink to={`/review/${encodeURIComponent(i.number)}`} className={({ isActive }) => `queue-item${isActive ? " is-active" : ""}`}>
              <Mark who={i.status === "awaiting_review" ? "open" : i.status === "settled" ? "person" : "none"} />
              <span className="queue-main">
                <span className="queue-firm">{i.firm_name}</span>
                <span className="queue-meta">{month(i.period_start)}</span>
              </span>
              <span className="queue-figures">
                {i.status === "awaiting_review" ? (
                  <>
                    <span className="queue-stake">{money(i.at_stake, currency)}</span>
                    <span className="queue-count">{i.pending} open</span>
                  </>
                ) : (
                  <span className="queue-adjusted">{Number(i.total_adjustment ?? 0) ? money(i.total_adjustment, currency) : "as billed"}</span>
                )}
                {i.degraded && <span className="queue-flag">without model</span>}
              </span>
            </NavLink>
          </li>
        ))}
      </ol>
    </section>
  );
}
