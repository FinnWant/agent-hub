// Notifications on this device, three ways:
//  - the Windows app (Tauri): native notifications for events from /api/events, while it runs;
//  - browsers and the iPhone home-screen app: Web Push through the service worker (HTTPS only);
//  - any open window: in-app toasts for the same events.
import { api } from "./api";

export const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
export const isIOS = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
export const isStandalone =
  typeof window !== "undefined" &&
  (window.matchMedia?.("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true);

export const pushSupported =
  typeof window !== "undefined" && window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window &&
  "Notification" in window;

export type DeviceMode =
  | "tauri" // Windows app: native notifications
  | "push" // can subscribe to Web Push
  | "ios-install" // iPhone in Safari: must be added to the Home Screen first
  | "insecure" // plain HTTP: no push
  | "unsupported";

export function deviceMode(): DeviceMode {
  if (isTauri) return "tauri";
  if (isIOS && !isStandalone) return pushSupported || window.isSecureContext ? "ios-install" : "insecure";
  if (!window.isSecureContext) return "insecure";
  return pushSupported ? "push" : "unsupported";
}

export function deviceLabel(): string {
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua) || (isIOS && !/iPhone/.test(ua))) return "iPad";
  if (/Android/.test(ua)) return "Android";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : "Safari";
  const os = /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "";
  return `${browser}${os ? ` on ${os}` : ""}`;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!pushSupported || isTauri) return null;
  try {
    return await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch {
    return null;
  }
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const pad = "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob((base64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return (await reg?.pushManager.getSubscription()) ?? null;
}

/** Must be called from a tap/click (iOS only asks for permission inside a user gesture). */
export async function enablePush(publicKey: string): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Notifications were not allowed. You can change this in the device's settings.");
  const reg = (await registerServiceWorker()) ?? (await navigator.serviceWorker.ready);
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
  const json = sub.toJSON();
  await api.pushSubscribe({ endpoint: json.endpoint!, keys: json.keys as Record<string, string>, label: deviceLabel() });
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await api.pushUnsubscribe(sub.endpoint).catch(() => {});
  await sub.unsubscribe();
}

// ---- Windows app (Tauri) ----------------------------------------------------

export async function nativeNotify(title: string, body: string): Promise<void> {
  if (!isTauri) return;
  const n = await import("@tauri-apps/plugin-notification");
  let granted = await n.isPermissionGranted();
  if (!granted) granted = (await n.requestPermission()) === "granted";
  if (granted) n.sendNotification({ title, body });
}

/** In the Windows app, open links to other sites (previews, GitHub) in the default browser. */
export function installExternalLinkHandler(): void {
  if (!isTauri) return;
  document.addEventListener(
    "click",
    async (e) => {
      const a = (e.target as Element).closest?.("a");
      if (!a?.href) return;
      const url = new URL(a.href, location.href);
      if (url.origin === location.origin && a.target !== "_blank") return;
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      e.preventDefault();
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(url.href);
    },
    true,
  );
}
