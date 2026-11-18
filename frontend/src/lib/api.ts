// Thin client for the Kiln API. Same origin only; the session is an HttpOnly cookie.
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
  preview_port: number | null;
  preview_url: string | null;
  workspace_bytes: number | null;
  /** The project's checks, run by Kiln when the agent wants to stop and after each run. */
  verify_cmd: string | null;
  /** Per run, estimated at API list prices; the agent is held when it's reached. */
  budget_usd: number | null;
  /** Tool calls that need the owner's OK, one rule per line (see owner.py). */
  approval_rules: string | null;
  /** Queued tasks wait while this is on; the queue turns it on itself when a run goes wrong. */
  queue_paused: boolean;
  queue_note: string | null;
  memory_usage: number | null;
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
  session_id: string | null;
  verify_cmd: string | null;
  verify: "passed" | "failed" | null;
  /** Times failing checks sent the agent back to work. */
  verify_attempts: number;
  /** The run this one continues (its branch and agent session). */
  parent_run_id: string | null;
  budget_usd: number | null;
  approval_rules: string | null;
  /** Measured at the Claude gateway; cost is an estimate at API list prices. */
  usage: RunUsage | null;
  cost_est: number | null;
  /** Seconds spent waiting on the owner (not counted against the time limit). */
  paused_seconds: number;
  /** The handoff report, once the run has ended. */
  report: RunReport | null;
  /** The checkpoint the branch was put back to, if it was. */
  rewound_to: number | null;
  /** The agent is running and reads messages (see `api.sendRunMessage`). */
  can_message: boolean;
  /** The pull request opened for the run's branch. */
  pr_url: string | null;
}

export interface QueuedTask {
  id: string;
  sandbox_id: string;
  prompt: string;
  runner: string;
  timeout: number | null;
  max_turns: number | null;
  /** Continue the run before it instead of starting fresh from the base branch. */
  continue_previous: boolean;
  position: number;
  created_at: number;
}

export interface TaskQueue {
  paused: boolean;
  note: string | null;
  items: QueuedTask[];
  /** The run a queue change started right away, if any. */
  started?: Run | null;
}

/** Something a run needs the owner to decide (approval, question, budget). */
export interface OwnerRequest {
  id: string;
  run_id: string;
  sandbox_id: string;
  sandbox_name: string | null;
  kind: "approval" | "question" | "budget" | "stuck";
  state: "pending" | "answered" | "expired" | "closed";
  title: string;
  body: string;
  options: string[];
  answer: { choice: string | null; text: string | null; amount: number | null } | null;
  created_at: number;
  answered_at: number | null;
  deadline: number | null;
  data: { spent?: number; budget?: number; tool?: string; rule?: string };
}

export interface RunReport {
  summary: string;
  sections: Partial<Record<"Done" | "Not done" | "Assumptions" | "How to verify" | "Risks", string>>;
  facts: {
    state: RunState; error: string | null; minutes: number; waited_minutes: number;
    checks: "passed" | "failed" | null; check_rounds: number;
    files: number | null; insertions: number | null; deletions: number | null; file_names: string[];
    commit: string | null; pushed: boolean; cost_est: number | null; api_calls: number | null;
    checkpoints: number; questions: number; approvals: number; compactions: number; loops: number;
  };
}

export interface Checkpoint {
  run_id: string;
  n: number;
  sha: string;
  ts: number;
  seq: number;
  files: string[];
}

export interface RunUsage {
  requests: number;
  input: number;
  cache_read: number;
  cache_write: number;
  output: number;
  cost: number;
  unpriced: number;
  context: number;
  model: string;
}

