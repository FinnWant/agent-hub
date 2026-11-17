import { useCallback, useEffect, useState } from "react";
import { api, type Run, type Sandbox } from "../lib/api";
import { NEEDS_YOU_EVENT, usePendingRequests } from "./Requests";

/** Fired when a run starts or ends in this window, so the Live list updates at once. */
export const RUNS_CHANGED_EVENT = "kiln:runs-changed";

/* ---------------------------------------------------------------- recent sandboxes (per browser) */

const RECENT_KEY = "kiln.recentSandboxes";
const RECENT_MAX = 5;
const RECENT_EVENT = "kiln:recent-sandboxes";

function readRecent(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, 20) : [];
  } catch {
    return [];
  }
}

/** Remember that a sandbox was opened (most recent first). */
export function noteSandboxOpened(id: string) {
  const next = [id, ...readRecent().filter((x) => x !== id)].slice(0, 20);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* private mode: no recents */
  }
  window.dispatchEvent(new Event(RECENT_EVENT));
}

/* ---------------------------------------------------------------- helpers */

function elapsed(run: Run, now: number): string {
  if (!run.started_at) return "queued";
  const s = Math.max(0, Math.round(now - run.started_at - (run.paused_seconds ?? 0)));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function useVisibleInterval(fn: () => void, ms: number) {
  useEffect(() => {
    fn();
    const t = setInterval(() => document.visibilityState === "visible" && fn(), ms);
    const onVis = () => document.visibilityState === "visible" && fn();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [fn, ms]);
}

/* ---------------------------------------------------------------- the section */

/**
 * The sidebar's Activity: agents running now (across sandboxes) and the sandboxes you opened last.
 * Hidden on phones, where the sidebar is a top bar.
 */
export default function SidebarActivity({ currentSandbox }: { currentSandbox: string | null }) {
  const [live, setLive] = useState<(Run & { sandbox_name: string | null })[] | null>(null);
  const [sandboxes, setSandboxes] = useState<Sandbox[] | null>(null);
  const [recent, setRecent] = useState<string[]>(readRecent);
  const [now, setNow] = useState(Date.now() / 1000);
  const { items: waiting } = usePendingRequests();

  const loadLive = useCallback(() => {
    api.liveRuns().then(setLive).catch(() => {});
  }, []);
  const loadSandboxes = useCallback(() => {
    api.sandboxes().then(setSandboxes).catch(() => {});
  }, []);
  useVisibleInterval(loadLive, 5000);
  useVisibleInterval(loadSandboxes, 15000);

  useEffect(() => {
    const onRuns = () => {
      loadLive();
      loadSandboxes();
    };
    const onRecent = () => setRecent(readRecent());
    window.addEventListener(RUNS_CHANGED_EVENT, onRuns);
    window.addEventListener(NEEDS_YOU_EVENT, loadLive);
    window.addEventListener(RECENT_EVENT, onRecent);
    window.addEventListener("storage", onRecent); // other tabs
    return () => {
      window.removeEventListener(RUNS_CHANGED_EVENT, onRuns);
      window.removeEventListener(NEEDS_YOU_EVENT, loadLive);
      window.removeEventListener(RECENT_EVENT, onRecent);
      window.removeEventListener("storage", onRecent);
    };
  }, [loadLive, loadSandboxes]);

  // The clock only ticks while something is running.
  useEffect(() => {
    if (!live?.length) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [live?.length]);

  const byId = new Map((sandboxes ?? []).map((s) => [s.id, s]));
  const recentSandboxes = recent.map((id) => byId.get(id)).filter((s): s is Sandbox => !!s).slice(0, RECENT_MAX);
  const waitingRuns = new Set(waiting.map((r) => r.run_id));

  return (
    <div className="side-activity">
      <section aria-labelledby="side-live">
        <h2 id="side-live" className="side-title">
          Live{live && live.length > 0 && <span className="side-count mono">{live.length}</span>}
        </h2>
        {live === null ? null : live.length === 0 ? (
          <p className="side-empty">No agents running</p>
        ) : (
          <ul className="side-list">
            {live.map((r) => (
              <li key={r.id}>
                <a href={`/sandboxes/${r.sandbox_id}/runs?run=${r.id}`} className="side-item"
                  title={`${r.sandbox_name ?? r.sandbox_id}: ${r.prompt.split("\n")[0]}`}>
                  <span className={`dot ${waitingRuns.has(r.id) ? "dot-waiting" : `run-dot-${r.state}`}`} aria-hidden />
                  <span className="side-item-main">
                    <span className="side-item-name">{r.sandbox_name ?? r.sandbox_id}</span>
                    <span className="side-item-sub">{r.prompt.split("\n")[0]}</span>
                  </span>
                  <span className="side-item-meta mono">
                    {waitingRuns.has(r.id) ? "needs you" : elapsed(r, now)}
                    {r.usage && r.usage.cost > 0 && <span>≈${r.usage.cost.toFixed(2)}</span>}
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>

      {recentSandboxes.length > 0 && (
        <section aria-labelledby="side-recent">
          <h2 id="side-recent" className="side-title">Recent</h2>
          <ul className="side-list">
            {recentSandboxes.map((sb) => (
              <li key={sb.id}>
                <a href={`/sandboxes/${sb.id}`} className="side-item side-item-compact"
                  aria-current={sb.id === currentSandbox ? "page" : undefined} title={`${sb.name} · ${sb.state}`}>
                  <span className={`dot dot-${sb.state}`} aria-hidden />
                  <span className="side-item-name">{sb.name}</span>
                  {sb.last_run && (sb.last_run.state === "running" || sb.last_run.state === "queued") && (
                    <span className="side-item-meta mono">agent</span>
                  )}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
