import { useEffect, useState } from "react";
import { PageBack } from "../components/BackLink";
import { api, type AuditEntry } from "../lib/api";
import { clock } from "../lib/format";
import { useTitle } from "../lib/router";
import { Icon } from "../components/ui";

const FILTERS = ["all", "sandbox", "terminal", "auth"] as const;
type Filter = (typeof FILTERS)[number];
const PAGE = 100;

function summary(e: AuditEntry): string {
  const d = e.detail;
  if (!d) return "";
  if (e.action === "terminal.close") return `${d.seconds}s · ${d.reason}`;
  if (e.action === "terminal.open" && d.cols) return `${d.cols}×${d.rows}`;
  if (e.action === "sandbox.exec") return `${d.cmd ?? ""}${d.exit_code !== undefined ? ` → ${d.exit_code}` : ""}`;
  if (Array.isArray(d.blocked)) return (d.blocked as string[]).join(" ");
  if (d.error) return String(d.error);
  if (d.name && d.template) return `${d.name} · ${d.template}`;
  if (d.name) return String(d.name);
  return JSON.stringify(d);
}

export default function Audit() {
  useTitle("Audit log");
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [more, setMore] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);

  const load = async (before?: number) => {
    try {
      const page = await api.audit(before, PAGE);
      setEntries((prev) => (before && prev ? [...prev, ...page] : page));
      setMore(page.length === PAGE);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(() => document.visibilityState === "visible" && refreshHead(), 10000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Prepend new entries without losing pages already loaded.
  const refreshHead = async () => {
    const page = await api.audit(undefined, 50).catch(() => null);
    if (!page) return;
    setEntries((prev) => {
      if (!prev) return page;
      const top = prev[0]?.id ?? 0;
      return [...page.filter((e) => e.id > top), ...prev];
    });
  };

  const shown = (entries ?? []).filter((e) => filter === "all" || e.action.startsWith(`${filter}.`));

  return (
    <div className="page">
      <PageBack />
      <header className="page-head">
        <div>
          <h1>Audit log</h1>
          <p>Every login, sandbox action and terminal session, newest first.</p>
        </div>
        <div className="segmented compact" role="radiogroup" aria-label="Filter">
          {FILTERS.map((f) => (
            <button key={f} type="button" role="radio" aria-checked={filter === f} onClick={() => setFilter(f)}>
              {f}
            </button>
          ))}
        </div>
      </header>
      {error && <div className="banner banner-danger" style={{ marginBottom: 16 }}><strong>Couldn't load.</strong> {error}</div>}
      <div className="card" style={{ overflow: "hidden" }}>
        <div className="table-scroll">
          <table className="audit-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>Sandbox</th>
                <th>Detail</th>
                <th>From</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((e) => (
                <tr key={e.id}>
                  <td className="when">{clock(e.ts)}</td>
                  <td className={`action ${e.ok ? "" : "fail"}`}>
                    {e.action}
                    {!e.ok && <Icon name="clear" size={12} />}
                  </td>
                  <td className="mono">{e.target ? <a href={`/sandboxes/${e.target}`}>{e.target}</a> : <span className="muted">—</span>}</td>
                  <td className="detail">{summary(e)}</td>
                  <td className="mono muted">{e.ip}</td>
                </tr>
              ))}
              {entries && !shown.length && (
                <tr><td colSpan={5} className="muted" style={{ padding: 24, textAlign: "center" }}>Nothing yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {more && (
          <div className="load-more">
            <button className="btn btn-sm" onClick={() => load(entries?.[entries.length - 1]?.id)}>Load older</button>
          </div>
        )}
      </div>
    </div>
  );
}
