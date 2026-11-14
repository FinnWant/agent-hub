import { useEffect, useMemo, useState } from "react";
import { Diff, Hunk, parseDiff, type FileData } from "react-diff-view";
import "react-diff-view/style/index.css";
import { api, type Run, type RunDiff, type Sandbox } from "../lib/api";
import { ago } from "../lib/format";
import { RunBadge } from "./Runs";

type View = "unified" | "split";

/** Review a run: its branch against the point where it left the base branch. */
export default function Changes({ sb }: { sb: Sandbox }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [diff, setDiff] = useState<RunDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>(() => (window.innerWidth > 1100 ? "split" : "unified"));

  useEffect(() => {
    api.runs(sb.id).then((rs) => {
      const reviewable = rs.filter((r) => r.commit_sha);
      setRuns(reviewable);
      setRunId((cur) => cur ?? reviewable[0]?.id ?? null);
    }).catch((e) => setError((e as Error).message));
  }, [sb.id]);

  useEffect(() => {
    if (!runId || sb.state !== "running") return;
    setDiff(null);
    setError(null);
    api.runDiff(runId).then(setDiff).catch((e) => setError((e as Error).message));
  }, [runId, sb.state]);

  const parsed: FileData[] = useMemo(() => {
    if (!diff?.patch) return [];
    try {
      return parseDiff(diff.patch, { nearbySequences: "zip" });
    } catch {
      return [];
    }
  }, [diff]);

  if (sb.state !== "running") {
    return <div className="card empty"><h2>Sandbox is stopped</h2><p>Start it to review changes.</p></div>;
  }
  if (runs && !runs.length) {
    return (
      <div className="card empty">
        <h2>Nothing to review yet</h2>
        <p>When a run changes files, its commit on <span className="mono">agent/&lt;run-id&gt;</span> shows up here as a diff.</p>
      </div>
    );
  }

  const run = runs?.find((r) => r.id === runId);
  const totals = diff?.files.reduce((t, f) => ({ a: t.a + (f.added ?? 0), r: t.r + (f.removed ?? 0) }), { a: 0, r: 0 });

  return (
    <div className="changes">
      <div className="changes-bar">
        <label className="visually-hidden" htmlFor="run-pick">Run</label>
        <select id="run-pick" className="input changes-select" value={runId ?? ""} onChange={(e) => setRunId(e.target.value)}>
          {runs?.map((r) => (
            <option key={r.id} value={r.id}>
              {r.prompt.split("\n")[0].slice(0, 70)} · {ago(r.created_at)}
            </option>
          ))}
        </select>
        {run && <RunBadge state={run.state} />}
        <span className="spacer" />
        <div className="segmented compact" role="radiogroup" aria-label="Diff layout">
          {(["unified", "split"] as View[]).map((v) => (
            <button key={v} type="button" role="radio" aria-checked={view === v} onClick={() => setView(v)}>{v}</button>
          ))}
        </div>
      </div>

      {diff && (
        <div className="changes-summary mono">
          <span>{diff.branch}</span>
          <span className="muted">vs {diff.base}</span>
          <span>{diff.files.length} file{diff.files.length === 1 ? "" : "s"}</span>
          {totals && <span><span className="add">+{totals.a}</span> <span className="del">−{totals.r}</span></span>}
        </div>
      )}
      {error && <div className="banner banner-danger"><div>{error}</div></div>}
      {!diff && !error && <div className="card list" aria-busy="true"><div className="skeleton" /><div className="skeleton" /></div>}
      {diff?.too_large && (
        <div className="banner banner-warning"><div><strong>This diff is too large to show</strong> ({Math.round(diff.size / 1024)} KB). The file list is below; use the terminal for details.</div></div>
      )}

      {diff && (
        <div className="changes-files">
          {diff.files.map((f, i) => {
            // numstat and the patch list files in the same order; fall back to matching by path.
            const fd = parsed.length === diff.files.length ? parsed[i]
              : parsed.find((p) => p.newPath === f.path || p.oldPath === f.path);
            return (
              <section key={f.path} className="card diff-file">
                <header className="diff-file-head">
                  <span className="mono diff-file-path">{f.path}</span>
                  {fd && fd.type !== "modify" && <span className="tag">{fd.type === "add" ? "new" : fd.type === "delete" ? "deleted" : fd.type}</span>}
                  <span className="spacer" />
                  {f.added === null ? <span className="muted mono">binary</span> : (
                    <span className="mono"><span className="add">+{f.added}</span> <span className="del">−{f.removed}</span></span>
                  )}
                </header>
                {fd && fd.hunks.length > 0 && (
                  <div className="diff-scroll">
                    <Diff viewType={view} diffType={fd.type} hunks={fd.hunks} className="kiln-diff">
                      {(hunks) => hunks.map((h) => <Hunk key={h.content} hunk={h} />)}
                    </Diff>
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
