import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { SandboxState } from "../lib/api";

/* ---------------------------------------------------------------- Status */

const STATE_LABEL: Record<SandboxState, string> = {
  creating: "Creating",
  running: "Running",
  stopped: "Stopped",
  error: "Error",
  destroyed: "Destroyed",
};

export function StatusDot({ state }: { state: SandboxState }) {
  return <span className={`dot dot-${state}`} aria-hidden />;
}

export function StatusBadge({ state }: { state: SandboxState }) {
  return (
    <span className={`badge badge-${state}`}>
      <StatusDot state={state} />
      {STATE_LABEL[state]}
    </span>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

/* ---------------------------------------------------------------- Dialog */

export function Dialog({
  title,
  description,
  onClose,
  children,
  footer,
  labelledBy = "dialog-title",
}: {
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer: ReactNode;
  labelledBy?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>("[autofocus], input, select, textarea, button");
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && ref.current) {
        // keep focus inside the dialog
        const items = ref.current.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), select, textarea, [tabindex]:not([tabindex='-1'])",
        );
        if (!items.length) return;
        const [a, z] = [items[0], items[items.length - 1]];
        if (e.shiftKey && document.activeElement === a) {
          e.preventDefault();
          z.focus();
        } else if (!e.shiftKey && document.activeElement === z) {
          e.preventDefault();
          a.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [onClose]);

  return (
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={labelledBy} ref={ref}>
        <div className="dialog-head">
          <h2 id={labelledBy}>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        <div className="dialog-body">{children}</div>
        <div className="dialog-foot">{footer}</div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- Toasts */

interface Toast {
  id: number;
  kind: "error" | "success" | "info";
  text: string;
}

const ToastCtx = createContext<(kind: Toast["kind"], text: string) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-3), { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 8000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            <span className="dot" aria-hidden />
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

/* ---------------------------------------------------------------- Icons (inline, 16px, stroke) */

const paths = {
  plus: "M8 3.5v9M3.5 8h9",
  terminal: "M3 4.5l3.5 3.5L3 11.5M8 12h5",
  play: "M5 3.5v9l7-4.5z",
  stop: "M4.5 4.5h7v7h-7z",
  trash: "M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5",
  back: "M9.5 3.5L5 8l4.5 4.5",
  box: "M8 2l5.5 3v6L8 14l-5.5-3V5zM2.5 5L8 8l5.5-3M8 8v6",
  list: "M5.5 4.5h8M5.5 8h8M5.5 11.5h8M2.5 4.5h.5M2.5 8h.5M2.5 11.5h.5",
  logout: "M6 3H3.5v10H6M10 5l3 3-3 3M13 8H6",
  refresh: "M12.5 6A5 5 0 1 0 13 9M12.5 2.5V6H9",
  clear: "M4 4l8 8M12 4l-8 8",
  bell: "M4 11.5V7a4 4 0 0 1 8 0v4.5l1 1H3zM6.5 13.5a1.5 1.5 0 0 0 3 0",
  external: "M9.5 3H13v3.5M13 3L7.5 8.5M11.5 9.5V13h-8.5V4.5H6.5",
  broom: "M10.5 2.5L7 8M4 8.5h6l1 5H3zM6 11v2.5M8 11v2.5",
  chevron: "M6 4l4 4-4 4",
  check: "M3.5 8.5l3 3 6-7",
  arrowDown: "M8 3v10M4 9l4 4 4-4",
};

export function Icon({ name, size = 16 }: { name: keyof typeof paths; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d={paths[name]} />
    </svg>
  );
}
