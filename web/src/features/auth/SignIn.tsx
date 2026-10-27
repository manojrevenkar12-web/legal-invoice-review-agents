import { useState } from "react";
import { DEMO_KEYS, session } from "./session";
import "./signin.css";

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [key, setKey] = useState("");
  const use = (k: string, label: string) => {
    session.set({ key: k, label });
    onSignedIn();
  };
  return (
    <main className="signin">
      <div className="signin-intro">
        <h1>Outside-counsel invoices, reviewed against your own guidelines</h1>
        <p>
          Rules a person approved settle what can be measured. A model reads the rest and cites the clause it relies on; you decide every
          line it proposes.
        </p>
      </div>
      <section className="signin-panel" aria-labelledby="demo-keys">
        <h2 id="demo-keys">Sign in</h2>
        <ul className="signin-list">
          {DEMO_KEYS.map((d) => (
            <li key={d.key}>
              <button type="button" className="signin-choice" onClick={() => use(d.key, d.label)}>
                <span>{d.label}</span>
                <small>{d.hint}</small>
              </button>
            </li>
          ))}
        </ul>
        <form
          className="signin-key"
          onSubmit={(e) => {
            e.preventDefault();
            if (key.trim()) use(key.trim(), "API key");
          }}
        >
          <label htmlFor="api-key">Or use an API key</label>
          <div className="signin-row">
            <input id="api-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
            <button type="submit" className="button button-primary" disabled={!key.trim()}>
              Sign in
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
