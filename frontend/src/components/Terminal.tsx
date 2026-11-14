import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal as XTerm, type ITheme } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { terminalUrl } from "../lib/api";
import { Icon } from "./ui";

// Kiln terminal palette: slate ground, ember cursor, status hues for ANSI colours.
const THEME: ITheme = {
  background: "#06080d",
  foreground: "#e6eaf2",
  cursor: "#ff5e14",
  cursorAccent: "#06080d",
  selectionBackground: "rgba(255, 94, 20, 0.28)",
  scrollbarSliderBackground: "rgba(255, 255, 255, 0.08)",
  scrollbarSliderHoverBackground: "rgba(255, 255, 255, 0.14)",
  scrollbarSliderActiveBackground: "rgba(255, 255, 255, 0.2)",
  black: "#1d2432",
  red: "#ff6b70",
  green: "#4ade9a",
  yellow: "#fbc54f",
  blue: "#6fa0ff",
  magenta: "#a88bfa",
  cyan: "#5ec8d8",
  white: "#cdd5e1",
  brightBlack: "#5e6c83",
  brightRed: "#ff8a8e",
  brightGreen: "#7ee8b5",
  brightYellow: "#ffd77a",
  brightBlue: "#93b8ff",
  brightMagenta: "#c2adff",
  brightCyan: "#8adce8",
  brightWhite: "#f6f8fb",
};

type Status =
  | { kind: "connecting" }
  | { kind: "open" }
  | { kind: "closed"; message: string; canRetry: boolean };

function closeMessage(code: number, reason: string): { message: string; canRetry: boolean } {
  switch (code) {
    case 1000:
      return { message: "The shell exited.", canRetry: true };
    case 4401:
      return { message: "Your session has expired. Reload the page and log in again.", canRetry: false };
    case 4404:
      return { message: "This sandbox no longer exists.", canRetry: false };
    case 4408:
      return { message: "Closed after an hour without input.", canRetry: true };
    case 4409:
      return { message: reason || "The sandbox isn't running.", canRetry: true };
    case 4502:
      return { message: "Docker couldn't open a shell in this sandbox.", canRetry: true };
    default:
      return { message: "Connection lost.", canRetry: true };
  }
}

export default function Terminal({ sandboxId, running }: { sandboxId: string; running: boolean }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<Status>({ kind: "connecting" });
  const [size, setSize] = useState<[number, number]>([0, 0]);

  const connect = useCallback(() => {
    const term = termRef.current;
    if (!term) return;
    wsRef.current?.close();
    setStatus({ kind: "connecting" });
    const ws = new WebSocket(terminalUrl(sandboxId, term.cols, term.rows));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    let opened = false;
    ws.onopen = () => {
      opened = true;
      setStatus({ kind: "open" });
      term.focus();
    };
    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data));
    };
    ws.onclose = (e) => {
      if (wsRef.current !== ws) return; // superseded by a reconnect
      // A refused handshake (not logged in / wrong origin) surfaces as 1006 before open.
      const code = !opened && e.code === 1006 ? 4401 : e.code;
      setStatus({ kind: "closed", ...closeMessage(code, e.reason) });
      if (opened) term.write("\r\n\x1b[38;5;242m── session ended ──\x1b[0m\r\n");
    };
  }, [sandboxId]);

  // Create the terminal once per sandbox.
  useEffect(() => {
    const term = new XTerm({
      theme: THEME,
      fontFamily: '"Geist Mono Variable", ui-monospace, Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      cursorBlink: true,
      cursorStyle: "bar",
      scrollback: 5000, // caps the in-browser buffer
      allowProposedApi: false,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current!);
    termRef.current = term;
    fitRef.current = fit;

    const enc = new TextEncoder();
    const send = (data: Uint8Array<ArrayBuffer>) => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(data);
    };
    const onData = term.onData((s) => send(enc.encode(s)));
    const onBinary = term.onBinary((s) => send(Uint8Array.from(s, (c) => c.charCodeAt(0))));

    let resizeTimer = 0;
    const onResize = term.onResize(({ cols, rows }) => {
      setSize([cols, rows]);
      clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols, rows }));
      }, 80);
    });

    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* hidden */
      }
    });
    ro.observe(hostRef.current!);
    // Fonts load asynchronously; refit once they're ready so cell metrics are right.
    document.fonts?.ready.then(() => fit.fit());
    fit.fit();
    setSize([term.cols, term.rows]);

    return () => {
      clearTimeout(resizeTimer);
      ro.disconnect();
      onData.dispose();
      onBinary.dispose();
      onResize.dispose();
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
      term.dispose();
      termRef.current = null;
    };
  }, [sandboxId]);

  // (Re)connect when the sandbox is running.
  useEffect(() => {
    if (running) connect();
    else {
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
      setStatus({ kind: "closed", message: "Start the sandbox to open a shell.", canRetry: false });
    }
  }, [running, connect]);

  const dotClass = status.kind === "open" ? "dot-running" : status.kind === "connecting" ? "dot-creating" : "dot-stopped";
  const label = status.kind === "open" ? "Connected" : status.kind === "connecting" ? "Connecting…" : "Disconnected";

  return (
    <div className="term">
      <div className="term-bar">
        <span className={`dot ${dotClass}`} aria-hidden />
        <span>{label}</span>
        <span className="mono">agent@/workspace</span>
        <span className="spacer" />
        <span className="mono" aria-label="Terminal size">{size[0]}×{size[1]}</span>
        <button className="btn btn-ghost btn-sm btn-icon" title="Clear screen" aria-label="Clear screen"
          onClick={() => {
            termRef.current?.clear();
            termRef.current?.focus();
          }}>
          <Icon name="clear" />
        </button>
        <button className="btn btn-ghost btn-sm btn-icon" title="Reconnect" aria-label="Reconnect"
          disabled={!running} onClick={connect}>
          <Icon name="refresh" />
        </button>
      </div>
      <div className="term-screen">
        <div ref={hostRef} style={{ height: "100%" }} />
        {status.kind === "closed" && (
          <div className="term-overlay">
            <div>
              <p>{status.message}</p>
              {status.canRetry && running && (
                <button className="btn btn-primary" onClick={connect}>
                  <Icon name="terminal" /> New shell
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
