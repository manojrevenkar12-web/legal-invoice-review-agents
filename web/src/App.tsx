import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { AnnotatePage } from "./features/annotate/AnnotatePage";
import { session } from "./features/auth/session";
import { SignIn } from "./features/auth/SignIn";
import { EvaluationPage } from "./features/evaluation/EvaluationPage";
import { FirmPage } from "./features/firm/FirmPage";
import { GuidelinesPage } from "./features/guidelines/GuidelinesPage";
import { Workstation } from "./features/invoice/Workstation";
import { useMe } from "./shared/api/hooks";
import { ErrorState, Skeleton } from "./shared/ui/states";
import "./app.css";

export function App({ client }: { client?: QueryClient }) {
  const [qc] = useState(() => client ?? new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } }));
  const [signedIn, setSignedIn] = useState(Boolean(session.get()));
  return (
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        {signedIn ? (
          <Shell onSignOut={() => { session.set(null); qc.clear(); setSignedIn(false); }} />
        ) : (
          <SignIn onSignedIn={() => setSignedIn(true)} />
        )}
      </BrowserRouter>
    </QueryClientProvider>
  );
}

function Shell({ onSignOut }: { onSignOut: () => void }) {
  const me = useMe();
  if (me.isPending) return <Skeleton lines={4} label="Signing in" />;
  if (me.isError) {
    return (
      <main className="page">
        <ErrorState title="This key was not accepted" error={me.error} onRetry={onSignOut} />
      </main>
    );
  }
  const role = me.data.role;
  const home = role === "firm" ? "/firm" : role === "legal_ops" ? "/guidelines" : "/review";
  const nav =
    role === "firm"
      ? [["/firm", "Invoices"]]
      : role === "legal_ops"
        ? [["/guidelines", "Guidelines"], ["/evaluation", "Evaluation"]]
        : [["/review", "Review"], ["/guidelines", "Guidelines"], ["/evaluation", "Evaluation"], ["/annotate", "Grade findings"]];
  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">
          <strong>{me.data.tenant_name}</strong>
          <span>Invoice review</span>
        </span>
        <nav aria-label="Sections" className="topnav">
          {nav.map(([to, label]) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "is-active" : "")}>
              {label}
            </NavLink>
          ))}
        </nav>
        <span className="who">
          {me.data.name}
          <button type="button" className="link" onClick={onSignOut}>Sign out</button>
        </span>
      </header>
      <div className="content">
        <Routes>
          <Route path="/" element={<Navigate to={home} replace />} />
          <Route path="/review" element={<Workstation />} />
          <Route path="/review/:number" element={<Workstation />} />
          <Route path="/guidelines" element={<GuidelinesPage />} />
          <Route path="/evaluation" element={<EvaluationPage />} />
          <Route path="/annotate" element={<AnnotatePage />} />
          <Route path="/firm" element={<FirmPage />} />
          <Route path="/firm/:number" element={<FirmPage />} />
          <Route path="*" element={<Navigate to={home} replace />} />
        </Routes>
      </div>
    </div>
  );
}
