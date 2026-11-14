# Agent Hub

Run coding agents in isolated sandboxes on a home server, and steer them from a browser or a
desktop app over a private network.

Each sandbox is a locked-down container with its own workspace volume. An agent (Claude Code
in non-interactive mode, behind a pluggable runner) works on a branch inside it; you watch the
stream live, review the diff, and approve. Nothing is exposed to the public internet.

> **Status:** Phase 3 — agent runs: give Claude Code a task in a sandbox, watch it live, get a
> commit on an `agent/<run-id>` branch. Files, diff review and previews come in phase 4.

## Architecture

```
Browser / desktop app ── Tailscale ──► Agent Hub :8090  (FastAPI, systemd, user `agenthub`)
                                         ├─ REST API + WebSockets
                                         ├─ SQLite: sandboxes, runs, audit log
                                         └─ rootless Docker (per-user daemon)
                                              ├─ sandbox-<id>  (volume ah-<id> at /workspace)
                                              └─ sandbox-<id>  ...
```

| Path | Contents |
|---|---|
| `backend/` | FastAPI app and tests |
| `frontend/` | React + TypeScript + Vite UI (design system: *Kiln*) |
| `desktop/` | Tauri 2 wrapper around the frontend |
| `images/` | Dockerfiles for sandbox templates (`python`, `node`, `static`) |
| `deploy/` | Install script, systemd units, egress firewall rules, `env.example` |

## Install

Prerequisites on the server (Ubuntu):

1. Docker Engine from Docker's apt repo, including `docker-ce-rootless-extras`, and
   Node.js 20.19+ or 22.12+ with npm (to build the web UI).
2. A system user with rootless Docker and lingering:
   ```bash
   sudo useradd --create-home --shell /bin/bash agenthub
   sudo loginctl enable-linger agenthub
   sudo machinectl shell agenthub@ /usr/bin/dockerd-rootless-setuptool.sh install
   ```
3. Optional but recommended — let sandboxes be disk-I/O throttled by delegating the `io`
   cgroup controller to that user (`Delegate=cpu cpuset io memory pids` in
   `/etc/systemd/system/user@<uid>.service.d/delegate.conf`).

Then:

```bash
sudo bash deploy/install.sh                       # app, web UI, images, egress filter, systemd units
cd /opt/agent-hub/backend && sudo -u agenthub ../.venv/bin/python -m app.set_password
sudo systemctl restart agent-hub
bash deploy/smoke-test.sh                         # API + terminal acceptance checks
bash deploy/integration-test.sh                   # Docker integration tests, as agenthub
```

Add the port (`tcp:8090`) to your Tailscale ACL so your devices can reach it, then open
`http://<server>:8090` in a browser on your tailnet.

### Agent credentials

```bash
claude setup-token        # on any machine signed in to your Claude subscription; copy the token
cd /opt/agent-hub/backend
sudo -u agenthub ../.venv/bin/python -m app.set_secret claude    # paste it (input is hidden)
sudo -u agenthub ../.venv/bin/python -m app.set_secret github    # optional, to push branches
sudo systemctl restart agent-hub
```

- **Claude:** `claude setup-token` creates a one-year token for your subscription. Anthropic's
  terms allow it for your own use, so keep Agent Hub single-user. Revoke it from your Claude
  account settings if you think it leaked.
- **GitHub:** create a *fine-grained* token at GitHub → Settings → Developer settings, with
  **only the repositories** agents may work on and **Contents: read and write**. Turn on branch
  protection for `main` in those repositories as a second line of defence. Pushing is off per
  sandbox until you enable it in the sandbox's Settings tab.

## Web UI

- **Sandboxes:** host memory, sandbox budget, CPU and disk at a glance, with the reasons a new
  sandbox can't start right now (caps, memory reserve, disk floor). Create (<kbd>N</kbd>),
  start, stop.
- **Sandbox → Runs:** describe a task and start Claude Code (<kbd>Ctrl/⌘</kbd>+<kbd>Enter</kbd>).
  The log streams live (messages, tool calls, results, cost and turns); cancel at any time.
  Each run gets its own branch `agent/<run-id>` from the sandbox's base branch, and whatever
  the agent leaves is committed there. Run history is kept per sandbox.
- **Sandbox → Terminal:** a real shell (`bash -l` as `agent` in `/workspace`) over a
  WebSocket, rendered with xterm.js. Resizes with the window; the shell and everything it
  started are killed when you close the tab or reconnect. Idle shells close after an hour.