export type RunEvent = { seq: number; ts: number } & (
  | { type: "log"; kind: "text" | "tool" | "tool_result" | "system" | "stderr" | "stdout" | "result" | "error" | "user";
      text: string; name?: string; is_error?: boolean }
  | { type: "log"; kind: "check"; text: string; phase: "stop" | "final"; passed: boolean;
      exit_code: number | null; timed_out: boolean; seconds: number; attempt?: number }
  | { type: "log"; kind: "request"; request: string; request_kind: OwnerRequest["kind"]; title: string;
      text: string; options: string[] }
  | { type: "log"; kind: "answer"; request: string; state: "answered" | "expired"; text: string }
  | { type: "log"; kind: "checkpoint"; n: number; sha: string; files: string[]; text: string }
  | { type: "log"; kind: "loop"; text: string }
  | { type: "status"; state: RunState }
  | { type: "cost"; cost_usd: number | null; turns: number | null; duration_ms: number | null }
  | { type: "done"; state: RunState; error: string | null; commit: string | null; branch: string; pushed: boolean }
);

export interface FileEntry {
  name: string;
  type: "dir" | "file" | "link" | "other";
  size: number;
  target?: string;
}

export interface Browse {
  path: string;
  kind: "dir" | "file" | "link" | "other";
  size: number;
  entries?: FileEntry[];
  truncated?: boolean;
  content?: string;
  binary?: boolean;
  too_large?: boolean;
  limit?: number;
  target?: string;
}

export interface RunDiff {
  run: string;
  base: string;
  branch: string;
  files: { path: string; added: number | null; removed: number | null }[];
  size: number;
  patch?: string;
  too_large?: boolean;
}

export interface PreviewStatus {
  url: string | null;
  port: number;
  host_port: number | null;
  enabled: boolean;
  listening: "any" | "loopback" | "none" | "stopped";
  static_server?: boolean;
  has_index?: boolean;
  lists_directories?: boolean;
}

export interface PruneResult {
  containers: string[];
  volumes: string[];
  images: string[];
  bytes: number;
}

export interface PushInfo {
  available: boolean;
  public_key: string | null;
  devices: { label: string | null; endpoint_host: string; created_at: number; last_ok_at: number | null; endpoint: string }[];
}

export interface HubEvent {
  kind: "run.finished" | "run.failed" | "run.needs_you" | "run.budget" | "sandbox.problem" | "test";
  title: string;
  body: string;
  url?: string;
  sandbox?: string;
  run?: string;
  ts: number;
}

