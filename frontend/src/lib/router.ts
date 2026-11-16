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

export function navigate(path: string, replace = false) {
  if (path === location.pathname) return;
  history[replace ? "replaceState" : "pushState"](null, "", path);
  listeners.forEach((fn) => fn());
  window.scrollTo(0, 0);
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, () => location.pathname);
}

export type Route =
  | { name: "home" }
  | { name: "sandbox"; id: string }
  | { name: "audit" }
  | { name: "needsYou" }
  | { name: "notFound" };

export function match(path: string): Route {
  if (path === "/" || path === "/sandboxes") return { name: "home" };
  if (path === "/audit") return { name: "audit" };
  if (path === "/needs-you") return { name: "needsYou" };
  const m = path.match(/^\/sandboxes\/([A-Za-z0-9]+)\/?$/);
  if (m) return { name: "sandbox", id: m[1] };
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
      navigate(url.pathname);
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