- **Sandbox → Settings:** push agent branches to GitHub (off by default), limits and isolation
  summary, destroy (type the name to confirm).
- **Audit log:** every login, sandbox action and terminal session, filterable.

The UI follows the *Kiln* design system (dark slate, matte surfaces, ember reserved for the
primary action) and works at phone width.

## Using the API

All requests except `/api/health` and login need the session cookie; requests that change
anything also need the header `X-Requested-With: agent-hub`.

```bash
H='X-Requested-With: agent-hub'; API=http://<server>:8090/api
curl -c jar -H "$H" -H 'Content-Type: application/json' -d '{"password":"..."}' $API/auth/login
curl -b jar $API/system                                   # host resources, caps, can I start one?
curl -b jar -H "$H" -H 'Content-Type: application/json' \
     -d '{"name":"demo","template":"python","memory":"2g"}' $API/sandboxes
curl -b jar -H "$H" -H 'Content-Type: application/json' \
     -d '{"cmd":"python3 --version"}' $API/sandboxes/<id>/exec
curl -b jar -H "$H" -X POST $API/sandboxes/<id>/stop      # .../start
curl -b jar -H "$H" -X DELETE $API/sandboxes/<id>
curl -b jar $API/audit
```

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness and Docker reachability (no auth) |
| `POST /api/auth/login` · `logout` · `GET /api/auth/me` | Session |
| `GET /api/system` | Host CPU/RAM/disk, caps, and why a sandbox can't start right now |
| `GET /api/templates` | Templates and whether their images are built |
| `GET` / `POST /api/sandboxes` | List / create (`name`, `template`, `repo_url`, `base_branch`, `memory`, `cpus`) |
| `GET` / `DELETE /api/sandboxes/{id}` | Detail / destroy (removes the container and its volume) |
| `POST /api/sandboxes/{id}/start` · `stop` | Lifecycle (start re-checks capacity) |
| `POST /api/sandboxes/{id}/exec` | Run a command as `agent` in `/workspace` (timeout, output capped) |
| `WS /api/sandboxes/{id}/terminal?cols=&rows=` | Interactive shell: binary frames both ways, `{"type":"resize","cols","rows"}` text frames |
| `PATCH /api/sandboxes/{id}` | Settings: `allow_push` |
| `GET` / `POST /api/sandboxes/{id}/runs` | Run history / start a run (`prompt`, `timeout_minutes`, `max_turns`) |
| `GET /api/runs/{id}` · `GET /api/runs/{id}/log` · `POST /api/runs/{id}/cancel` | Run detail, stored events, cancel |
| `WS /api/runs/{id}/events` | Backlog then live events, batched every 150 ms, as `{"events": [...]}` |
| `GET /api/audit` | Audit log, newest first |

## Development

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest              # unit tests, no Docker needed
DEMO_MODE=true PORT=18090 SESSION_SECRET=dev-only-secret-dev-only-secret .venv/bin/python -m app.serve

