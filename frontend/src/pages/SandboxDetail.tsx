import { useEffect, useState } from "react";
import { api, ApiError, type Sandbox, type SystemInfo } from "../lib/api";
import { ago, bytes, repoLabel } from "../lib/format";
import { navigate, previousPath, useQuery, useTitle } from "../lib/router";
import { BackLink } from "../components/BackLink";
import { noteSandboxOpened } from "../components/SidebarActivity";
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

const isTab = (t: string | null): t is Tab => TABS.some((x) => x.id === t);

export default function SandboxDetail({ id, tab: tabParam }: { id: string; tab: string | null }) {
  const { data: sb, error, reload } = usePoll(() => api.sandbox(id), 5000, [id]);
  const { data: system } = usePoll(api.system, 15000);
  useQuery(); // re-render on ?run= / ?file= changes (the back link reads them)
  const tab: Tab = isTab(tabParam) ? tabParam : "runs";
  // Each tab is its own URL, so Back returns to the previous tab instead of leaving the sandbox.
  const setTab = (t: Tab) => navigate(`/sandboxes/${id}/${t}`);
  const [termOpened, setTermOpened] = useState(false); // connect only once the tab is used
  useEffect(() => {
    if (tab === "terminal") setTermOpened(true);
  }, [tab]);
  const [busy, setBusy] = useState(false);
  const [destroying, setDestroying] = useState(false);
  const toast = useToast();
  useTitle(sb?.name ?? "Sandbox");
  const found = !!sb;
  useEffect(() => {
    if (found) noteSandboxOpened(id);
  }, [id, found]);

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
          <>
            <nav className="crumbs" aria-label="Breadcrumb"><BackLink /></nav>
            <div className="banner banner-danger"><strong>Couldn't load it.</strong> {error.message}</div>
          </>
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
        <BackLink />
        {previousPath() && previousPath() !== "/" && (
          <>
            <span className="crumbs-sep" aria-hidden>·</span>
            <a href="/">Sandboxes</a>
          </>
        )}
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
          <button key={t.id} className="tab" role="tab" aria-selected={tab === t.id} onClick={() => t.id !== tab && setTab(t.id)}>
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
      <BaseBranchSetting sb={sb} onChanged={onChanged} />
      <ChecksSetting sb={sb} onChanged={onChanged} />
      <OwnerSettings sb={sb} onChanged={onChanged} />
      <dl className="card kv">
        <dt>Template</dt><dd>{sb.template}</dd>
        <dt>Memory</dt><dd>{bytes(sb.memory)} (no swap)</dd>
        <dt>CPUs</dt><dd>{sb.cpus}</dd>
        <dt>Repository</dt><dd>{sb.repo_url ?? "none"}</dd>
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

const BRANCH_RE = /^[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$/;

function BaseBranchSetting({ sb, onChanged }: { sb: Sandbox; onChanged: () => void }) {
  const toast = useToast();
  const [value, setValue] = useState(sb.base_branch ?? "");
  const [saving, setSaving] = useState(false);
  useEffect(() => setValue(sb.base_branch ?? ""), [sb.base_branch]);
  const v = value.trim();
  const invalid = !!v && !BRANCH_RE.test(v);
  const changed = v !== (sb.base_branch ?? "");
  const save = async () => {
    setSaving(true);
    try {
      await api.updateSandbox(sb.id, { base_branch: v });
      toast("success", v ? `Base branch set to ${v}; it applies from the next run` : "Base branch cleared");
      onChanged();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <form className="card checks-setting" onSubmit={(e) => { e.preventDefault(); if (!invalid) save(); }}>
      <div>
        <label htmlFor="base-branch"><strong>Base branch</strong></label>
        <p>
          The branch each new run starts from, and that its diff is compared against.
          {sb.repo_url ? " If it isn't in the sandbox yet, it's fetched from the repository at the next run."
            : " Leave it empty to use the workspace's current branch."}
        </p>
      </div>
      <div className="checks-row">
        <input id="base-branch" className="input mono" maxLength={100} spellCheck={false} autoComplete="off"
          placeholder="the repository's default branch" value={value} aria-invalid={invalid}
          onChange={(e) => setValue(e.target.value)} />
        <button className="btn" type="submit" disabled={saving || !changed || invalid}>
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      {invalid && <span className="error">Not a valid branch name</span>}
    </form>
  );
}

function ChecksSetting({ sb, onChanged }: { sb: Sandbox; onChanged: () => void }) {
  const toast = useToast();
  const [value, setValue] = useState(sb.verify_cmd ?? "");
  const [hint, setHint] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => setValue(sb.verify_cmd ?? ""), [sb.verify_cmd]);
  useEffect(() => {
    api.templates().then((ts) => setHint(ts.find((t) => t.name === sb.template)?.check_hint ?? "")).catch(() => {});
  }, [sb.template]);
  const changed = value.trim() !== (sb.verify_cmd ?? "");
  const save = async () => {
    setSaving(true);
    try {
      await api.updateSandbox(sb.id, { verify_cmd: value.trim() });
      toast("success", value.trim() ? "Checks saved; they apply from the next run" : "Checks removed");
      onChanged();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <form className="card checks-setting" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <div>
        <label htmlFor="verify-cmd"><strong>Checks</strong></label>
        <p>
          A command that passes when the work is done, such as your tests and linter. When the agent wants to
          finish, Kiln runs it; if it fails, the agent gets the output and keeps working (a few rounds at most).
          Kiln runs it again after the run, and never pushes a branch whose checks fail.
        </p>
      </div>
      <div className="checks-row">
        <input id="verify-cmd" className="input mono" maxLength={1000} spellCheck={false} autoComplete="off"
          placeholder={hint ? `e.g. ${hint}` : "e.g. make test"} value={value}
          onChange={(e) => setValue(e.target.value)} />
        <button className="btn" type="submit" disabled={saving || !changed}>
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}

const RULES_HINT = "rm -rf\nBash: git push\nEdit: .github/workflows/\nWrite: .env";

function OwnerSettings({ sb, onChanged }: { sb: Sandbox; onChanged: () => void }) {
  const toast = useToast();
  const [budget, setBudget] = useState(sb.budget_usd != null ? String(sb.budget_usd) : "");
  const [rules, setRules] = useState(sb.approval_rules ?? "");
  const [saving, setSaving] = useState(false);
  useEffect(() => setBudget(sb.budget_usd != null ? String(sb.budget_usd) : ""), [sb.budget_usd]);
  useEffect(() => setRules(sb.approval_rules ?? ""), [sb.approval_rules]);
  const budgetNum = budget.trim() ? Number(budget) : 0;
  const valid = Number.isFinite(budgetNum) && budgetNum >= 0;
  const changed = budgetNum !== (sb.budget_usd ?? 0) || rules.trim() !== (sb.approval_rules ?? "");
  const save = async () => {
    setSaving(true);
    try {
      await api.updateSandbox(sb.id, { budget_usd: budgetNum, approval_rules: rules.trim() });
      toast("success", "Saved; applies from the next run");
      onChanged();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <form className="card checks-setting" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <div>
        <label htmlFor="budget"><strong>Budget per run</strong></label>
        <p>
          Kiln measures every Claude call at its gateway and estimates the cost at API list prices (your
          subscription isn't billed per token). At 80% you're notified; at 100% the agent is paused until you extend
          the budget or stop the run (after 30 minutes without an answer it stops). Empty: no budget.
        </p>
      </div>
      <div className="checks-row">
        <span className="input-prefix">$</span>
        <input id="budget" className="input mono" inputMode="decimal" placeholder="e.g. 5" value={budget}
          aria-invalid={!valid} onChange={(e) => setBudget(e.target.value)} style={{ maxWidth: 140 }} />
      </div>
      <div>
        <label htmlFor="rules"><strong>Ask me before</strong></label>
        <p>
          One rule per line. Shell commands containing the text, or <span className="mono">Edit:</span> /{" "}
          <span className="mono">Write:</span> and part of a file path. The agent waits for your OK (10 minutes,
          then it's denied). A guard rail for the agent's judgement, not a security boundary.
        </p>
      </div>
      <textarea id="rules" className="input mono rules-input" rows={4} maxLength={5000} spellCheck={false}
        placeholder={RULES_HINT} value={rules} onChange={(e) => setRules(e.target.value)} />
      <div className="checks-row" style={{ justifyContent: "flex-end" }}>
        <button className="btn" type="submit" disabled={saving || !changed || !valid}>{saving ? "Saving…" : "Save"}</button>
      </div>
    </form>
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
