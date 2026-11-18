// A tiny History-API router: the app has four routes, which doesn't justify a dependency.
import { useEffect, useSyncExternalStore } from "react";

const listeners = new Set<() => void>();

function subscribe(fn: () => void) {
  listeners.add(fn);
  window.addEventListener("popstate", fn);
  return () => {
    listeners.delete(fn);
    window.removeEventListener("popstate", fn);
  };
}

/**
 * Each in-app history entry remembers the pillar page (one in the main navigation: Sandboxes,
 * Needs you, the audit log) it was reached from, and how deep in the history that pillar's entry
 * is. "Back" then returns to the pillar in one step, however many tabs, runs and files were
 * opened since, instead of retracing each of them.
 */
type EntryState = { prev?: string; pillar?: string; pillarDepth?: number; depth?: number };

const here = () => location.pathname + location.search;
const entry = (): EntryState => (history.state as EntryState | null) ?? {};

export function isPillar(path: string): boolean {
  const name = match(new URL(path, location.href).pathname).name;
  return name === "home" || name === "audit" || name === "needsYou";
}

/** Go to `path` (may include a query). Scrolls to the top only when the page itself changes. */
export function navigate(path: string, replace = false) {
  if (path === here()) return;
  const target = new URL(path, location.href).pathname;
  const samePage = target === location.pathname;
  const cur = entry();
  let state: EntryState;
  if (replace) {
    state = { ...cur };
  } else {
    const depth = cur.depth ?? 0;
    // Leaving a pillar: it becomes the way back. Anywhere else: keep the way back we came with.
    const leaving = isPillar(here()) && !samePage;
    let pillar = leaving ? here() : cur.pillar;
    let pillarDepth = leaving ? depth : cur.pillarDepth;
    if (pillar && new URL(pillar, location.href).pathname === target) {
      pillar = undefined; // arrived at that pillar again: nothing behind it to go back to
      pillarDepth = undefined;
    }
    state = { prev: here(), pillar, pillarDepth, depth: depth + 1 };
  }
  history[replace ? "replaceState" : "pushState"](state, "", path);
  listeners.forEach((fn) => fn());
  if (!samePage) window.scrollTo(0, 0);
}

/** The pillar page the current entry was reached from (null after a fresh load or a typed URL). */
export function pillarPath(): string | null {
  return entry().pillar ?? null;
}

/** Back to the pillar this page was reached from, in one step; `fallback` when there is none. */
export function goBack(fallback: string) {
  const { pillar, pillarDepth, depth = 0 } = entry();
  if (pillar && pillarDepth != null && pillarDepth < depth) history.go(pillarDepth - depth);
  else navigate(pillar ?? fallback);
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, () => location.pathname);
}

/** The query string's parameters; re-renders when they change. */
export function useQuery(): URLSearchParams {
  const search = useSyncExternalStore(subscribe, () => location.search);
  return new URLSearchParams(search);
}

/** The current URL with some query parameters set (null or "" removes one). */
export function withQuery(params: Record<string, string | null>, path = location.pathname): string {
  const q = new URLSearchParams(location.search);
  if (path !== location.pathname) q.forEach((_, k) => q.delete(k));
  for (const [k, v] of Object.entries(params)) {
    if (v) q.set(k, v);
    else q.delete(k);
  }
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

export type Route =
  | { name: "home" }
  | { name: "sandbox"; id: string; tab: string | null }
  | { name: "audit" }
  | { name: "needsYou" }
  | { name: "notFound" };

export function match(path: string): Route {
  if (path === "/" || path === "/sandboxes") return { name: "home" };
  if (path === "/audit") return { name: "audit" };
  if (path === "/needs-you") return { name: "needsYou" };
  const m = path.match(/^\/sandboxes\/([A-Za-z0-9]+)(?:\/([a-z]+))?\/?$/);
  if (m) return { name: "sandbox", id: m[1], tab: m[2] ?? null };
  return { name: "notFound" };
}

/** Intercept plain left-clicks on same-origin <a href="/..."> so they route client-side. */
export function useLinkInterception() {
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = (e.target as Element).closest("a");
      if (!a || a.target || a.hasAttribute("download")) return;
      const url = new URL(a.href, location.href);
      if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
      e.preventDefault();
      navigate(url.pathname + url.search);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);
}

export function useTitle(title: string) {
  useEffect(() => {
    document.title = title ? `${title} · Kiln` : "Kiln";
  }, [title]);
}
