// Loading, failure and empty states. Every screen uses these three, so the console never shows a blank region.

import type { ReactNode } from "react";
import "./states.css";

export function Skeleton({ lines = 6, label = "Loading" }: { lines?: number; label?: string }) {
  return (
    <output className="skeleton" aria-live="polite" aria-busy="true">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="skeleton-bar" style={{ width: `${92 - ((i * 17) % 38)}%` }} />
      ))}
    </output>
  );
}

export function ErrorState({ title, error, onRetry }: { title: string; error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="state state-error" role="alert">
      <h3>{title}</h3>
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state state-empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}
