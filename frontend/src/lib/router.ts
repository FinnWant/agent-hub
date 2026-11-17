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

/** Each in-app history entry remembers the page it was opened from, so pages can offer "Back to …". */
type EntryState = { prev?: string };

const here = () => location.pathname + location.search;

/** Go to `path` (may include a query). Scrolls to the top only when the page itself changes. */
export function navigate(path: string, replace = false) {
  if (path === here()) return;
  const samePage = new URL(path, location.href).pathname === location.pathname;
  const state: EntryState = replace ? { prev: previousPath() ?? undefined } : { prev: here() };
  history[replace ? "replaceState" : "pushState"](state, "", path);
  listeners.forEach((fn) => fn());
  if (!samePage) window.scrollTo(0, 0);
}

/** Where the current entry was opened from, within the app (null after a fresh load or a typed URL). */
export function previousPath(): string | null {
  return (history.state as EntryState | null)?.prev ?? null;
}

/** Back to the page this one was opened from; `fallback` when there is none (opened directly). */
export function goBack(fallback: string) {
  if (previousPath()) history.back();
  else navigate(fallback);
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