export interface Template {
  name: string;
  description: string;
  image: string;
  preview_port: number;
  built: boolean;
  check_hint: string;
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
  preview: { enabled: boolean; host: string | null; ports: string };
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
  liveRuns: () => request<(Run & { sandbox_name: string | null })[]>("GET", "/runs"),
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
  updateSandbox: (id: string, body: { allow_push?: boolean; verify_cmd?: string; budget_usd?: number; base_branch?: string;
    approval_rules?: string }) =>
    request<Sandbox>("PATCH", `/sandboxes/${encodeURIComponent(id)}`, body),
  runs: (sandboxId: string) => request<Run[]>("GET", `/sandboxes/${encodeURIComponent(sandboxId)}/runs`),
  startRun: (sandboxId: string, body: { prompt: string; runner?: string; timeout_minutes?: number;
    max_turns?: number; continue_from?: string }) =>
    request<Run>("POST", `/sandboxes/${encodeURIComponent(sandboxId)}/runs`, body),
  run: (id: string) => request<Run>("GET", `/runs/${encodeURIComponent(id)}`),
  openPr: (id: string, draft = false) => request<Run>("POST", `/runs/${encodeURIComponent(id)}/pr`, { draft }),
  syncSandbox: (id: string) => request<{ message: string }>("POST", `/sandboxes/${encodeURIComponent(id)}/sync`),
  queue: (sandboxId: string) => request<TaskQueue>("GET", `/sandboxes/${encodeURIComponent(sandboxId)}/queue`),
  enqueue: (sandboxId: string, body: { prompt: string; runner?: string; timeout_minutes?: number; max_turns?: number;
    continue_previous?: boolean }) =>
    request<TaskQueue>("POST", `/sandboxes/${encodeURIComponent(sandboxId)}/queue`, body),
  setQueuePaused: (sandboxId: string, paused: boolean) =>
    request<TaskQueue>("PATCH", `/sandboxes/${encodeURIComponent(sandboxId)}/queue`, { paused }),
  dequeue: (sandboxId: string, taskId: string) =>
    request<TaskQueue>("DELETE", `/sandboxes/${encodeURIComponent(sandboxId)}/queue/${encodeURIComponent(taskId)}`),
  moveQueued: (sandboxId: string, taskId: string, offset: number) =>
    request<TaskQueue>("POST", `/sandboxes/${encodeURIComponent(sandboxId)}/queue/${encodeURIComponent(taskId)}/move`, { offset }),
  cancelRun: (id: string) => request<Run>("POST", `/runs/${encodeURIComponent(id)}/cancel`),
  requests: (params: { state?: OwnerRequest["state"]; run?: string; limit?: number } = {}) =>
    request<OwnerRequest[]>("GET", `/requests?${new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]))}`),
  answerRequest: (id: string, body: { choice?: string; text?: string; amount?: number }) =>
    request<OwnerRequest>("POST", `/requests/${encodeURIComponent(id)}/answer`, body),
  ledger: (sandboxId: string) => request<{ exists: boolean; text: string }>("GET", `/sandboxes/${encodeURIComponent(sandboxId)}/ledger`),
  saveLedger: (sandboxId: string, text: string) =>
    request<{ exists: boolean; text: string }>("PUT", `/sandboxes/${encodeURIComponent(sandboxId)}/ledger`, { text }),
  checkpoints: (runId: string) => request<Checkpoint[]>("GET", `/runs/${encodeURIComponent(runId)}/checkpoints`),
  rewind: (runId: string, n: number) => request<Run>("POST", `/runs/${encodeURIComponent(runId)}/rewind`, { n }),
  sendRunMessage: (id: string, text: string) =>
    request<{ ok: boolean }>("POST", `/runs/${encodeURIComponent(id)}/messages`, { text }),
  files: (id: string, path: string) =>
    request<Browse>("GET", `/sandboxes/${encodeURIComponent(id)}/files?path=${encodeURIComponent(path)}`),
  runDiff: (runId: string) => request<RunDiff>("GET", `/runs/${encodeURIComponent(runId)}/diff`),
  preview: (id: string) => request<PreviewStatus>("GET", `/sandboxes/${encodeURIComponent(id)}/preview`),
  startStatic: (id: string) => request<PreviewStatus>("POST", `/sandboxes/${encodeURIComponent(id)}/preview/static`),
  stopStatic: (id: string) => request<PreviewStatus>("DELETE", `/sandboxes/${encodeURIComponent(id)}/preview/static`),
  measure: (id: string) => request<{ workspace_bytes: number | null }>("POST", `/sandboxes/${encodeURIComponent(id)}/measure`),
  prune: () => request<PruneResult>("POST", "/system/prune"),
  push: () => request<PushInfo>("GET", "/push"),
  pushSubscribe: (body: { endpoint: string; keys: Record<string, string>; label: string }) =>
    request<{ ok: boolean }>("POST", "/push/subscribe", body),
  pushUnsubscribe: (endpoint: string) => request<{ removed: boolean }>("POST", "/push/unsubscribe", { endpoint }),
  pushTest: () => request<{ sent: number; removed: number; failed: number }>("POST", "/push/test"),
  audit: (beforeId?: number, limit = 100) =>
    request<AuditEntry[]>("GET", `/audit?limit=${limit}${beforeId ? `&before_id=${beforeId}` : ""}`),
};

function wsBase(): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api`;
}

export function hubEventsUrl(): string {
  return `${wsBase()}/events`;
}

export function runEventsUrl(id: string): string {
  return `${wsBase()}/runs/${encodeURIComponent(id)}/events`;
}

export function terminalUrl(id: string, cols: number, rows: number): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/sandboxes/${encodeURIComponent(id)}/terminal?cols=${cols}&rows=${rows}`;
}
