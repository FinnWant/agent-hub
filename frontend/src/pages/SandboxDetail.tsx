import { useState } from "react";
import { api, ApiError, type Sandbox } from "../lib/api";
import { ago, bytes, repoLabel } from "../lib/format";
import { navigate, useTitle } from "../lib/router";
import { usePoll } from "../lib/usePoll";
import { Dialog, Icon, StatusBadge, useToast } from "../components/ui";
import Terminal from "../components/Terminal";

type Tab = "terminal" | "settings";
const LATER: { label: string; phase: number }[] = [
  { label: "Runs", phase: 3 },
  { label: "Files", phase: 4 },
  { label: "Changes", phase: 4 },
  { label: "Preview", phase: 4 },
];

export default function SandboxDetail({ id }: { id: string }) {
  const { data: sb, error, reload } = usePoll(() => api.sandbox(id), 5000, [id]);
  const [tab, setTab] = useState<Tab>("terminal");
  const [busy, setBusy] = useState(false);
  const [destroying, setDestroying] = useState(false);
  const toast = useToast();
  useTitle(sb?.name ?? "Sandbox");

  if (!sb) {
    return (
      <div className="page">
        {error instanceof ApiError && error.status === 404 ? (
          <div className="empty card">
            <h2>Sandbox not found</h2>
            <p>It may have been destroyed.</p>
            <a className="btn" href="/">Back to sandboxes</a>
          </div>
        ) : error ? (
          <div className="banner banner-danger"><strong>Couldn't load it.</strong> {error.message}</div>
        ) : null}
      </div>
    );
  }

  const act = async (what: "start" | "stop") => {
    setBusy(true);
    try {
      await api[what](sb.id);
    } catch (e) {
      const err = e as ApiError;
      toast("error", err.reasons?.length ? err.reasons.join(" ") : err.message);
    } finally {
      setBusy(false);
      reload();
    }
  };

  const repo = repoLabel(sb.repo_url, sb.base_branch);
  const running = sb.state === "running";

  return (
    <div className="page page-wide">
      <nav className="crumbs" aria-label="Breadcrumb">
        <a href="/"><Icon name="back" size={14} /> Sandboxes</a>
      </nav>
      <div className="detail-head">
        <h1>{sb.name}</h1>
        <StatusBadge state={sb.state} />
        <div className="actions">
          {running ? (
            <button className="btn" disabled={busy} onClick={() => act("stop")}>
              <Icon name="stop" /> Stop
            </button>
          ) : (
            <button className="btn" disabled={busy} onClick={() => act("start")}>
              <Icon name="play" /> Start
            </button>
          )}
        </div>
      </div>
      <div className="detail-meta">
        <span>{sb.template}</span>
        <span>{bytes(sb.memory)} · {sb.cpus} CPU</span>
        {repo && <span>{repo}</span>}
        <span>created {ago(sb.created_at)}</span>
        <span>id {sb.id}</span>
      </div>
      {sb.state === "error" && sb.error && (
        <div className="banner banner-danger" style={{ marginTop: 16 }}>
          <div><strong>Error:</strong> {sb.error}</div>
        </div>
      )}

      <div className="tabs" role="tablist">
        <button className="tab" role="tab" aria-selected={tab === "terminal"} onClick={() => setTab("terminal")}>
          <Icon name="terminal" size={14} /> Terminal
        </button>
        {LATER.map((t) => (
          <button key={t.label} className="tab" role="tab" aria-selected={false} disabled
            title={`Arrives in phase ${t.phase}`}>
            {t.label} <span className="soon">P{t.phase}</span>
          </button>
        ))}
        <button className="tab" role="tab" aria-selected={tab === "settings"} onClick={() => setTab("settings")}>
          Settings
        </button>
      </div>

      {/* Kept mounted while on Settings so the shell survives a tab switch. */}
      <div style={{ display: tab === "terminal" ? "flex" : "none", flex: 1, minHeight: 0 }}>
        <Terminal sandboxId={sb.id} running={running} />
      </div>
      {tab === "settings" && <Settings sb={sb} onDestroy={() => setDestroying(true)} />}

      {destroying && (
        <DestroyDialog sb={sb} onClose={() => setDestroying(false)}
          onDone={() => {
            toast("success", `${sb.name} destroyed`);
            navigate("/", true);
          }} />
      )}
    </div>
  );
}

function Settings({ sb, onDestroy }: { sb: Sandbox; onDestroy: () => void }) {
  return (
    <div style={{ maxWidth: 720 }}>
      <dl className="card kv">
        <dt>Template</dt><dd>{sb.template}</dd>
        <dt>Memory</dt><dd>{bytes(sb.memory)} (no swap)</dd>
        <dt>CPUs</dt><dd>{sb.cpus}</dd>
        <dt>Repository</dt><dd>{sb.repo_url ?? "none"}</dd>
        <dt>Base branch</dt><dd>{sb.base_branch ?? "none"}</dd>
        <dt>Workspace</dt><dd>/workspace (kept across stop/start)</dd>
        <dt>Runs as</dt><dd>agent (uid 1000), no capabilities</dd>
        <dt>Network</dt><dd>Internet only; LAN, Tailscale and host blocked</dd>
      </dl>
      <div className="card danger-zone">
        <div>
          <strong>Destroy sandbox</strong>
          <p>Removes the container and its workspace volume. This can't be undone.</p>
        </div>
        <button className="btn btn-danger" onClick={onDestroy}>
          <Icon name="trash" /> Destroy…
        </button>
      </div>
    </div>
  );
}

function DestroyDialog({ sb, onClose, onDone }: { sb: Sandbox; onClose: () => void; onDone: () => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = typed === sb.name;

  const destroy = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.destroy(sb.id);
      onDone();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={`Destroy ${sb.name}?`}
      description="The container and everything in /workspace will be deleted permanently."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-danger-solid" disabled={!ok || busy} onClick={destroy}>
            {busy ? "Destroying…" : "Destroy sandbox"}
          </button>
        </>
      }
    >
      {error && <div className="banner banner-danger" role="alert"><div>{error}</div></div>}
      <div className="field">
        <label htmlFor="confirm-name">Type <span className="mono">{sb.name}</span> to confirm</label>
        <input id="confirm-name" className="input mono" autoFocus value={typed} autoComplete="off"
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ok && !busy && destroy()} />
      </div>
    </Dialog>
  );
}
