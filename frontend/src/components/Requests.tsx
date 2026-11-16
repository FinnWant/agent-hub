import { useCallback, useEffect, useState } from "react";
import { api, type OwnerRequest } from "../lib/api";
import { ago } from "../lib/format";
import { Icon, useToast } from "./ui";

/** Fired by the hub events stream when a run needs the owner; lists refresh on it. */
export const NEEDS_YOU_EVENT = "kiln:needs-you";

/** Open requests, kept fresh: polled, and refreshed at once on a needs-you event. */
export function usePendingRequests(run?: string, enabled = true) {
  const [items, setItems] = useState<OwnerRequest[]>([]);
  const reload = useCallback(async () => {
    try {
      setItems(await api.requests({ state: "pending", ...(run ? { run } : {}) }));
    } catch {
      /* transient */
    }
  }, [run]);
  useEffect(() => {
    if (!enabled) {
      setItems([]);
      return;
    }
    reload();
    const t = setInterval(() => document.visibilityState === "visible" && reload(), 5000);
    window.addEventListener(NEEDS_YOU_EVENT, reload);
    return () => {
      clearInterval(t);
      window.removeEventListener(NEEDS_YOU_EVENT, reload);
    };
  }, [reload, enabled]);
  return { items, reload };
}

const KIND_LABEL: Record<OwnerRequest["kind"], string> = {
  approval: "Approval",
  question: "Question",
  budget: "Budget",
  stuck: "Stuck",
};

function minutesLeft(deadline: number | null): string | null {
  if (!deadline) return null;
  const m = Math.max(0, Math.round((deadline - Date.now() / 1000) / 60));
  return m < 1 ? "under a minute left" : `${m} min left`;
}

export function RequestCard({ req, onDone, showSandbox = false }: {
  req: OwnerRequest;
  onDone: () => void;
  showSandbox?: boolean;
}) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [amount, setAmount] = useState<number | "">(req.data.budget ?? 5);
  const [busy, setBusy] = useState(false);
  const pending = req.state === "pending";

  const send = async (body: { choice?: string; text?: string; amount?: number }) => {
    setBusy(true);
    try {
      await api.answerRequest(req.id, body);
      onDone();
      window.dispatchEvent(new Event(NEEDS_YOU_EVENT));
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const answered = req.answer && (req.answer.text ?? req.answer.choice ?? "")
    + (req.answer.choice === "extend" && req.answer.amount ? ` by $${req.answer.amount.toFixed(2)}` : "");

  return (
    <div className={`request-card request-${req.kind} ${pending ? "is-pending" : ""}`}>
      <div className="request-head">
        <span className="request-kind">{KIND_LABEL[req.kind]}</span>
        {showSandbox && req.sandbox_name && (
          <a className="mono request-where" href={`/sandboxes/${req.sandbox_id}`}>{req.sandbox_name}</a>
        )}
        <span className="muted request-when">
          {ago(req.created_at)}{pending && minutesLeft(req.deadline) ? ` · ${minutesLeft(req.deadline)}` : ""}
        </span>
      </div>
      <p className="request-body">{req.body}</p>

      {!pending && (
        <p className="request-result muted">
          {req.state === "answered" ? `Answered: ${answered}` : req.state === "expired" ? "No answer in time" : "Closed with its run"}
        </p>
      )}

      {pending && req.kind === "approval" && (
        <div className="request-actions">
          <input className="input" placeholder="Reason, if you deny (optional)" maxLength={4000}
            value={text} onChange={(e) => setText(e.target.value)} />
          <button className="btn btn-danger" disabled={busy} onClick={() => send({ choice: "deny", text })}>Deny</button>
          <button className="btn btn-primary" disabled={busy} onClick={() => send({ choice: "allow" })}>Allow</button>
        </div>
      )}

      {pending && req.kind === "question" && (
        <>
          {req.options.length > 0 && (
            <div className="request-options">
              {req.options.map((o) => (
                <button key={o} className="btn btn-sm" disabled={busy} onClick={() => send({ choice: o, text: o })}>{o}</button>
              ))}
            </div>
          )}
          <form className="request-actions" onSubmit={(e) => { e.preventDefault(); if (text.trim()) send({ text }); }}>
            <input className="input" placeholder="Your answer" maxLength={4000} value={text}
              onChange={(e) => setText(e.target.value)} />
            <button className="btn btn-primary" type="submit" disabled={busy || !text.trim()}>Answer</button>
          </form>
        </>
      )}

      {pending && req.kind === "stuck" && (
        <div className="request-actions">
          <input className="input" placeholder="Advice for the agent (optional)" maxLength={4000}
            value={text} onChange={(e) => setText(e.target.value)} />
          <button className="btn btn-danger" disabled={busy} onClick={() => send({ choice: "stop" })}>Stop the run</button>
          <button className="btn btn-primary" disabled={busy} onClick={() => send({ choice: "continue", text })}>Continue</button>
        </div>
      )}

      {pending && req.kind === "budget" && (
        <div className="request-actions">
          <label className="request-amount">
            <span>$</span>
            <input className="input mono" type="number" min={0.01} step={0.5} value={amount}
              onChange={(e) => setAmount(e.target.value ? Number(e.target.value) : "")} aria-label="Extend by (USD)" />
          </label>
          <button className="btn btn-danger" disabled={busy} onClick={() => send({ choice: "stop" })}>Stop the run</button>
          <button className="btn btn-primary" disabled={busy || !amount} onClick={() => send({ choice: "extend", amount: Number(amount) })}>
            Extend
          </button>
        </div>
      )}
    </div>
  );
}

/** The "Needs you" page: everything waiting on the owner, across sandboxes. */
export function NeedsYou() {
  const { items, reload } = usePendingRequests();
  const [recent, setRecent] = useState<OwnerRequest[]>([]);
  const loadRecent = useCallback(() => {
    api.requests({ limit: 30 }).then((rs) => setRecent(rs.filter((r) => r.state !== "pending"))).catch(() => {});
  }, []);
  useEffect(loadRecent, [loadRecent, items.length]);
  const done = () => {
    reload();
    loadRecent();
  };
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Needs you</h1>
          <p>Approvals, questions and budgets from running agents. The run's clock stops while it waits.</p>
        </div>
      </div>
      {items.length === 0 ? (
        <div className="card empty">
          <Icon name="inbox" size={20} />
          <h2>Nothing waiting</h2>
          <p>When an agent asks something, wants to do something your rules flag, or reaches its budget, it shows up here.</p>
        </div>
      ) : (
        <div className="request-list">
          {items.map((r) => <RequestCard key={r.id} req={r} onDone={done} showSandbox />)}
        </div>
      )}
      {recent.length > 0 && (
        <>
          <div className="section-title">Recent<span className="line" /></div>
          <div className="request-list">
            {recent.map((r) => <RequestCard key={r.id} req={r} onDone={done} showSandbox />)}
          </div>
        </>
      )}
    </div>
  );
}
