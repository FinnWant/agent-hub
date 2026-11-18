import { goBack, match, pillarPath } from "../lib/router";
import { Icon } from "./ui";

/** The name of a pillar page. */
function label(path: string): string {
  const r = match(new URL(path, location.href).pathname);
  if (r.name === "needsYou") return "Needs you";
  if (r.name === "audit") return "Audit log";
  return "Sandboxes";
}

/**
 * "Back to …": returns to the pillar page this one was reached from (Sandboxes, Needs you, the
 * audit log) in one step, skipping the tabs, runs and files opened since. With no in-app history
 * (opened from a link or a notification) it goes to `fallback`. Visible in the installed app,
 * which has no browser back button.
 */
export function BackLink({ fallback = "/", fallbackLabel = "Sandboxes" }: { fallback?: string; fallbackLabel?: string }) {
  const prev = pillarPath();
  const text = prev ? label(prev) : fallbackLabel;
  return (
    <a href={prev ?? fallback} className="back-link"
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        goBack(fallback);
      }}>
      <Icon name="back" size={14} /> {prev ? `Back to ${text}` : text}
    </a>
  );
}

/** For pillar pages: a back link only when they were reached from another pillar. */
export function PageBack() {
  if (!pillarPath()) return null;
  return <nav className="crumbs" aria-label="Breadcrumb"><BackLink /></nav>;
}
