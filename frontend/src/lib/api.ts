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
  allow_push: boolean;
  last_run?: { id: string; state: RunState; created_at: number; finished_at: number | null } | null;
}

export type RunState = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "timed_out";

export interface Run {
  id: string;
  sandbox_id: string;
  runner: string;
  prompt: string;
  state: RunState;
  branch: string | null;
  base_branch: string | null;
  commit_sha: string | null;
  pushed: boolean;
  exit_code: number | null;
  error: string | null;
  timeout: number;
  max_turns: number;
  cost_usd: number | null;
  num_turns: number | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export type RunEvent = { seq: number; ts: number } & (
  | { type: "log"; kind: "text" | "tool" | "tool_result" | "system" | "stderr" | "stdout" | "result" | "error";
      text: string; name?: string; is_error?: boolean }
  | { type: "status"; state: RunState }
  | { type: "cost"; cost_usd: number | null; turns: number | null; duration_ms: number | null }
  | { type: "done"; state: RunState; error: string | null; commit: string | null; branch: string; pushed: boolean }
);

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
  agent: {
    claude_token: boolean;
    github_token: boolean;
    model: string | null;
    run_timeout_default: number;
    run_timeout_max: number;
    run_max_turns_default: number;
  };
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
  updateSandbox: (id: string, body: { allow_push?: boolean }) =>
    request<Sandbox>("PATCH", `/sandboxes/${encodeURIComponent(id)}`, body),
  runs: (sandboxId: string) => request<Run[]>("GET", `/sandboxes/${encodeURIComponent(sandboxId)}/runs`),
  startRun: (sandboxId: string, body: { prompt: string; timeout_minutes?: number; max_turns?: number }) =>
    request<Run>("POST", `/sandboxes/${encodeURIComponent(sandboxId)}/runs`, body),
  run: (id: string) => request<Run>("GET", `/runs/${encodeURIComponent(id)}`),
  cancelRun: (id: string) => request<Run>("POST", `/runs/${encodeURIComponent(id)}/cancel`),
  audit: (beforeId?: number, limit = 100) =>
    request<AuditEntry[]>("GET", `/audit?limit=${limit}${beforeId ? `&before_id=${beforeId}` : ""}`),
};

function wsBase(): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api`;
}

export function runEventsUrl(id: string): string {
  return `${wsBase()}/runs/${encodeURIComponent(id)}/events`;
}

export function terminalUrl(id: string, cols: number, rows: number): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/sandboxes/${encodeURIComponent(id)}/terminal?cols=${cols}&rows=${rows}`;
}
