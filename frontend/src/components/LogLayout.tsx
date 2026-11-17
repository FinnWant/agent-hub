import { useCallback, useState, type KeyboardEvent, type PointerEvent, type RefObject } from "react";
import { Icon } from "./ui";

/** How the run log is laid out, remembered per browser (a convenience; defaults when unavailable). */
export type LogPrefs = {
  head: number | null; // px the run's header may take above the log (wide screens); null = default
  zoom: number;        // text size of the log
  max: boolean;        // log expanded: the header folds to its top row (full screen on a phone)
};

const KEY = "kiln.runLog";
const DEFAULTS: LogPrefs = { head: null, zoom: 1, max: false };
export const ZOOMS = [0.85, 1, 1.15, 1.3, 1.5, 1.75];

function load(): LogPrefs {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    return {
      head: typeof p.head === "number" ? p.head : null,
      zoom: ZOOMS.includes(p.zoom) ? p.zoom : 1,
      // Expanded is full screen on a phone: don't reopen every run that way.
      max: p.max === true && !window.matchMedia?.("(max-width: 900px)").matches,
    };
  } catch {
    return DEFAULTS;
  }
}

export function useLogPrefs(): [LogPrefs, (patch: Partial<LogPrefs>) => void] {
  const [prefs, setPrefs] = useState(load);
  const update = useCallback((patch: Partial<LogPrefs>) => {
    setPrefs((old) => {
      const next = { ...old, ...patch };
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
      } catch {
        /* private mode: keep it for this page only */
      }
      return next;
    });
  }, []);
  return [prefs, update];
}

const MIN_HEAD = 56;  // the header's top row (state, cost, Cancel) stays visible
const MIN_LOG = 120;

/**
 * The bar between a run's header and its log: drag it (or use the arrow keys) to give the log more
 * or less room, change the log's text size, or expand the log. Double-click resets the split.
 */
export function LogBar({ prefs, update, viewRef, headRef }: {
  prefs: LogPrefs;
  update: (patch: Partial<LogPrefs>) => void;
  viewRef: RefObject<HTMLDivElement | null>;
  headRef: RefObject<HTMLElement | null>;
}) {
  const clampHead = (px: number) => {
    const view = viewRef.current?.clientHeight ?? 800;
    return Math.round(Math.max(MIN_HEAD, Math.min(px, view - MIN_LOG)));
  };
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || prefs.max) return;
    const head = headRef.current;
    if (!head) return;
    e.preventDefault();
    const startY = e.clientY;
    const startH = head.offsetHeight;
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    document.body.classList.add("is-resizing");
    const move = (ev: globalThis.PointerEvent) => update({ head: clampHead(startH + ev.clientY - startY) });
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      document.body.classList.remove("is-resizing");
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const head = headRef.current?.offsetHeight ?? 200;
    const step = e.shiftKey ? 96 : 24;
    if (e.key === "ArrowUp") update({ head: clampHead(head - step) });
    else if (e.key === "ArrowDown") update({ head: clampHead(head + step) });
    else if (e.key === "Home") update({ head: MIN_HEAD });
    else return;
    e.preventDefault();
  };
  const z = ZOOMS.indexOf(prefs.zoom);
  return (
    <div className="log-bar">
      {!prefs.max && (
        <div className="log-grip" role="separator" aria-orientation="horizontal" tabIndex={0}
          aria-label="Resize the log (arrow keys; double-click to reset)" title="Drag to resize · double-click to reset"
          onPointerDown={onPointerDown} onKeyDown={onKey} onDoubleClick={() => update({ head: null })}>
          <span aria-hidden />
        </div>
      )}
      <span className="log-bar-label">Log</span>
      <span className="spacer" />
      <div className="log-bar-tools">
        <button type="button" className="btn btn-ghost btn-xs" disabled={z <= 0} aria-label="Smaller text"
          title="Smaller text" onClick={() => update({ zoom: ZOOMS[z - 1] })}>A−</button>
        <button type="button" className="btn btn-ghost btn-xs mono log-zoom" title="Reset text size"
          disabled={prefs.zoom === 1} onClick={() => update({ zoom: 1 })}>{Math.round(prefs.zoom * 100)}%</button>
        <button type="button" className="btn btn-ghost btn-xs" disabled={z >= ZOOMS.length - 1} aria-label="Larger text"
          title="Larger text" onClick={() => update({ zoom: ZOOMS[z + 1] })}>A+</button>
        <button type="button" className="btn btn-ghost btn-xs" aria-pressed={prefs.max}
          title={prefs.max ? "Show the run details again" : "Give the log the whole space"}
          onClick={() => update({ max: !prefs.max })}>
          <Icon name={prefs.max ? "collapse" : "expand"} size={14} /> {prefs.max ? "Details" : "Expand"}
        </button>
      </div>
    </div>
  );
}
