import { useEffect, useState } from "react";
import { PageBack } from "../components/BackLink";
import { api, type Sandbox, type SystemInfo } from "../lib/api";
import { ago, bytes, gib, repoLabel } from "../lib/format";
import { navigate, useTitle } from "../lib/router";
import { usePoll } from "../lib/usePoll";
import { Dialog, Icon, Kbd, StatusDot, useToast } from "../components/ui";
import NewSandboxDialog from "../components/NewSandboxDialog";

export default function Home() {
  useTitle("Sandboxes");
  const system = usePoll(api.system, 5000);
  const list = usePoll(api.sandboxes, 5000);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key.toLowerCase() !== "n" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (t.closest("input, textarea, select, [contenteditable], .xterm, [role=dialog]")) return;
      e.preventDefault();
      setCreating(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const sandboxes = list.data ?? [];
  const running = sandboxes.filter((s) => s.state === "running").length;

  return (
    <div className="page">
      <PageBack />
      <header className="page-head">
        <div>
          <h1>Sandboxes</h1>
          <p>
            {list.data
              ? `${sandboxes.length} sandbox${sandboxes.length === 1 ? "" : "es"} · ${running} running`
              : "Loading…"}
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Icon name="plus" /> New sandbox <Kbd>N</Kbd>
        </button>
      </header>

      <HostPanel system={system.data} />

      <div className="section-title">
        All sandboxes <span className="line" />
      </div>
      <SandboxList
        sandboxes={list.data}
        error={list.error}
        onCreate={() => setCreating(true)}
        onChanged={() => {
          list.reload();
          system.reload();
        }}
      />

      {creating && (
        <NewSandboxDialog
          system={system.data}
          onClose={() => setCreating(false)}
          onCreated={(sb) => {
            setCreating(false);
            navigate(`/sandboxes/${sb.id}`);
          }}
        />
      )}
    </div>
  );
}

function HostPanel({ system }: { system: SystemInfo | null }) {
  if (!system) return <div className="card meters" style={{ height: 132 }} aria-busy="true" />;
  const { host, caps, sandboxes } = system;
  const used = host.mem_total - host.mem_available;
  const pct = (n: number, of: number) => `${Math.max(0, Math.min(100, (n / of) * 100))}%`;
  const reserve = Math.min(caps.memory_reserve, host.mem_available);
  const load = host.load[0];
  const diskUsed = host.disk_total - host.disk_free;
  const lowDisk = host.disk_free < caps.min_free_disk;

  return (
    <section className="card" aria-label="Host resources">
      <div className="meters">
        <div className="meter">
          <div className="meter-label">
            <span>Host memory</span>
            <span className="mono">{gib(host.mem_total, 0)} GB total</span>
          </div>
          <div className="meter-value">
            {gib(host.mem_available)} <small>GB available</small>
          </div>
          <div className="bar" role="img"
            aria-label={`${gib(used)} GB in use, ${gib(caps.memory_reserve, 0)} GB kept in reserve`}>
            <span className="seg-other" style={{ width: pct(used, host.mem_total) }} />
            <span style={{ flex: 1 }} />
            <span className="seg-reserve" style={{ width: pct(reserve, host.mem_total) }} />
          </div>
          <div className="legend">
            <span><i style={{ background: "var(--slate-500)" }} />In use (Minecraft, sandboxes, system)</span>
            <span><i className="seg-reserve" style={{ border: "1px solid var(--border-default)" }} />{gib(caps.memory_reserve, 0)} GB reserve</span>
          </div>
        </div>
        <div className="meter">
          <div className="meter-label">
            <span>Sandbox budget</span>
            <span className="mono">{sandboxes.running}/{caps.max_concurrent_sandboxes} running</span>
          </div>
          <div className="meter-value">
            {gib(sandboxes.memory_used)} <small>/ {gib(caps.total_sandbox_memory, 0)} GB</small>
          </div>
          <div className="bar">
            <span className="seg-sandbox" style={{ width: pct(sandboxes.memory_used, caps.total_sandbox_memory) }} />
          </div>
          <div className="legend">
            <span>Up to {bytes(caps.max_sandbox_memory)} each</span>
          </div>
        </div>
        <div className="meter">
          <div className="meter-label">
            <span>CPU load</span>
            <span className="mono">{host.cpus} cores</span>
          </div>
          <div className="meter-value">
            {load.toFixed(2)} <small>1 min</small>
          </div>
          <div className="bar">
            <span className={load > host.cpus ? "seg-warn" : "seg-sandbox"} style={{ width: pct(load, host.cpus) }} />
          </div>
          <div className="legend">
            <span className="mono">5m {host.load[1].toFixed(2)} · 15m {host.load[2].toFixed(2)}</span>
          </div>
        </div>
        <div className="meter">
          <div className="meter-label">
            <span>Disk</span>
            <span className="mono">{gib(host.disk_total, 0)} GB</span>
          </div>
          <div className="meter-value">
            {gib(host.disk_free, 0)} <small>GB free</small>
          </div>
          <div className="bar">
            <span className={lowDisk ? "seg-warn" : "seg-sandbox"} style={{ width: pct(diskUsed, host.disk_total) }} />
          </div>
          <div className="legend" style={{ alignItems: "center" }}>
            <span>New sandboxes and runs need {gib(caps.min_free_disk, 0)} GB free</span>
            <CleanUp />
          </div>
        </div>
      </div>
      {!system.can_start_default && system.blockers.length > 0 && (
        <div style={{ padding: "0 16px 16px" }}>
          <div className="banner banner-warning">
            <div>
              <strong>A new {bytes(caps.default_sandbox_memory)} sandbox can't start right now.</strong>
              <ul>
                {system.blockers.map((b) => <li key={b}>{b}</li>)}
              </ul>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function CleanUp() {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const run = async () => {
    setBusy(true);
    try {
      const r = await api.prune();
      const parts = [
        r.bytes ? `${bytes(r.bytes)} freed` : "nothing to free",
        r.images.length ? `${r.images.length} old image${r.images.length === 1 ? "" : "s"}` : "",
        r.containers.length + r.volumes.length ? `${r.containers.length + r.volumes.length} leftovers` : "",
      ].filter(Boolean);
      toast("success", `Clean up: ${parts.join(", ")}`);
      setConfirming(false);
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto", height: 22 }} onClick={() => setConfirming(true)}>
        Clean up
      </button>
      {confirming && (
        <Dialog title="Clean up disk space?" onClose={() => setConfirming(false)}
          description="Removes Docker data no sandbox needs. Sandboxes you haven't destroyed, and their workspaces, are never touched."
          footer={<>
            <button className="btn btn-ghost" onClick={() => setConfirming(false)}>Cancel</button>
            <button className="btn btn-primary" disabled={busy} onClick={run}>{busy ? "Cleaning…" : "Clean up"}</button>
          </>}>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-secondary)" }}>
            <li>Sandbox images from older versions that no sandbox uses</li>
            <li>Untagged images and the image build cache</li>
            <li>Containers and volumes left behind by destroyed sandboxes</li>
          </ul>
        </Dialog>
      )}
    </>
  );
}

function SandboxList({
  sandboxes,
  error,
  onCreate,
  onChanged,
}: {
  sandboxes: Sandbox[] | null;
  error: Error | null;
  onCreate: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);

  if (!sandboxes) {
    if (error) return <div className="banner banner-danger"><strong>Couldn't load sandboxes.</strong> {error.message}</div>;
    return (
      <div className="card list" aria-busy="true">
        <div className="skeleton" />
        <div className="skeleton" />
      </div>
    );
  }

  if (!sandboxes.length) {
    return (
      <div className="card empty">
        <h2>No sandboxes yet</h2>
        <p>A sandbox is an isolated container with its own workspace. Agents work inside one; you can open a shell in it too.</p>
        <button className="btn btn-primary" onClick={onCreate}>
          <Icon name="plus" /> New sandbox
        </button>
      </div>
    );
  }

  const act = async (sb: Sandbox, what: "start" | "stop") => {
    setBusy(sb.id);
    try {
      await api[what](sb.id);
      toast("success", `${sb.name} ${what === "start" ? "started" : "stopped"}`);
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  return (
    <div className="card list">
      {sandboxes.map((sb) => {
        const repo = repoLabel(sb.repo_url, sb.base_branch);
        return (
          <div key={sb.id} className="row" style={{ cursor: "pointer" }}
            onClick={(e) => !(e.target as Element).closest("a, button") && navigate(`/sandboxes/${sb.id}`)}>
            <div className="row-name">
              <StatusDot state={sb.state} />
              <div style={{ minWidth: 0 }}>
                <a href={`/sandboxes/${sb.id}`} style={{ textDecoration: "none" }}>
                  <strong>{sb.name}</strong>
                </a>
                <div className="row-sub">
                  <span className="mono">{sb.template}</span>
                  {repo && <> · {repo}</>}
                  {sb.last_run && (
                    <> · last run <span className={`run-text-${sb.last_run.state}`}>{sb.last_run.state.replace("_", " ")}</span></>
                  )}
                  {sb.state !== "running" && (
                    <span style={{ color: sb.state === "error" ? "var(--status-danger)" : undefined }}>
                      {" · "}{sb.state === "error" && sb.error ? sb.error : sb.state}
                    </span>
                  )}
                </div>
              </div>
            </div>
            <div className="row-meta hide-sm">
              {sb.memory_usage != null ? `${bytes(sb.memory_usage)} / ` : ""}{sb.memory_human.toUpperCase()} · {sb.cpus} CPU
              {sb.workspace_bytes != null && <div className="row-res">disk {bytes(sb.workspace_bytes)}</div>}
              <div className="row-sub">{ago(sb.created_at)}</div>
            </div>
            <div className="row-actions">
              {sb.state === "running" ? (
                <>
                  <a className="btn btn-sm" href={`/sandboxes/${sb.id}`} title="Open terminal">
                    <Icon name="terminal" /> <span className="hide-sm">Terminal</span>
                  </a>
                  <button className="btn btn-sm btn-icon btn-ghost" title="Stop" aria-label={`Stop ${sb.name}`}
                    disabled={busy === sb.id} onClick={() => act(sb, "stop")}>
                    <Icon name="stop" />
                  </button>
                </>
              ) : sb.state === "stopped" || sb.state === "error" ? (
                <button className="btn btn-sm" disabled={busy === sb.id} onClick={() => act(sb, "start")}>
                  <Icon name="play" /> Start
                </button>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
