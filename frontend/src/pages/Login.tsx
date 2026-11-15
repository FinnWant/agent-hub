import { useState, type FormEvent } from "react";
import { api, ApiError } from "../lib/api";
import { useTitle } from "../lib/router";

export default function Login({
  unconfigured,
  demo,
  onLoggedIn,
}: {
  unconfigured: boolean;
  demo: boolean;
  onLoggedIn: () => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useTitle("Log in");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      onLoggedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server");
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          <span>Kiln</span>
        </div>
        <p>Sandboxed coding agents on your server.</p>
        {unconfigured ? (
          <div className="banner banner-warning">
            <div>
              <strong>No admin password is set.</strong> On the server, run
              <div className="mono" style={{ marginTop: 6, fontSize: 12 }}>python -m app.set_password</div>
            </div>
          </div>
        ) : (
          <>
            <div className="field">
              <label htmlFor="pw">Password</label>
              <input id="pw" className="input" type="password" autoComplete="current-password" autoFocus
                value={password} onChange={(e) => setPassword(e.target.value)}
                aria-invalid={!!error} aria-describedby={error ? "pw-error" : undefined} />
              {error && <span className="error" id="pw-error" role="alert">{error}</span>}
              {demo && !error && <span className="hint">Demo mode: the password is <span className="mono">demo</span>.</span>}
            </div>
            <button className="btn btn-primary" type="submit" disabled={busy || !password}>
              {busy ? "Signing in…" : "Log in"}
            </button>
          </>
        )}
      </form>
    </div>
  );
}
