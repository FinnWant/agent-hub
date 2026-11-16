import { useEffect, useMemo, useState } from "react";
import { api, type Checkpoint, type Run, type RunEvent } from "../lib/api";
import { Dialog, Icon, useToast } from "./ui";

/* ---------------------------------------------------------------- Timeline */

type Phase = "explore" | "edit" | "run" | "other";

const PHASE_OF: Record<string, Phase> = {
  Read: "explore", Grep: "explore", Glob: "explore", LS: "explore", WebFetch: "explore", WebSearch: "explore",
  Edit: "edit", Write: "edit", MultiEdit: "edit", NotebookEdit: "edit",
  Bash: "run",
};
const PHASE_LABEL: Record<Phase, string> = { explore: "Reading", edit: "Editing", run: "Running", other: "Other" };
const EDITS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit"]);

type Marker = { ts: number; kind: "checkpoint" | "compact" | "loop" | "request" | "message" | "check"; label: string; bad?: boolean };

/** The run at a glance: what the agent did when, and where things happened. From the log alone. */
export function Timeline({ events, run }: { events: RunEvent[]; run: Run }) {
  const { segments, markers, start, end, files } = useMemo(() => {
    const logs = events.filter((e) => e.type === "log");
    const start = run.started_at ?? logs[0]?.ts ?? 0;
    const end = Math.max(run.finished_at ?? 0, logs[logs.length - 1]?.ts ?? start, start + 1);
    const segments: { from: number; to: number; phase: Phase; name: string }[] = [];
    const markers: Marker[] = [];
    const files = new Map<string, number>();
    for (const e of logs) {
      if (e.type !== "log") continue;
      if (e.kind === "tool") {
        const phase = PHASE_OF[e.name ?? ""] ?? "other";
        const last = segments[segments.length - 1];
        if (last) last.to = e.ts;
        segments.push({ from: e.ts, to: e.ts, phase, name: e.name ?? "" });
        if (EDITS.has(e.name ?? "") && e.text) files.set(e.text, (files.get(e.text) ?? 0) + 1);
      } else if (e.kind === "checkpoint") markers.push({ ts: e.ts, kind: "checkpoint", label: e.text });
      else if (e.kind === "loop") markers.push({ ts: e.ts, kind: "loop", label: e.text, bad: true });
      else if (e.kind === "request") markers.push({ ts: e.ts, kind: "request", label: e.title });
      else if (e.kind === "user") markers.push({ ts: e.ts, kind: "message", label: `You: ${e.text.slice(0, 80)}` });
      else if (e.kind === "check") markers.push({ ts: e.ts, kind: "check", label: e.passed ? "Checks pass" : "Checks fail", bad: !e.passed });
      else if (e.kind === "system" && e.text.includes("compacted")) markers.push({ ts: e.ts, kind: "compact", label: "Context compacted" });
    }
    const last = segments[segments.length - 1];
    if (last) last.to = end;
    return { segments, markers, start, end, files: [...files.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8) };
  }, [events, run.started_at, run.finished_at]);

  if (!segments.length && !markers.length) return null;
  const pos = (ts: number) => `${((ts - start) / (end - start)) * 100}%`;
  const used = new Set(segments.map((s) => s.phase));
  return (
    <div className="timeline" aria-label="Run timeline">
      <div className="timeline-track">
        {segments.map((s, i) => (
          <span key={i} className={`tl-seg tl-${s.phase}`} title={`${PHASE_LABEL[s.phase]} (${s.name})`}
            style={{ left: pos(s.from), width: `max(2px, calc(${pos(s.to)} - ${pos(s.from)}))` }} />
        ))}
        {markers.map((m, i) => (
          <span key={`m${i}`} className={`tl-mark tl-mark-${m.kind} ${m.bad ? "is-bad" : ""}`} style={{ left: pos(m.ts) }}
            title={m.label} />
        ))}
      </div>
      <div className="timeline-legend">
        {(["explore", "edit", "run", "other"] as Phase[]).filter((p) => used.has(p)).map((p) => (
          <span key={p}><i className={`tl-${p}`} />{PHASE_LABEL[p]}</span>
        ))}
        {markers.some((m) => m.kind === "checkpoint") && <span><i className="tl-key-checkpoint" />Checkpoint</span>}
        {markers.some((m) => m.bad) && <span><i className="tl-key-bad" />Problem</span>}
        {markers.some((m) => m.kind === "compact" || m.kind === "request" || m.kind === "message") && (
          <span><i className="tl-key-note" />Compaction, request or message</span>
        )}
      </div>
      {files.length > 0 && (
        <div className="timeline-files">
          {files.map(([f, n]) => (
            <span key={f} className="mono" title={`${n} edit${n === 1 ? "" : "s"}`}>
              {f.replace(/^\/workspace\//, "")}{n > 1 ? ` ×${n}` : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- Report */

const ORDER = ["Done", "Not done", "Assumptions", "How to verify", "Risks"] as const;

export function ReportCard({ run, onChange, onContinue }: {
  run: Run;
  onChange: (run: Run) => void;
  onContinue?: (run: Run) => void;
}) {
  const toast = useToast();
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [busy, setBusy] = useState<number | null>(null);
  const rep = run.report;
  useEffect(() => {
    api.checkpoints(run.id).then(setCheckpoints).catch(() => {});
  }, [run.id, run.finished_at]);
  if (!rep) return null;
  const f = rep.facts;
  const rewind = async (n: number) => {
    setBusy(n);
    try {
      const r = await api.rewind(run.id, n);
      onChange(r);
      toast("success", `Branch put back to checkpoint ${n}`);
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const facts = [
    `${f.minutes} min${f.waited_minutes ? ` (+${f.waited_minutes} waiting on you)` : ""}`,
    f.files != null && `${f.files} file${f.files === 1 ? "" : "s"} · +${f.insertions ?? 0} −${f.deletions ?? 0}`,
    f.checks && `checks ${f.checks === "passed" ? "pass" : "fail"}${f.check_rounds ? ` after ${f.check_rounds} round${f.check_rounds === 1 ? "" : "s"} of fixes` : ""}`,
    f.cost_est != null && `≈$${f.cost_est.toFixed(2)}${f.api_calls ? ` · ${f.api_calls} API calls` : ""}`,
    f.questions + f.approvals > 0 && `${f.questions + f.approvals} request${f.questions + f.approvals === 1 ? "" : "s"} to you`,
    f.compactions > 0 && `${f.compactions} compaction${f.compactions === 1 ? "" : "s"}`,
    f.loops > 0 && `${f.loops} loop${f.loops === 1 ? "" : "s"} caught`,
  ].filter(Boolean) as string[];
  const sections = ORDER.filter((k) => rep.sections[k]);
  return (
    <section className="report" aria-label="Handoff report">
      <div className="report-facts">
        {facts.map((t) => <span key={t} className="mono">{t}</span>)}
      </div>
      {sections.length > 0 ? (
        <dl className="report-sections">
          {sections.map((k) => (
            <div key={k} className={`report-section report-${k.replace(/ /g, "-").toLowerCase()}`}>
              <dt>{k}</dt>
              <dd>{rep.sections[k]}</dd>
            </div>
          ))}
        </dl>
      ) : rep.summary ? <p className="report-summary">{rep.summary}</p> : null}
      {checkpoints.length > 0 && (
        <details className="report-checkpoints">
          <summary>
            {checkpoints.length} checkpoint{checkpoints.length === 1 ? "" : "s"}
            {run.rewound_to ? ` · branch put back to ${run.rewound_to}` : ""}
          </summary>
          <ol>
            {checkpoints.map((c) => (
              <li key={c.n}>
                <span className="mono">{c.n}</span>
                <span className="muted">{new Date(c.ts * 1000).toLocaleTimeString()}</span>
                <span className="report-cp-files mono" title={c.files.join("\n")}>
                  {c.files.slice(0, 3).join(", ")}{c.files.length > 3 ? ` +${c.files.length - 3}` : ""}
                </span>
                <button className="btn btn-ghost btn-sm" disabled={busy !== null} onClick={() => rewind(c.n)}
                  title="Put the run's branch back to this point (the work after it is dropped from the branch)">
                  {busy === c.n ? "Rewinding…" : run.rewound_to === c.n ? "Current" : "Rewind here"}
                </button>
              </li>
            ))}
          </ol>
          {run.rewound_to && onContinue && (
            <button className="btn btn-sm" onClick={() => onContinue(run)}>Continue from checkpoint {run.rewound_to}</button>
          )}
        </details>
      )}
      <a className="report-download" href={`/api/runs/${encodeURIComponent(run.id)}/log/full`} download>
        Download the full log
      </a>
    </section>
  );
}

/* ---------------------------------------------------------------- Ledger */

export function LedgerButton({ sandboxId, disabled }: { sandboxId: string; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="btn btn-ghost btn-sm" disabled={disabled} onClick={() => setOpen(true)}
        title={disabled ? "Start the sandbox to open its ledger" : "The agent's notes across runs"}>
        <Icon name="list" size={14} /> Ledger
      </button>
      {open && <LedgerDialog sandboxId={sandboxId} onClose={() => setOpen(false)} />}
    </>
  );
}

function LedgerDialog({ sandboxId, onClose }: { sandboxId: string; onClose: () => void }) {
  const toast = useToast();
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.ledger(sandboxId).then((l) => {
      setText(l.text);
      setSaved(l.text);
    }).catch((e) => toast("error", (e as Error).message));
  }, [sandboxId, toast]);
  const save = async () => {
    if (text == null) return;
    setBusy(true);
    try {
      await api.saveLedger(sandboxId, text);
      setSaved(text);
      toast("success", "Ledger saved");
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog title="Ledger" labelledBy="ledger-title" onClose={onClose}
      description={<>The agent's notes in <span className="mono">/workspace/.kiln/LEDGER.md</span>, kept across runs and
        context compaction, never committed. Correct its plan or assumptions here; Kiln rewrites only its own block.</>}
      footer={<>
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
        <button className="btn btn-primary" onClick={save} disabled={busy || text == null || text === saved}>
          {busy ? "Saving…" : "Save"}
        </button>
      </>}>
      {text == null ? <p className="muted">Loading…</p> : (
        <textarea className="input mono ledger-input" rows={18} maxLength={60000} spellCheck={false}
          value={text} onChange={(e) => setText(e.target.value)}
          placeholder="No ledger yet: the first run creates one." />
      )}
    </Dialog>
  );
}
