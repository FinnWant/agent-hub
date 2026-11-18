import { useCallback, useEffect, useState } from "react";
import { api, type PreviewStatus, type Run, type Sandbox } from "../lib/api";
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

      <BranchCard sb={sb} branch={st.branch ?? null} onChanged={load} />

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

/** What /workspace has checked out (so what the preview serves), and a switch to another run's work. */
function BranchCard({ sb, branch, onChanged }: { sb: Sandbox; branch: string | null; onChanged: () => void }) {
  const toast = useToast();
  const [runs, setRuns] = useState<Run[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.runs(sb.id).then(setRuns).catch(() => {});
  }, [sb.id, branch]);
  if (sb.state !== "running") return null;

  const withWork = runs.filter((r) => r.branch && r.commit_sha && !["queued", "running"].includes(r.state));
  const options: { value: string; label: string }[] = [];
  if (sb.base_branch) options.push({ value: sb.base_branch, label: `${sb.base_branch} (base branch)` });
  for (const r of withWork) {
    options.push({ value: r.branch!, label: `${r.id.slice(0, 6)} · ${r.prompt.split("\n")[0].slice(0, 60)}` });
  }
  if (branch && !options.some((o) => o.value === branch)) {
    const r = runs.find((x) => x.branch === branch);
    options.unshift({ value: branch, label: r ? `${r.id.slice(0, 6)} · ${r.prompt.split("\n")[0].slice(0, 60)} (no changes)` : branch });
  }
  const current = runs.find((r) => r.branch === branch);
  const latest = withWork[0];

  const switchTo = async (to: string) => {
    if (!to || to === branch) return;
    setBusy(true);
    try {
      await api.checkout(sb.id, to);
      toast("success", "Switched; reload the preview to see it");
      onChanged();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card checks-setting" style={{ marginTop: 16 }}>
      <div>
        <label htmlFor="preview-branch"><strong>What it shows</strong></label>
        <p>
          The preview serves the files checked out in /workspace
          {branch ? <>: <span className="mono">{branch}</span>{current ? ` (run ${current.id.slice(0, 6)})` : ""}.</> : "."}
          {latest && branch !== latest.branch && (
            <> The latest run with changes is <span className="mono">{latest.id.slice(0, 6)}</span>; switch to it to see its work.</>
          )}
        </p>
      </div>
      <div className="checks-row">
        <select id="preview-branch" className="input" value={branch ?? ""} disabled={busy || options.length === 0}
          onChange={(e) => switchTo(e.target.value)}>
          {!branch && <option value="">(not on a branch)</option>}
          {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>
    </div>
  );
}
