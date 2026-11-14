import { useEffect, useState } from "react";
import { api, ApiError, type Sandbox, type SystemInfo } from "../lib/api";
import { ago, bytes, repoLabel } from "../lib/format";
import { navigate, useTitle } from "../lib/router";
import { usePoll } from "../lib/usePoll";
import { Dialog, Icon, StatusBadge, useToast } from "../components/ui";
import Terminal from "../components/Terminal";
import Runs from "../components/Runs";
import Files from "../components/Files";
import Changes from "../components/Changes";
import Preview from "../components/Preview";

type Tab = "runs" | "changes" | "files" | "terminal" | "preview" | "settings";
const TABS: { id: Tab; label: string }[] = [
  { id: "runs", label: "Runs" },
  { id: "changes", label: "Changes" },
  { id: "files", label: "Files" },
  { id: "terminal", label: "Terminal" },
  { id: "preview", label: "Preview" },
  { id: "settings", label: "Settings" },
];

export default function SandboxDetail({ id }: { id: string }) {
  const { data: sb, error, reload } = usePoll(() => api.sandbox(id), 5000, [id]);
  const { data: system } = usePoll(api.system, 15000);
  const [tab, setTab] = useState<Tab>("runs");
  const [termOpened, setTermOpened] = useState(false); // connect only once the tab is used
  useEffect(() => {
    if (tab === "terminal") setTermOpened(true);
  }, [tab]);
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
        <span>
          {sb.memory_usage != null ? `${bytes(sb.memory_usage)} of ` : ""}{bytes(sb.memory)} · {sb.cpus} CPU
        </span>
        {sb.workspace_bytes != null && <span>workspace {bytes(sb.workspace_bytes)}</span>}
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
        {TABS.map((t) => (
          <button key={t.id} className="tab" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
            {t.id === "terminal" && <Icon name="terminal" size={14} />} {t.label}
          </button>
        ))}
      </div>

      {tab === "runs" && <Runs sb={sb} system={system} />}
      {tab === "changes" && <Changes sb={sb} />}
      {tab === "files" && <Files sb={sb} />}
      {tab === "preview" && <Preview sb={sb} />}
      {/* Kept mounted on other tabs so the shell survives a tab switch. */}
      {termOpened && (
        <div style={{ display: tab === "terminal" ? "flex" : "none", flex: 1, minHeight: 0 }}>
          <Terminal sandboxId={sb.id} running={running} />
        </div>
      )}
      {tab === "settings" && (
        <Settings sb={sb} system={system} onChanged={reload} onDestroy={() => setDestroying(true)} />
      )}

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

function Settings({ sb, system, onChanged, onDestroy }: {
  sb: Sandbox;
  system: SystemInfo | null;
  onChanged: () => void;
  onDestroy: () => void;
}) {
  const toast = useToast();
  const [saving, setSaving] = useState(false);
  const hasToken = system?.agent.github_token ?? false;
  const canPush = !!sb.repo_url && hasToken;
  const togglePush = async () => {
    setSaving(true);
    try {
      await api.updateSandbox(sb.id, { allow_push: !sb.allow_push });
      onChanged();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <div style={{ maxWidth: 720 }}>
      <div className="card toggle-row" style={{ marginBottom: 16 }}>
        <div>
          <strong id="push-label">Push agent branches to GitHub</strong>
          <p>
            After a successful run, push its <span className="mono">agent/&lt;run-id&gt;</span> branch to the
            repository. Never <span className="mono">main</span>, never forced; the GitHub token stays on the server.
            {!sb.repo_url && " This sandbox has no repository."}
            {sb.repo_url && !hasToken && " No GitHub token is set (python -m app.set_secret github)."}
          </p>
        </div>
        <button className="toggle" role="switch" aria-checked={sb.allow_push} aria-labelledby="push-label"
          disabled={saving || (!canPush && !sb.allow_push)} onClick={togglePush} />
      </div>
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
