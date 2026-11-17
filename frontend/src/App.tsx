import { useEffect, useState } from "react";
import { api, setUnauthorizedHandler, type Health } from "./lib/api";
import { match, navigate, usePath, useLinkInterception } from "./lib/router";
import { Icon, ToastProvider } from "./components/ui";
import Login from "./pages/Login";
import Home from "./pages/Home";
import SandboxDetail from "./pages/SandboxDetail";
import Audit from "./pages/Audit";
import { NeedsYou, usePendingRequests } from "./components/Requests";
import SidebarActivity from "./components/SidebarActivity";
import NotificationsDialog, { useHubEvents } from "./components/Notifications";
import { installExternalLinkHandler, registerServiceWorker } from "./lib/notifications";

registerServiceWorker();
installExternalLinkHandler();

function HubEvents() {
  useHubEvents();
  return null;
}

function NeedsYouLink({ current }: { current: boolean }) {
  const { items } = usePendingRequests();
  return (
    <a href="/needs-you" className="nav-item" aria-current={current ? "page" : undefined}>
      <Icon name="inbox" /> <span className="nav-label">Needs you</span>
      {items.length > 0 && <span className="count count-attention" aria-label={`${items.length} waiting`}>{items.length}</span>}
    </a>
  );
}

type AuthState = "checking" | "in" | "out" | "unconfigured";

export default function App() {
  const [auth, setAuth] = useState<AuthState>("checking");
  const [health, setHealth] = useState<Health | null>(null);
  const [notifOpen, setNotifOpen] = useState(false);
  const path = usePath();
  useLinkInterception();

  useEffect(() => {
    setUnauthorizedHandler(() => setAuth("out"));
    api.health().then(setHealth).catch(() => {});
    api
      .me()
      .then(() => setAuth("in"))
      .catch((e) => setAuth(e.status === 503 ? "unconfigured" : "out"));
  }, []);

  if (auth === "checking") return null;
  if (auth !== "in") {
    return (
      <ToastProvider>
        <Login unconfigured={auth === "unconfigured"} demo={!!health?.demo} onLoggedIn={() => setAuth("in")} />
      </ToastProvider>
    );
  }

  const route = match(path);
  const logout = async () => {
    await api.logout().catch(() => {});
    setAuth("out");
    navigate("/", true);
  };

  return (
    <ToastProvider>
      <div className="shell">
        <nav className="sidebar" aria-label="Main">
          <a href="/" className="brand">
            <span className="brand-mark" aria-hidden />
            <span>Kiln</span>
          </a>
          <a href="/" className="nav-item" aria-current={route.name === "home" || route.name === "sandbox" ? "page" : undefined}>
            <Icon name="box" /> <span className="nav-label">Sandboxes</span>
          </a>
          <NeedsYouLink current={route.name === "needsYou"} />
          <a href="/audit" className="nav-item" aria-current={route.name === "audit" ? "page" : undefined}>
            <Icon name="list" /> <span className="nav-label">Audit log</span>
          </a>
          <SidebarActivity currentSandbox={route.name === "sandbox" ? route.id : null} />
          <div className="sidebar-foot">
            {health && (
              <div className="host-mini muted" style={{ fontSize: 11 }}>
                {health.demo ? "Demo mode · simulated Docker" : health.docker ? "Docker connected" : "Docker unreachable"}
                <br />
                <span className="mono">v{health.version}</span>
              </div>
            )}
            <button className="btn btn-ghost btn-sm" onClick={() => setNotifOpen(true)} style={{ justifyContent: "flex-start" }}
              aria-label="Notifications">
              <Icon name="bell" /> <span className="hide-sm">Notifications</span>
            </button>
            <button className="btn btn-ghost btn-sm" onClick={logout} style={{ justifyContent: "flex-start" }} aria-label="Log out">
              <Icon name="logout" /> <span className="hide-sm">Log out</span>
            </button>
          </div>
        </nav>
        <main className="main" id="main">
          {route.name === "home" && <Home />}
          {route.name === "sandbox" && <SandboxDetail id={route.id} tab={route.tab} />}
          {route.name === "audit" && <Audit />}
          {route.name === "needsYou" && <NeedsYou />}
          {route.name === "notFound" && (
            <div className="page">
              <div className="empty">
                <h2>Nothing here</h2>
                <a className="btn" href="/">Back to sandboxes</a>
              </div>
            </div>
          )}
        </main>
      </div>
      <HubEvents />
      {notifOpen && <NotificationsDialog onClose={() => setNotifOpen(false)} />}
    </ToastProvider>
  );
}