cd frontend && npm ci && npm run dev    # http://127.0.0.1:5173, proxies /api to :18090
```

In `DEMO_MODE` the backend fakes Docker, seeds two sandboxes and gives each a simulated shell,
so the whole UI works without Docker (log in with `demo`).

Integration tests (`-m docker`, run with `bash deploy/integration-test.sh`) use the real rootless daemon as the `agenthub`
user; see `backend/tests/test_integration_docker.py`.

## Agent runs

```
queued → running → succeeded | failed | cancelled | timed_out
```

1. **Workspace.** If the sandbox has a repository and no clone yet, Agent Hub clones it *on the
   host* (with the GitHub token if set), bundles it, and copies the bundle in. No repository:
   `/workspace` becomes a fresh git repo. A dirty working tree is refused, so every run starts
   clean.
2. **Branch** `agent/<run-id>` from the base branch.
3. **Agent:** `claude -p` with `--output-format stream-json`, `--max-turns`, and
   `--permission-mode bypassPermissions` (the sandbox is the permission boundary), as user
   `agent` in its own process session. Only `CLAUDE_CODE_OAUTH_TOKEN` is added to its
   environment. The CLI is a pinned, checksum-verified release in a root-owned path, with
   auto-update and telemetry off.
4. **Limits:** one run per sandbox; wall-clock timeout (default 30 min) and cancel both kill the
   agent's whole process session; turn limit; the memory reserve is re-checked before a run.
5. **Commit** everything the agent left on the branch (also after cancel or timeout, so the
   work can be reviewed).
6. **Push** (only if enabled for the sandbox, the run succeeded, and the repository is on
   github.com): the branch is bundled in the sandbox, copied out, fetched into a fresh repo on
   the host and pushed by URL, exactly `agent/<run-id>`, never forced. Nothing the agent wrote
   (hooks, `.git/config`, credential helpers) runs alongside the token.

Every log line is redacted for known secrets (and common token shapes) before it is stored or
streamed.

## Security model

Agent Hub runs code written by an AI model, which may in turn have read untrusted text
(issues, READMEs, web pages). Treat every sandbox as hostile.

**Isolation**
- **Rootless Docker.** The Docker daemon runs as the unprivileged `agenthub` user, not root.
  Access to its socket is therefore *not* equivalent to root on the host — the usual
  `docker` group trade-off does not apply. Agent Hub never mounts the Docker socket into a
  sandbox.
- Every container: non-root user, all capabilities dropped, `no-new-privileges`, never
  privileged, no host mounts (only its own named volume), memory/CPU/PID limits, and a
  higher OOM score than everything else so the kernel kills a sandbox first.
- Agent Hub only touches containers labelled `agent-hub.managed=true`.

**Network**
- The API listens only on localhost and the Tailscale/LAN interface; nothing is added to the
  router or any public tunnel.
- Sandboxes can reach the internet (package registries, GitHub, the model API) but **not**
  the home LAN, the Tailscale network, link-local addresses or the host's own services.
  Because rootless container traffic leaves the host as ordinary traffic from the
  `agenthub` user, this is enforced with `iptables` `OUTPUT` rules matched on that user
  (`-m owner`), re-applied at boot, and covered by integration tests.

**Capacity** — global caps protect other workloads on the host: at most 2 running sandboxes,
6 GB total sandbox memory (4 GB max each), a 3 GB free-memory reserve checked before every
start, disk I/O limits, and a free-disk floor for new runs.

**Credentials** — secrets live only in a mode-600 `.env`; the API reports only whether they
are set. The Claude token goes into the agent process's environment for the length of a run;
the GitHub token never enters a sandbox (clone and push run on the host). Both are redacted
from stored and streamed logs. GitHub access is scoped to selected repositories; only
`agent/<run-id>` branches are pushed, never `main`/`master`, never forced, and only when you
enable it per sandbox.

**Access** — single admin account (hashed password), `HttpOnly` `SameSite=Strict` session
cookie, CSRF header on mutating requests, session cookie **and** same-origin `Origin` checks
on the terminal WebSocket, login rate limiting, fail-closed when no password is set, a strict
Content-Security-Policy (same-origin scripts only, no framing), and an audit log of every
action including terminal open/close.

### Known risks

- **Prompt injection.** An agent can be manipulated by content it reads into doing things you
  didn't ask for *within its sandbox* — deleting work, writing bad code, or exfiltrating
  anything the sandbox can see (including the repo and the model credential) to the
  internet. The sandbox boundary and the network rules limit the blast radius; they do not
  make the agent trustworthy. Review every diff before approving.
- **The Claude token is visible to the agent.** Its tools inherit the process environment, so
  a manipulated agent could read the token and send it out. Use a token you can revoke, keep
  an eye on your usage, and revoke it if a run looks suspicious. (Your GitHub token is not
  exposed this way.)
- **Open egress.** Sandboxes may contact any public host. A domain allowlist (egress proxy)
  is planned, not built.
- **Container escape.** Rootless Docker limits the damage of a kernel or runtime escape to the
  `agenthub` user. gVisor is a possible further hardening step.

## Roadmap

0. Preparation — Docker, caps, scaffold ✓
1. Sandbox API — templates, lifecycle, limits, egress rules, auth, audit log, systemd ✓
2. Live terminal and minimal web UI ✓
3. Agent runs — runner interface, branch workflow, streaming logs, cancel/timeout, redaction *(current)*
4. Files, diff review, previews, disk checks, phone layout
5. Tauri desktop app with native notifications

**Later:** gVisor, egress domain allowlist, opening pull requests, multiple runners, run
templates, scheduled runs.
