// Thin client for the Agent Hub API. Same origin only; the session is an HttpOnly cookie.
// Mutating requests carry the CSRF header the backend requires.

export type SandboxState = "creating" | "running" | "stopped" | "error" | "destroyed";

export interface Sandbox {
  id: string;
  name: string;
  template: string;
  state: SandboxState;
  memory: number;
  memory_human: string;
  cpus: number;
  repo_url: string | null;
  base_branch: string | null;
  error: string | null;
  created_at: number;
  updated_at: number;
}

export interface Template {
  name: string;
  description: string;
  image: string;
  preview_port: number;
  built: boolean;
}

export interface SystemInfo {
  host: {
    cpus: number;
    load: [number, number, number];
    mem_total: number;
    mem_available: number;
    disk_total: number;
    disk_free: number;
  };
  caps: {
    max_concurrent_sandboxes: number;
    default_sandbox_memory: number;
    max_sandbox_memory: number;
    total_sandbox_memory: number;
    memory_reserve: number;
    default_sandbox_cpus: number;
    max_sandbox_cpus: number;
    min_free_disk: number;
  };
  sandboxes: { running: number; memory_used: number };
  can_start_default: boolean;
  blockers: string[];
}

export interface AuditEntry {
  id: number;
  ts: number;
  ip: string;
  action: string;
  target: string | null;
  ok: boolean;
  detail: Record<string, unknown> | null;
}

export interface Health {
  status: string;
  version: string;
  docker: boolean;
  demo: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public reasons: string[] = [],
  ) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

function messageFrom(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    // FastAPI validation errors: [{loc, msg}]
    if (Array.isArray(d) && d.length) {
      return d
        .map((e: { loc?: unknown[]; msg?: string }) => {
          const field = e.loc?.filter((p) => p !== "body").join(".");
          return field ? `${field}: ${e.msg}` : e.msg;
        })
        .join("; ");
    }
  }
  return `Request failed (${status})`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Requested-With"] = "agent-hub";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && path !== "/auth/login") onUnauthorized();
    throw new ApiError(res.status, messageFrom(data, res.status), (data && data.reasons) || []);
  }
  return data as T;
}

export const api = {
  health: () => request<Health>("GET", "/health"),
  me: () => request<{ authenticated: boolean }>("GET", "/auth/me"),
  login: (password: string) => request<{ ok: boolean }>("POST", "/auth/login", { password }),
  logout: () => request<{ ok: boolean }>("POST", "/auth/logout"),
  system: () => request<SystemInfo>("GET", "/system"),
  templates: () => request<Template[]>("GET", "/templates"),
  sandboxes: () => request<Sandbox[]>("GET", "/sandboxes"),
  sandbox: (id: string) => request<Sandbox>("GET", `/sandboxes/${encodeURIComponent(id)}`),
  createSandbox: (body: {
    name: string;
    template: string;
    memory: string;
    cpus: number;
    repo_url?: string;
    base_branch?: string;
  }) => request<Sandbox>("POST", "/sandboxes", body),
  start: (id: string) => request<Sandbox>("POST", `/sandboxes/${encodeURIComponent(id)}/start`),
  stop: (id: string) => request<Sandbox>("POST", `/sandboxes/${encodeURIComponent(id)}/stop`),
  destroy: (id: string) => request<void>("DELETE", `/sandboxes/${encodeURIComponent(id)}`),
  audit: (beforeId?: number, limit = 100) =>
    request<AuditEntry[]>("GET", `/audit?limit=${limit}${beforeId ? `&before_id=${beforeId}` : ""}`),
};

export function terminalUrl(id: string, cols: number, rows: number): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/sandboxes/${encodeURIComponent(id)}/terminal?cols=${cols}&rows=${rows}`;
}
