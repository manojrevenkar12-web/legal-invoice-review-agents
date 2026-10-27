// The two pieces of ledger vocabulary: an amount that may be struck through and replaced, and the margin mark that
// says who decides a line (code, a person, or still open).

import { motion as m } from "framer-motion";
import { marks, motion } from "../styles/theme";
import { money } from "./format";
import "./ledger.css";

export function Amount({ billed, adjustment, currency }: { billed: string; adjustment: string; currency: string }) {
  const adj = Number(adjustment || 0);
  if (!adj) return <span className="amount">{money(billed, currency)}</span>;
  const paid = Number(billed) + adj;
  return (
    <span className="amount amount-adjusted" aria-label={`${money(billed, currency)} reduced to ${money(paid, currency)}`}>
      <span className="amount-struck">
        {money(billed, currency)}
        <m.span className="amount-strike" initial={{ scaleX: 0 }} animate={{ scaleX: 1 }} transition={{ duration: motion.strike, ease: motion.ease }} />
      </span>
      <span className="amount-paid">{money(paid, currency)}</span>
    </span>
  );
}

export type Decider = "rule" | "person" | "open" | "none";

export function Mark({ who }: { who: Decider }) {
  if (who === "none") return <span className="mark" aria-hidden="true" />;
  const label = { rule: "Applied by the guidelines' rules", person: "Decided by a reviewer", open: "Waiting for a reviewer" }[who];
  return (
    <span className={`mark mark-${who}`} title={label} aria-label={label} role="img">
      {who === "rule" ? marks.rule : who === "person" ? marks.person : marks.open}
    </span>
  );
}
