import { useEffect, useRef, useState } from "react";
import { api, hubEventsUrl, type HubEvent, type PushInfo } from "../lib/api";
import { ago } from "../lib/format";
import {
  currentSubscription,
  deviceLabel,
  deviceMode,
  disablePush,
  enablePush,
  nativeNotify,
} from "../lib/notifications";
import { Dialog, useToast } from "./ui";

/** App-wide: listen for notification events; toast them and, in the Windows app, notify natively. */
export function useHubEvents() {
  const toast = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stopped = false;
    let retry = 0;
    let timer = 0;
    const connect = () => {
      ws = new WebSocket(hubEventsUrl());
      ws.onopen = () => (retry = 0);
      ws.onmessage = (e) => {
        for (const ev of (JSON.parse(e.data).events ?? []) as HubEvent[]) {
          toastRef.current(ev.kind === "run.finished" || ev.kind === "test" ? "success" : "error", `${ev.title}: ${ev.body}`);
          nativeNotify(ev.title, ev.body);
        }
      };
      ws.onclose = (e) => {
        if (stopped || e.code === 4401) return;
        timer = window.setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
      };
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, []);
}

export default function NotificationsDialog({ onClose }: { onClose: () => void }) {
  const mode = deviceMode();
  const [info, setInfo] = useState<PushInfo | null>(null);
  const [subscribed, setSubscribed] = useState<string | null>(null); // this device's endpoint
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const load = async () => {
    try {
      setInfo(await api.push());
      setSubscribed((await currentSubscription())?.endpoint ?? null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const thisDeviceOn = !!subscribed && !!info?.devices.some((d) => d.endpoint === subscribed);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      if (done) toast("success", done);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const test = () =>
    act(async () => {
      const r = await api.pushTest();
      if (mode !== "tauri") toast("success", `Test sent to ${r.sent} device${r.sent === 1 ? "" : "s"}`);
    });

  return (
    <Dialog
      title="Notifications"
      description="When a run finishes, fails or times out, or a sandbox stops on its own."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Close</button>
          <button className="btn" disabled={busy} onClick={test}>Send a test</button>
        </>
      }
    >
      {error && <div className="banner banner-danger" role="alert"><div>{error}</div></div>}

      {mode === "tauri" && (
        <div className="banner banner-info">
          <div>
            <strong>This Windows app notifies you natively</strong> while it's running. Closing the window keeps it in
            the system tray, so notifications keep coming; quit from the tray icon.
          </div>
        </div>
      )}

      {mode === "ios-install" && (
        <div className="install-steps">
          <strong>Add Kiln to your Home Screen first</strong>
          <p className="muted">iPhone only allows notifications for web apps opened from the Home Screen.</p>
          <ol>
            <li>Tap the <b>Share</b> button in Safari.</li>
            <li>Choose <b>Add to Home Screen</b>, then <b>Add</b>.</li>
            <li>Open <b>Kiln</b> from your Home Screen and log in.</li>
            <li>Come back here (bell icon) and tap <b>Turn on</b>.</li>
          </ol>
        </div>
      )}

      {mode === "insecure" && (
        <div className="banner banner-warning">
          <div>
            <strong>Push notifications need HTTPS.</strong> Open Kiln at its https:// address (set up by the
            installer with <span className="mono">tailscale serve</span>).
          </div>
        </div>
      )}

      {mode === "unsupported" && <p className="muted">This browser doesn't support push notifications.</p>}

      {mode === "push" && info && (
        <div className="toggle-row card" style={{ padding: "14px 16px" }}>
          <div>
            <strong>This device · {deviceLabel()}</strong>
            <p>
              {thisDeviceOn ? "Notifications are on."
                : info.available ? "Get notified even when Kiln isn't open."
                : "Not available until the server runs on HTTPS (re-run install.sh after enabling HTTPS in Tailscale)."}
            </p>
          </div>
          {thisDeviceOn ? (
            <button className="btn btn-sm" disabled={busy} onClick={() => act(disablePush, "Notifications turned off")}>
              Turn off
            </button>
          ) : (
            <button className="btn btn-primary btn-sm" disabled={busy || !info.available || !info.public_key}
              onClick={() => act(() => enablePush(info.public_key!), "Notifications turned on")}>
              Turn on
            </button>
          )}
        </div>
      )}

      {info && info.devices.length > 0 && (
        <div>
          <div className="field-label" style={{ marginBottom: 6 }}>Devices with push turned on</div>
          <ul className="device-list">
            {info.devices.map((d) => (
              <li key={d.endpoint}>
                <span>{d.label ?? d.endpoint_host}{d.endpoint === subscribed && <span className="muted"> (this device)</span>}</span>
                <span className="muted mono">{d.last_ok_at ? `last sent ${ago(d.last_ok_at)}` : `added ${ago(d.created_at)}`}</span>
                <button className="btn btn-ghost btn-sm" disabled={busy}
                  onClick={() => act(async () => { await api.pushUnsubscribe(d.endpoint); }, "Device removed")}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Dialog>
  );
}
