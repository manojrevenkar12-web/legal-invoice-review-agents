// Questions about the guidelines, answered only from retrieved clauses. An answer whose citations or numbers cannot
// be checked against those clauses is replaced by "no supported answer" plus the clauses themselves.

import { useState } from "react";
import { useAsk } from "../../shared/api/hooks";
import { ErrorState } from "../../shared/ui/states";
import "./ask.css";

export function AskPanel() {
  const ask = useAsk();
  const [q, setQ] = useState("");
  return (
    <section className="ask" aria-labelledby="ask-title">
      <h2 id="ask-title">Ask the guidelines</h2>
      <form
        className="ask-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (q.trim().length >= 5) ask.mutate(q.trim());
        }}
      >
        <label htmlFor="ask-q" className="visually-hidden">Question</label>
        <input id="ask-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Can we bill for travel to a deposition?" />
        <button type="submit" className="button button-primary" disabled={ask.isPending || q.trim().length < 5}>
          {ask.isPending ? "Looking" : "Ask"}
        </button>
      </form>
      {ask.isError && <ErrorState title="No answer" error={ask.error} onRetry={() => ask.mutate(q.trim())} />}
      {ask.data && (
        <div className={`ask-answer${ask.data.answerable && ask.data.verified ? "" : " is-abstain"}`} aria-live="polite">
          <p className="ask-text">{ask.data.answer}</p>
          <p className="ask-meta">
            {ask.data.answerable && ask.data.verified
              ? `Cites ${ask.data.citations.map((c) => `clause ${c}`).join(", ")}; checked against the retrieved clauses.`
              : `Closest clauses: ${ask.data.retrieved.join(", ") || "none"}.`}
          </p>
        </div>
      )}
    </section>
  );
}
