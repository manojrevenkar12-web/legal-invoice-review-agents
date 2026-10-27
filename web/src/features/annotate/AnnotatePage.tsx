// Human evaluation: grade the pinned sample of findings blind, on the same rubric as the LLM judge. Labels stay in
// this browser until exported as JSONL (evals/annotation/round-N/annotator_<x>.jsonl), as the protocol describes.

import { AnimatePresence, motion as m } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { type AnnotationItem, useAnnotationItems } from "../../shared/api/hooks";
import { motion } from "../../shared/styles/theme";
import { kindLabel } from "../../shared/ui/format";
import { EmptyState, ErrorState, Skeleton } from "../../shared/ui/states";
import "./annotate.css";

const CRITERIA = [
  ["supported", "The cited clause states the rule this finding applies"],
  ["correct", "A careful reviewer applying that clause would decide the same"],
  ["specific", "The explanation is about this entry, not entries in general"],
  ["professional", "The explanation is courteous and factual, fit to send to the firm"],
] as const;
type Grade = Partial<Record<(typeof CRITERIA)[number][0], "pass" | "fail">>;
const STORE = "lira.annotation.round-1";

function load(): Record<string, Grade> {
  try {
    return JSON.parse(localStorage.getItem(STORE) ?? "{}");
  } catch {
    return {};
  }
}

export function AnnotatePage() {
  const q = useAnnotationItems();
  const [grades, setGrades] = useState<Record<string, Grade>>(load);
  const [i, setI] = useState(0);
  useEffect(() => {
    try {
      localStorage.setItem(STORE, JSON.stringify(grades));
    } catch {
      /* not persisted in this browser */
    }
  }, [grades]);
  const done = useMemo(() => Object.values(grades).filter((g) => CRITERIA.every(([c]) => g[c])).length, [grades]);
  if (q.isPending) return <main className="page"><Skeleton lines={10} label="Loading the annotation round" /></main>;
  if (q.isError) return <main className="page"><ErrorState title="The annotation round did not load" error={q.error} onRetry={() => q.refetch()} /></main>;
  if (!q.data.length) return <main className="page"><EmptyState title="No items in this round">Run lira eval to draw the sample.</EmptyState></main>;
  const item = q.data[Math.min(i, q.data.length - 1)];
  const g = grades[item.id] ?? {};
  const set = (c: string, v: "pass" | "fail") => setGrades((all) => ({ ...all, [item.id]: { ...all[item.id], [c]: v } }));
  return (
    <main className="page annotate">
      <header className="page-head">
        <h1>Grade findings</h1>
        <p>
          Round 1, {done} of {q.data.length} graded. You do not see which model or classifier produced a finding, nor its confidence. Grade what is
          in front of you.
        </p>
      </header>
      <AnimatePresence mode="wait">
        <m.article key={item.id} className="annotate-item" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
                   transition={{ duration: motion.base, ease: motion.ease }}>
          <Item item={item} />
          <fieldset className="rubric">
            <legend>Your grade</legend>
            {CRITERIA.map(([c, text]) => (
              <div key={c} className="rubric-row" role="radiogroup" aria-label={text}>
                <span>{text}</span>
                {(["pass", "fail"] as const).map((v) => (
                  <label key={v} className={`rubric-choice${g[c] === v ? " is-on" : ""}`}>
                    <input type="radio" name={`${item.id}-${c}`} checked={g[c] === v} onChange={() => set(c, v)} />
                    {v === "pass" ? "Yes" : "No"}
                  </label>
                ))}
              </div>
            ))}
          </fieldset>
        </m.article>
      </AnimatePresence>
      <nav className="annotate-nav" aria-label="Items">
        <button type="button" className="button" disabled={i === 0} onClick={() => setI(i - 1)}>Previous</button>
        <span>{i + 1} of {q.data.length}</span>
        <button type="button" className="button button-primary" disabled={i >= q.data.length - 1} onClick={() => setI(i + 1)}>Next</button>
        <button type="button" className="button" disabled={done === 0} onClick={() => exportLabels(q.data, grades)}>Export my labels</button>
      </nav>
    </main>
  );
}

function Item({ item }: { item: AnnotationItem }) {
  return (
    <div className="annotate-case">
      <p className="annotate-kind">{kindLabel(item.kind)}: {item.action === "reject" ? "line not payable" : item.action === "reduce" ? "line reduced" : "flag"}</p>
      <blockquote className="clause"><p>{item.clause_text}</p><footer>Clause {item.clause_id}</footer></blockquote>
      <p className="annotate-entry">
        Line {item.line_no}, {item.timekeeper_role}, {item.hours} h: <span>{item.description}</span>
      </p>
      <p className="annotate-letter">{item.explanation}</p>
    </div>
  );
}

function exportLabels(items: AnnotationItem[], grades: Record<string, Grade>) {
  const rows = items
    .filter((it) => CRITERIA.every(([c]) => grades[it.id]?.[c]))
    .map((it) => {
      const g = grades[it.id];
      const overall = g.supported === "pass" && g.correct === "pass" ? "pass" : "fail";
      return JSON.stringify({ id: it.id, ...g, overall });
    });
  const url = URL.createObjectURL(new Blob([rows.join("\n") + "\n"], { type: "application/x-ndjson" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "annotator_labels.jsonl";
  a.click();
  URL.revokeObjectURL(url);
}
