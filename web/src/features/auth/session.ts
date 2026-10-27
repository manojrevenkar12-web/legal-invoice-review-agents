// The signed-in key, kept per browser tab. Storage can be unavailable (private mode): fall back to memory.

export interface Session {
  key: string;
  label: string;
}

let memory: Session | null = null;
const KEY = "lira.session";

export const session = {
  get(): Session | null {
    try {
      const raw = sessionStorage.getItem(KEY);
      return raw ? (JSON.parse(raw) as Session) : memory;
    } catch {
      return memory;
    }
  },
  set(s: Session | null) {
    memory = s;
    try {
      if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
      else sessionStorage.removeItem(KEY);
    } catch {
      /* memory only */
    }
  },
};

/** Demo keys created by `lira demo` (never present in a real deployment). */
export const DEMO_KEYS: { label: string; key: string; hint: string }[] = [
  { label: "Reviewer, Kestrel Logistics", key: "demo-reviewer-kestrel", hint: "EUR, guidelines v1 and v2" },
  { label: "Reviewer, Brightwater Insurance", key: "demo-reviewer-brightwater", hint: "GBP" },
  { label: "Reviewer, Corvane Pharmaceuticals", key: "demo-reviewer-corvane", hint: "USD" },
  { label: "Legal operations, Kestrel", key: "demo-legalops-kestrel", hint: "approves guidelines" },
  { label: "Okafor Brandt LLP for Kestrel", key: "demo-firm-okafor-kestrel", hint: "a law firm: sees outcomes, appeals" },
];
