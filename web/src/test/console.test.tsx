import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AnnotatePage } from "../features/annotate/AnnotatePage";
import { session } from "../features/auth/session";
import { SignIn } from "../features/auth/SignIn";
import { EvaluationPage } from "../features/evaluation/EvaluationPage";
import { describe as describeRule } from "../features/guidelines/GuidelinesPage";
import { FindingInspector } from "../features/invoice/FindingInspector";
import { decider, InvoiceLedger } from "../features/invoice/InvoiceLedger";
import { QueueRail } from "../features/queue/QueueRail";
import { invoice, mockFetch, queue } from "./fixtures";

function wrap(ui: ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="*" element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => session.set({ key: "demo-reviewer-kestrel", label: "test" }));
afterEach(() => vi.unstubAllGlobals());

describe("queue", () => {
  it("shows a skeleton, then open invoices first with the money at stake", async () => {
    mockFetch({ "/v1/invoices": queue });
    wrap(<QueueRail currency="EUR" />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(await screen.findByText("Okafor Brandt LLP")).toBeInTheDocument();
    expect(screen.getByText("Waiting for you (1)")).toBeInTheDocument();
    expect(screen.getByText("€583.50")).toBeInTheDocument();
    expect(screen.getByText("without model")).toBeInTheDocument();
  });

  it("offers a retry when the queue fails, and an empty state when there is nothing", async () => {
    let fail = true;
    mockFetch({ "/v1/invoices": () => (fail ? new Response(JSON.stringify({ detail: "database unavailable" }), { status: 503 }) : []) });
    wrap(<QueueRail currency="EUR" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No invoices yet")).toBeInTheDocument();
  });
});

describe("ledger and inspector", () => {
  it("strikes adjusted amounts and marks who decides each line", () => {
    wrap(<InvoiceLedger detail={invoice} selected={null} onSelect={() => undefined} />);
    expect(screen.getByLabelText("€210.00 reduced to €0.00")).toBeInTheDocument();
    expect(screen.getByLabelText("Waiting for a reviewer")).toBeInTheDocument();
    expect(screen.getByLabelText("Applied by the guidelines' rules")).toBeInTheDocument();
    expect(decider([])).toBe("none");
  });

  it("sends the reviewer's decision for one finding", async () => {
    const calls = mockFetch({ "/v1/invoices/OKA-2609-0202/decisions": { status: "settled", total_adjustment: "-474.00", pending: 0 } });
    wrap(<FindingInspector detail={invoice} line="2" />);
    expect(screen.getByText("Administrative and clerical work is overhead.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept -€264.00" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ decisions: { "OKA-2609-0202:2:clerical_work": { decision: "accept" } } });
    expect(calls[0].init?.headers).toMatchObject({ authorization: "Bearer demo-reviewer-kestrel" });
  });

  it("says when a line has nothing to decide", () => {
    wrap(<FindingInspector detail={{ ...invoice, findings: [] }} line="2" />);
    expect(screen.getByText("Nothing to decide on this line")).toBeInTheDocument();
  });
});

describe("guidelines, evaluation, annotation, sign-in", () => {
  it("describes compiled rules in a reviewer's words", () => {
    expect(describeRule({ kind: "travel_time", clause_id: "5.1", quote: "", rate_factor: "0" })).toBe("Travel time is not billable");
    expect(describeRule({ kind: "block_billing", clause_id: "3.2", quote: "", action: "reduce", reduce_factor: "0.5" })).toBe(
      "Block-billed entries reduced by 50%");
    expect(describeRule({ kind: "max_daily_hours", clause_id: "3.4", quote: "", hours: "10.0" })).toBe("At most 10 h per timekeeper per day");
  });

  it("explains how to produce the evaluation when there is none", async () => {
    mockFetch({});
    wrap(<EvaluationPage />);
    expect(await screen.findByText("No evaluation yet")).toBeInTheDocument();
  });

  it("lets an annotator grade an item blind and export labels", async () => {
    mockFetch({ "/v1/annotation/items": [{ id: "a1", kind: "clerical_work", action: "reject", clause_id: "4.1", clause_text: "Clerical work is overhead.",
      line_no: "2", timekeeper_role: "LA", hours: "1.20", description: "Bates label production documents", explanation: "Line 2 describes administrative work." }] });
    wrap(<AnnotatePage />);
    expect(await screen.findByText("Clerical work is overhead.")).toBeInTheDocument();
    const exportButton = screen.getByRole("button", { name: "Export my labels" });
    expect(exportButton).toBeDisabled();
    for (const group of screen.getAllByRole("radiogroup")) await userEvent.click(group.querySelectorAll("input")[0]);
    expect(screen.getByText(/1 of 1 graded/)).toBeInTheDocument();
    expect(exportButton).toBeEnabled();
  });

  it("signs in with a demo key", async () => {
    const done = vi.fn();
    render(<SignIn onSignedIn={done} />);
    await userEvent.click(screen.getByRole("button", { name: /Reviewer, Brightwater/ }));
    expect(session.get()?.key).toBe("demo-reviewer-brightwater");
    expect(done).toHaveBeenCalled();
  });
});
