import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, ApiError, runEventsUrl, type Run, type RunEvent, type RunState, type Sandbox, type SystemInfo } from "../lib/api";
import { ago } from "../lib/format";
import { Icon, Kbd, useToast } from "./ui";
import { RequestCard, usePendingRequests } from "./Requests";

const MAX_EVENTS = 5000; // cap the in-browser buffer
const ACTIVE: RunState[] = ["queued", "running"];
const SUBMIT_KEY = /Mac|iPhone|iPad/.test(navigator.userAgent) ? "⌘↵" : "Ctrl ↵";

const STATE_LABEL: Record<RunState, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  timed_out: "Timed out",
};

export function RunBadge({ state }: { state: RunState }) {
  return (
    <span className={`badge run-${state}`}>
      <span className={`dot run-dot-${state}`} aria-hidden />
      {STATE_LABEL[state]}
    </span>
  );
}

function duration(run: Run, now: number): string {
  if (!run.started_at) return "";
  const s = Math.max(0, Math.round((run.finished_at ?? now) - run.started_at));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/* ---------------------------------------------------------------- Panel */

export default function Runs({ sb, system }: { sb: Sandbox; system: SystemInfo | null }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [continuing, setContinuing] = useState<Run | null>(null);
  const toast = useToast();

  const reload = useCallback(async () => {
    try {
      const list = await api.runs(sb.id);
      setRuns(list);
      setSelected((cur) => cur ?? list[0]?.id ?? null);
    } catch (e) {
      toast("error", (e as Error).message);
    }
  }, [sb.id, toast]);

  useEffect(() => {
    reload();
    const t = setInterval(() => document.visibilityState === "visible" && reload(), 5000);
    return () => clearInterval(t);
  }, [reload]);

  const active = runs?.find((r) => ACTIVE.includes(r.state));
  const current = runs?.find((r) => r.id === selected) ?? null;

  return (
    <div className="runs">
      <aside className="runs-side">
        <Composer sb={sb} system={system} busyRun={active ?? null} continuing={continuing}
          onClearContinue={() => setContinuing(null)}
          onStarted={(run) => {
            setRuns((rs) => [run, ...(rs ?? [])]);
            setSelected(run.id);
            setContinuing(null);
          }} />
        <div className="runs-history" role="listbox" aria-label="Runs">
          {runs?.length === 0 && <p className="muted" style={{ padding: "8px 4px", margin: 0 }}>No runs yet.</p>}
          {runs?.map((r) => (
            <button key={r.id} role="option" aria-selected={r.id === selected} className="run-item"
              onClick={() => setSelected(r.id)}>
              <span className="run-item-top">
                <span className={`dot run-dot-${r.state}`} aria-hidden />
                <span className="run-item-prompt">{r.prompt.split("\n")[0]}</span>
              </span>
              <span className="run-item-meta">
                {STATE_LABEL[r.state]} · {ago(r.created_at)}
                {r.parent_run_id && ` · continues ${r.parent_run_id.slice(0, 6)}`}
                {r.verify && ` · checks ${r.verify === "passed" ? "pass" : "fail"}`}
                {r.cost_usd != null && ` · $${r.cost_usd.toFixed(2)}`}
              </span>
            </button>
          ))}
        </div>
      </aside>
      <section className="runs-main">
        {current ? (
          <RunView key={current.id} initial={current}
            onChange={(run) => setRuns((rs) => rs?.map((r) => (r.id === run.id ? run : r)) ?? rs)}
            onContinue={active ? undefined : setContinuing} />
        ) : (
          <div className="card empty" style={{ height: "100%", justifyContent: "center" }}>
            <h2>Give the agent a task</h2>
            <p>
              It works on a fresh <span className="mono">agent/&lt;run-id&gt;</span> branch in this sandbox. You
              watch it live, and its changes are committed for you to review.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

/* ---------------------------------------------------------------- Composer */

function Composer({ sb, system, busyRun, continuing, onClearContinue, onStarted }: {
  sb: Sandbox;
  system: SystemInfo | null;
  busyRun: Run | null;
  continuing: Run | null;
  onClearContinue: () => void;
  onStarted: (run: Run) => void;
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (continuing) inputRef.current?.focus();
  }, [continuing]);
  const [prompt, setPrompt] = useState("");
  const [minutes, setMinutes] = useState<number | "">("");
  const [turns, setTurns] = useState<number | "">("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const agent = system?.agent;

  const blocked = sb.state !== "running" ? "Start the sandbox to run the agent."
    : agent && !agent.claude_token ? "No Claude token is set on the server (python -m app.set_secret claude)."
    : busyRun ? "A run is in progress in this sandbox."
    : null;

  const submit = async () => {
    if (!prompt.trim() || blocked || busy) return;
    setBusy(true);
    setError(null);
    try {
      const run = await api.startRun(sb.id, {
        prompt: prompt.trim(),
        ...(minutes ? { timeout_minutes: Number(minutes) } : {}),
        ...(turns ? { max_turns: Number(turns) } : {}),
        ...(continuing ? { continue_from: continuing.id } : {}),
      });
      setPrompt("");
      onStarted(run);
    } catch (e) {
      const err = e as ApiError;
      setError(err.reasons?.length ? err.reasons.join(" ") : err.message);
    } finally {
      setBusy(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  const defMin = agent ? Math.round(agent.run_timeout_default / 60) : 30;
  const defTurns = agent?.run_max_turns_default ?? 50;

  return (
    <div className="card composer">
      {continuing && (
        <div className="composer-continue">
          <span>
            Continuing <span className="mono">{continuing.id.slice(0, 6)}</span>: {continuing.prompt.split("\n")[0]}
          </span>
          <button type="button" className="btn btn-ghost btn-sm btn-icon" onClick={onClearContinue}
            aria-label="Start a fresh run instead" title="Start a fresh run instead">
            <Icon name="clear" size={14} />
          </button>
        </div>
      )}
      <label htmlFor="prompt" className="visually-hidden">Task for the agent</label>
      <textarea id="prompt" ref={inputRef} className="input composer-input" rows={4} maxLength={20000}
        placeholder={continuing ? "What should the agent do next?"
          : sb.repo_url ? "e.g. Add a README section describing this project" : "Describe what the agent should build or change"}
        value={prompt} onChange={(e) => setPrompt(e.target.value)} onKeyDown={onKey} disabled={!!blocked && !busyRun} />
      {advanced && (
        <div className="composer-advanced">
          <label>
            Time limit
            <input className="input mono" type="number" min={1} max={agent ? agent.run_timeout_max / 60 : 240}
              placeholder={`${defMin}`} value={minutes} onChange={(e) => setMinutes(e.target.value ? Number(e.target.value) : "")} />
            <span>min</span>
          </label>
          <label>
            Turns
            <input className="input mono" type="number" min={1} max={500} placeholder={`${defTurns}`} value={turns}
              onChange={(e) => setTurns(e.target.value ? Number(e.target.value) : "")} />
          </label>
        </div>
      )}
      {(error || blocked) && <p className={error ? "composer-error" : "composer-note"}>{error ?? blocked}</p>}
      <div className="composer-foot">
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAdvanced((a) => !a)}
          aria-expanded={advanced}>
          {minutes || defMin} min · {turns || defTurns} turns
        </button>
        <button className="btn btn-primary" onClick={submit} disabled={!prompt.trim() || !!blocked || busy}>
          {busy ? "Starting…" : "Run agent"} <Kbd>{SUBMIT_KEY}</Kbd>
        </button>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- Live run */

function useRunEvents(runId: string, onDone: () => void) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [conn, setConn] = useState<"connecting" | "live" | "closed">("connecting");
  const lastSeq = useRef(-1);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stopped = false;
    let retry = 0;
    let timer = 0;
    const connect = () => {
      setConn("connecting");
      ws = new WebSocket(runEventsUrl(runId));
      ws.onopen = () => {
        retry = 0;
        setConn("live");
      };
      ws.onmessage = (e) => {
        const batch: RunEvent[] = (JSON.parse(e.data).events ?? []).filter((ev: RunEvent) => ev.seq > lastSeq.current);
        if (!batch.length) return;
        lastSeq.current = batch[batch.length - 1].seq;
        setEvents((prev) => {
          const next = prev.concat(batch);
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
        });
        if (batch.some((ev) => ev.type === "done" || ev.type === "status")) doneRef.current();
      };
      ws.onclose = (e) => {
        setConn("closed");
        if (stopped || e.code === 1000 || e.code === 4404) return;
        if (e.code === 4401) return; // logged out; the app shows the login screen on the next request
        timer = window.setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
      };
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [runId]);

  return { events, conn };
}

function RunView({ initial, onChange, onContinue }: {
  initial: Run;
  onChange: (run: Run) => void;
  onContinue?: (run: Run) => void;
}) {
  const [run, setRun] = useState(initial);
  const [now, setNow] = useState(Date.now() / 1000);
  const [cancelling, setCancelling] = useState(false);
  const toast = useToast();
  const active = ACTIVE.includes(run.state);

  const refresh = useCallback(async () => {
    try {
      const r = await api.run(initial.id);
      setRun(r);
      onChange(r);
    } catch {
      /* transient */
    }
  }, [initial.id, onChange]);

  const { events, conn } = useRunEvents(initial.id, refresh);
  const { items: openRequests, reload: reloadRequests } = usePendingRequests(initial.id, active);

  // Usage, budget and paused time change without log events: re-read the run while it's live.
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => document.visibilityState === "visible" && refresh(), 4000);
    return () => clearInterval(t);
  }, [active, refresh]);

  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [active]);

  // Messages to the agent show as "not read yet" until the agent reads them (a `user` event);
  // it reads them in order, so message n is read once there are base + n + 1 user events.
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<string[]>([]);
  const base = useRef<number | null>(null);
  const read = events.filter((e) => e.type === "log" && e.kind === "user").length;
  const unread = sent.filter((_, n) => read < (base.current ?? 0) + n + 1);
  const sendMessage = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      await api.sendRunMessage(run.id, text);
      if (base.current === null) base.current = read;
      setSent((s) => [...s, text]);
      setDraft("");
      setFollow(true);
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const cancel = async () => {
    setCancelling(true);
    try {
      setRun(await api.cancelRun(run.id));
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setCancelling(false);
    }
  };

  // Follow the bottom unless the reader scrolled up.
  const logRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  useLayoutEffect(() => {
    const el = logRef.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [events, follow]);
  const onScroll = () => {
    const el = logRef.current;
    if (el) setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  const cost = [...events].reverse().find((e) => e.type === "cost") as Extract<RunEvent, { type: "cost" }> | undefined;
  const costUsd = run.cost_usd ?? cost?.cost_usd ?? null;
  const turnsUsed = run.num_turns ?? cost?.turns ?? null;

  return (
    <div className="card run-view">
      <header className="run-head">
        <div className="run-head-top">
          <RunBadge state={run.state} />
          {run.verify && <CheckBadge passed={run.verify === "passed"} rounds={run.verify_attempts} />}
          <span className="mono muted">{duration(run, now)}</span>
          {run.usage ? (
            <span className="mono muted" title={usageTitle(run)}>
              ≈${run.usage.cost.toFixed(2)}{run.budget_usd ? ` of $${run.budget_usd.toFixed(2)}` : ""}
            </span>
          ) : costUsd != null && <span className="mono muted" title="Estimated by Claude Code">${costUsd.toFixed(3)}</span>}
          {run.usage && run.usage.context > 0 && (
            <span className="mono muted" title={`Prompt size of the last ${run.usage.model} call`}>
              {Math.round(run.usage.context / 1000)}k context
            </span>
          )}
          {turnsUsed != null && <span className="mono muted">{turnsUsed}/{run.max_turns} turns</span>}
          <span className="spacer" />
          {conn !== "live" && active && <span className="muted" style={{ fontSize: 12 }}>reconnecting…</span>}
          {!active && onContinue && run.base_branch && run.runner === "claude-code" && (
            <button className="btn btn-sm" onClick={() => onContinue(run)}
              title="Start a new run on top of this one's branch, in the same agent session">
              Continue
            </button>
          )}
          {active && (
            <button className="btn btn-danger btn-sm" onClick={cancel} disabled={cancelling}>
              <Icon name="stop" size={14} /> {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
        </div>
        <p className="run-prompt">{run.prompt}</p>
        <div className="run-meta mono">
          {run.branch && <span>{run.branch}{run.base_branch ? ` ← ${run.base_branch}` : ""}</span>}
          {run.commit_sha && <span title={run.commit_sha}>commit {run.commit_sha.slice(0, 10)}</span>}
          {run.pushed && <span className="pushed">pushed</span>}
          <span>run {run.id}</span>
        </div>
        {run.error && !active && <div className="banner banner-danger" style={{ marginTop: 10 }}><div>{run.error}</div></div>}
      </header>
      <div className="run-log" ref={logRef} onScroll={onScroll} aria-live="polite">
        {events.map((e) => <EventLine key={e.seq} e={e} />)}
        {active && openRequests.map((r) => (
          <div key={r.id} className="ev"><RequestCard req={r} onDone={() => { reloadRequests(); refresh(); }} /></div>
        ))}
        {active && unread.map((text, i) => (
          <div key={`p${i}`} className="ev ev-user ev-user-pending">
            <span className="ev-user-label">You · not read yet</span>
            {text}
          </div>
        ))}
        {active && <div className="ev ev-pending"><span className="dot run-dot-running" /> working…</div>}
      </div>
      {!follow && (
        <button className="btn btn-sm jump" onClick={() => setFollow(true)}>Jump to latest <Icon name="arrowDown" size={14} /></button>
      )}
      {active && run.runner === "claude-code" && (
        <form className="run-steer" onSubmit={(e) => { e.preventDefault(); sendMessage(); }}>
          <textarea
            className="input run-steer-input"
            rows={1}
            maxLength={4000}
            placeholder="Message the agent while it works…"
            aria-label="Message the agent"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                sendMessage();
              }
            }}
          />
          <button className="btn btn-sm" type="submit" disabled={sending || !draft.trim()}>
            {sending ? "Sending…" : "Send"}
          </button>
        </form>
      )}
    </div>
  );
}

function usageTitle(run: Run): string {
  const u = run.usage!;
  const k = (n: number) => (n >= 10000 ? `${Math.round(n / 1000)}k` : String(n));
  return [
    `${u.requests} API calls, measured at the gateway`,
    `${k(u.input)} input, ${k(u.cache_read)} cache read, ${k(u.cache_write)} cache write, ${k(u.output)} output tokens`,
    "Cost estimated at API list prices (a subscription isn't billed per token)",
    u.unpriced ? `${u.unpriced} calls to models without a known price aren't counted` : "",
    run.paused_seconds > 1 ? `Waited ${Math.round(run.paused_seconds / 60)} min on you (not counted against the time limit)` : "",
  ].filter(Boolean).join("\n");
}

function CheckBadge({ passed, rounds }: { passed: boolean; rounds: number }) {
  const title = rounds ? `The agent was sent back ${rounds} time${rounds === 1 ? "" : "s"} by failing checks` : undefined;
  return (
    <span className={`badge ${passed ? "run-succeeded" : "run-failed"}`} title={title}>
      <Icon name={passed ? "check" : "clear"} size={12} />
      Checks {passed ? "pass" : "fail"}
    </span>
  );
}

type CheckEvent = Extract<RunEvent, { kind: "check" }>;

function CheckLine({ e }: { e: CheckEvent }) {
  const [open, setOpen] = useState(!e.passed);
  const when = e.phase === "final" ? "after the run" : `before finishing, round ${e.attempt ?? 1}`;
  const result = e.passed ? "Checks pass" : e.timed_out ? "Checks took too long" : `Checks fail (exit ${e.exit_code ?? "?"})`;
  return (
    <div className={`ev ev-check ${e.passed ? "ev-check-pass" : "ev-check-fail"}`}>
      <button className="ev-check-head" onClick={() => setOpen((o) => !o)} aria-expanded={open} disabled={!e.text}>
        <Icon name={e.passed ? "check" : "clear"} size={14} />
        <strong>{result}</strong>
        <span className="muted">{when} · {e.seconds.toFixed(1)} s</span>
      </button>
      {open && e.text && <pre>{e.text}</pre>}
    </div>
  );
}

function EventLine({ e }: { e: RunEvent }) {
  const [open, setOpen] = useState(false);
  if (e.type === "status") return null;
  if (e.type === "cost") return null;
  if (e.type === "done") {
    return (
      <div className={`ev ev-done ev-done-${e.state}`}>
        <RunBadge state={e.state} />
        <span>
          {e.commit ? <>Changes committed to <span className="mono">{e.branch}</span></> : "No commit"}
          {e.pushed && " and pushed"}
        </span>
      </div>
    );
  }
  switch (e.kind) {
    case "text":
      return <div className="ev ev-text">{e.text}</div>;
    case "result":
      return <div className="ev ev-result">{e.text}</div>;
    case "check":
      return <CheckLine e={e} />;
    case "request":
      return (
        <div className="ev ev-request">
          <Icon name="inbox" size={14} />
          <span>
            <strong>{e.request_kind === "question" ? "Asked you" : e.request_kind === "budget" ? "Over budget" : "Wants your OK"}:</strong>{" "}
            {e.title}
          </span>
        </div>
      );
    case "answer":
      return <div className="ev ev-system">{e.text}</div>;
    case "user":
      return (
        <div className="ev ev-user">
          <span className="ev-user-label">You</span>
          {e.text}
        </div>
      );
    case "tool":
      return (
        <div className="ev ev-tool">
          <span className="ev-tool-name">{e.name}</span>
          <span className="ev-tool-arg">{e.text}</span>
        </div>
      );
    case "tool_result": {
      const lines = e.text.split("\n");
      const long = lines.length > 4 || e.text.length > 400;
      return (
        <div className={`ev ev-out ${e.is_error ? "ev-out-error" : ""}`}>
          <pre>{open || !long ? e.text : lines.slice(0, 4).join("\n").slice(0, 400) + " …"}</pre>
          {long && (
            <button className="ev-more" onClick={() => setOpen((o) => !o)}>
              {open ? "Show less" : `Show all ${lines.length} lines`}
            </button>
          )}
        </div>
      );
    }
    case "error":
      return <div className="ev ev-error">{e.text}</div>;
    case "stderr":
      return <div className="ev ev-stderr">{e.text}</div>;
    case "stdout":
      return <div className="ev ev-stdout">{e.text}</div>;
    default:
      return <div className="ev ev-system">{e.text}</div>;
  }
}
