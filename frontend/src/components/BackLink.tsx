import { goBack, match, previousPath } from "../lib/router";
import { Icon } from "./ui";

const TAB_LABEL: Record<string, string> = {
  runs: "Runs", changes: "Changes", files: "Files", terminal: "Terminal", preview: "Preview", settings: "Settings",
};

/** A short name for an in-app path, said from the page at `from`. */
function label(path: string, from: string): string {
  const url = new URL(path, location.href);
  const r = match(url.pathname);
  const here = match(from);
  if (r.name === "home") return "Sandboxes";
  if (r.name === "needsYou") return "Needs you";
  if (r.name === "audit") return "Audit log";
  if (r.name === "sandbox") {
    if (here.name !== "sandbox" || here.id !== r.id) return "the sandbox";
    const tab = r.tab ?? "runs";
    const sameTab = (here.tab ?? "runs") === tab;
    if (sameTab && url.searchParams.get("run")) return "the previous run";
    if (sameTab && url.searchParams.get("file")) return "the previous file";
    return TAB_LABEL[tab] ?? "the previous page";
  }
  return "the previous page";
}

/**
 * "Back to …": returns to the page this one was opened from (Needs you, the audit log, another tab,
 * the run you were reading), not always the sandbox list. With no in-app history (opened from a
 * link or a notification) it goes to `fallback`. Visible in the installed app, which has no browser
 * back button.
 */
export function BackLink({ fallback = "/", fallbackLabel = "Sandboxes" }: { fallback?: string; fallbackLabel?: string }) {
  const prev = previousPath();
  const text = prev ? label(prev, location.pathname) : fallbackLabel;
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

/** For pages in the main navigation: a back link only when they were opened from another page. */
export function PageBack() {
  if (!previousPath()) return null;
  return <nav className="crumbs" aria-label="Breadcrumb"><BackLink /></nav>;
}
