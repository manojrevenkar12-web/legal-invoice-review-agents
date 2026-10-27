import { useState } from "react";
import { useParams } from "react-router-dom";
import { useInvoice, useMe } from "../../shared/api/hooks";
import { EmptyState, ErrorState, Skeleton } from "../../shared/ui/states";
import { QueueRail } from "../queue/QueueRail";
import { FindingInspector } from "./FindingInspector";
import { InvoiceLedger } from "./InvoiceLedger";
import "./invoice.css";

export function Workstation() {
  const { number } = useParams();
  const me = useMe();
  const inv = useInvoice(number);
  const [line, setLine] = useState<string | null>(null);
  const currency = me.data?.currency ?? "EUR";
  return (
    <div className="workstation">
      <QueueRail currency={currency} />
      {!number ? (
        <section className="ledger">
          <EmptyState title="Choose an invoice">The queue lists invoices with open findings first, the most money at stake on top.</EmptyState>
        </section>
      ) : inv.isPending ? (
        <section className="ledger"><Skeleton lines={14} label="Loading the invoice" /></section>
      ) : inv.isError ? (
        <section className="ledger"><ErrorState title="The invoice did not load" error={inv.error} onRetry={() => inv.refetch()} /></section>
      ) : (
        <>
          <InvoiceLedger key={number} detail={inv.data} selected={line} onSelect={setLine} />
          <FindingInspector detail={inv.data} line={line} />
        </>
      )}
    </div>
  );
}
