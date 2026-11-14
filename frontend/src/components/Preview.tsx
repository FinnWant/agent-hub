import { useCallback, useEffect, useState } from "react";
import { api, type PreviewStatus, type Sandbox } from "../lib/api";
import { Icon, useToast } from "./ui";

const HINTS: Record<string, { label: string; cmd: string }[]> = {
  python: [
    { label: "Flask", cmd: "flask run --host 0.0.0.0 --port 8000" },
    { label: "Django", cmd: "python manage.py runserver 0.0.0.0:8000" },
    { label: "Any folder", cmd: "python3 -m http.server 8000 --bind 0.0.0.0" },
  ],
  node: [
    { label: "Vite", cmd: "npm run dev -- --host 0.0.0.0 --port 5173" },
    { label: "Next.js", cmd: "npx next dev -H 0.0.0.0 -p 5173" },
  ],
  static: [{ label: "Any folder", cmd: "busybox httpd -f -p 0.0.0.0:3000 -h /workspace" }],
};

export default function Preview({ sb }: { sb: Sandbox }) {
  const [st, setSt] = useState<PreviewStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setSt(await api.preview(sb.id));
    } catch (e) {
      toast("error", (e as Error).message);
    }
  }, [sb.id, toast]);

  useEffect(() => {
    load();
    const t = setInterval(() => document.visibilityState === "visible" && load(), 5000);
    return () => clearInterval(t);
  }, [load]);

  const toggleStatic = async () => {
    setBusy(true);
    try {
      setSt(await (st?.static_server ? api.stopStatic(sb.id) : api.startStatic(sb.id)));
      setTimeout(load, 600);
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!st) return <div className="card" style={{ height: 160 }} aria-busy="true" />;

  if (!st.enabled || !st.url) {
    return (
      <div className="card empty">
        <h2>No preview for this sandbox</h2>
        <p>
          {!st.enabled
            ? "Previews are off: PREVIEW_BIND and PREVIEW_HOST aren't set on the server (install.sh fills them in)."
            : "It was created before previews were set up, or the preview port pool was full. Create a new sandbox to get one."}
        </p>
      </div>
    );
  }

  const state = st.listening;
  const live = state === "any";

  return (
    <div className="preview" style={{ maxWidth: 760 }}>
      <div className="card preview-card">
        <div className="preview-status">
          <span className={`dot ${live ? "dot-running" : state === "loopback" ? "run-dot-timed_out" : "dot-stopped"}`} aria-hidden />
          <div>
            <strong>
              {state === "stopped" ? "Sandbox is stopped"
                : live ? `Something is serving on port ${st.port}`
                : state === "loopback" ? `Port ${st.port} only listens on 127.0.0.1`
                : `Nothing is listening on port ${st.port}`}
            </strong>
            <p>
              {live ? "Open it from any device on your tailnet."
                : state === "loopback" ? "Restart the server bound to 0.0.0.0 so it can be reached from outside the sandbox."
                : `Start a dev server on 0.0.0.0:${st.port} in the terminal, ask the agent to, or serve /workspace as static files.`}
            </p>
          </div>
        </div>
        <div className="preview-url">
          <a className="mono" href={st.url} target="_blank" rel="noopener noreferrer">{st.url}</a>
          <div className="preview-actions">
            <button className="btn btn-sm" onClick={() => navigator.clipboard?.writeText(st.url!).then(() => toast("success", "Link copied"))}>
              Copy
            </button>
            <a className={`btn btn-sm ${live ? "btn-primary" : ""}`} href={st.url} target="_blank" rel="noopener noreferrer">
              Open <Icon name="external" size={13} />
            </a>
          </div>
        </div>
      </div>

      {st.static_server && !st.has_index && !st.lists_directories && (
        <div className="banner banner-warning" style={{ marginTop: 16 }}>
          <div>
            <strong>There's no index.html in /workspace</strong>, so the link opens a 404. Add a file path to the
            link (for example <a className="mono" href={`${st.url}README.md`} target="_blank" rel="noopener noreferrer">/README.md</a>)
            or ask the agent to build a page.
          </div>
        </div>
      )}

      <div className="card toggle-row" style={{ marginTop: 16 }}>
        <div>
          <strong id="static-label">Serve /workspace as static files</strong>
          <p>
            {st.lists_directories
              ? `Python's file server on port ${st.port}: opens index.html if there is one, otherwise lists the files.`
              : `A small file server on port ${st.port}: serves files by path and opens index.html at /.`}{" "}
            Stop it before starting your own dev server on the same port.
          </p>
        </div>
        <button className="toggle" role="switch" aria-checked={!!st.static_server} aria-labelledby="static-label"
          disabled={busy || state === "stopped" || (live && !st.static_server)} onClick={toggleStatic} />
      </div>

      <div className="card" style={{ marginTop: 16, padding: "16px 20px" }}>
        <strong>Start your own dev server</strong>
        <p className="muted" style={{ margin: "4px 0 12px" }}>In the Terminal tab, bind to 0.0.0.0 on port {st.port}:</p>
        <dl className="hints">
          {(HINTS[sb.template] ?? []).map((h) => (
            <div key={h.label}>
              <dt>{h.label}</dt>
              <dd className="mono">{h.cmd}</dd>
            </div>
          ))}
        </dl>
        <p className="muted" style={{ margin: "12px 0 0", fontSize: 12 }}>
          Previews are reachable only over Tailscale. Your ACL needs <span className="mono">tcp:8200-8299</span> for this server.
        </p>
      </div>
    </div>
  );
}
