# Kiln

Run coding agents in isolated sandboxes on a home server, and steer them from a browser or a
desktop app over a private network.

Each sandbox is a locked-down container with its own workspace volume. An agent (Claude Code
in non-interactive mode, behind a pluggable runner) works on a branch inside it; you watch the
stream live, review the diff, and approve. Nothing is exposed to the public internet.

> **Status:** Hardening pass after phase 5: the Claude token stays out of sandboxes, sandboxes
> are isolated from each other, the diff you review is computed on the host, and the server
> setup is tightened (see [Security model](#security-model) and [Server hardening](#server-hardening)).

## Architecture

```
Browser / desktop app ── Tailscale ──► Kiln :8090  (FastAPI, systemd, user `agenthub`)
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
| `desktop/` | Tauri 2 Windows app (built by `.github/workflows/desktop.yml`) |
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

In the Tailscale admin console, enable **HTTPS Certificates** (DNS page) before installing, and
allow `tcp:443` and, for previews, `tcp:8200-8299` in your ACL. The installer then serves Agent
Hub at `https://<server>.<tailnet>.ts.net` through `tailscale serve` (Kiln itself listens on
127.0.0.1 only). Certificates are public records, so the machine and tailnet names appear in
Certificate Transparency logs; nothing becomes reachable from outside your tailnet. Without
HTTPS (`HTTPS=off` in `.env`) Kiln stays on `http://<tailscale-ip>:8090` (`tcp:8090`) and
everything works except push notifications.

## Apps and notifications

You're notified when a run finishes, fails or times out, and when a sandbox stops on its own
(for example, killed for using too much memory).

- **Windows:** the desktop app (`desktop/`, Tauri 2) shows Kiln in its own window with
  native notifications. Closing the window keeps it in the tray. Build it from the
  *Actions → Desktop app (Windows) → Run workflow* button; the installers land in a draft
  release. They aren't code-signed, so SmartScreen asks once. On first launch, enter your
  Kiln address. The app gives your server's pages only two native abilities: notifications
  and opening links in your browser.
- **iPhone (iOS 16.4 or later):** open the https address in Safari, tap **Share → Add to Home
  Screen**, open Kiln from the Home Screen, log in, then tap the bell and **Turn on**.
  Notifications arrive through Apple's push service even when the app is closed.
- **Desktop browsers** on the https address can turn on push the same way (bell icon).

Push messages are end-to-end encrypted (Web Push) and signed with a key generated on the server
(`data/vapid.pem`). They contain only the notification text, never logs or secrets.

### Agent credentials

```bash
claude setup-token        # on any machine signed in to your Claude subscription; copy the token
cd /opt/agent-hub/backend
sudo -u agenthub ../.venv/bin/python -m app.set_secret claude    # paste it (input is hidden)
sudo -u agenthub ../.venv/bin/python -m app.set_secret github    # optional, to push branches
sudo systemctl restart agent-hub
```

- **Claude:** `claude setup-token` creates a one-year token for your subscription. Anthropic's
  terms allow it for your own use, so keep Kiln single-user. Revoke it from your Claude
  account settings if you think it leaked. It never enters a sandbox: the **Claude gateway**
  holds it (see [Agent runs](#agent-runs)).
- **GitHub:** create a *fine-grained* token at GitHub → Settings → Developer settings, with
  **only the repositories** agents may work on and **Contents: read and write**. Leave
  **Workflows** off, so agent branches can't change GitHub Actions workflows (and keep
  `on: push` workflows in those repositories limited to protected branches). Turn on branch
  protection for `main` in those repositories as a second line of defence. Pushing is off per
  sandbox until you enable it in the sandbox's Settings tab.

## Web UI

- **Sandboxes:** host memory, sandbox budget, CPU and disk at a glance, with the reasons a new
  sandbox can't start right now (caps, memory reserve, disk floor). Create (<kbd>N</kbd>),
  start, stop.
- **Sandbox → Runs:** describe a task and start Claude Code (<kbd>Ctrl/⌘</kbd>+<kbd>Enter</kbd>).
  The log streams live (messages, tool calls, results, cost and turns); cancel at any time.
  While the agent works you can send it messages; each shows in the log once the agent reads it.
  Each run gets its own branch `agent/<run-id>` from the sandbox's base branch, and whatever
  the agent leaves is committed there. Run history is kept per sandbox.
- **Sandbox → Changes:** a run's branch against the point where it left the base branch,
  as a unified or split diff with per-file line counts.
- **Sandbox → Files:** a read-only tree and viewer for `/workspace` (text up to 512 KB;
  binaries and links are labelled, never followed).
- **Sandbox → Preview:** the sandbox's preview link, whether anything is listening (and on
  which address), a one-click static file server for `/workspace`, and dev-server commands for
  the template.
- **Sandbox → Terminal:** a real shell (`bash -l` as `agent` in `/workspace`) over a
  WebSocket, rendered with xterm.js. Resizes with the window; the shell and everything it
  started are killed when you close the tab or reconnect. Idle shells close after an hour.
- **Sandbox → Settings:** push agent branches to GitHub (off by default), limits and isolation
  summary, destroy (type the name to confirm).
- **Audit log:** every login, sandbox action and terminal session, filterable.
- **Resources:** memory in use per sandbox (every 15 s) and workspace size (every 5 min, at
  low priority). **Clean up** removes old sandbox images, the build cache and anything left
  behind by destroyed sandboxes; it never touches a sandbox you haven't destroyed.

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
| `GET /api/sandboxes/{id}/files?path=` | Directory listing or file content (read-only, capped) |
| `GET /api/runs/{id}/diff` | The run's branch vs its base: per-file counts and the patch |
| `GET /api/sandboxes/{id}/preview` · `POST`/`DELETE …/preview/static` | Preview status; static file server |
| `POST /api/sandboxes/{id}/measure` · `POST /api/system/prune` | Measure workspace now; clean up disk |
| `WS /api/events` | Notification events for open windows and the Windows app |
| `GET /api/push` · `POST /api/push/subscribe` · `unsubscribe` · `test` | Web Push: key, devices, test |
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

1. **Workspace.** If the sandbox has a repository and no clone yet, Kiln clones it *on the
   host* (with the GitHub token if set), bundles it, and copies the bundle in. No repository:
   `/workspace` becomes a fresh git repo. A dirty working tree is refused, so every run starts
   clean.
2. **Branch** `agent/<run-id>` from the base branch.
3. **Agent:** `claude -p` with stream-json input and output, `--replay-user-messages`,
   `--max-turns`, and `--permission-mode bypassPermissions` (the sandbox is the permission
   boundary), as user `agent` in its own process session. The CLI is a pinned,
   checksum-verified release in a root-owned path, with auto-update and telemetry off.

   **Detached, so runs survive Kiln.** The agent runs detached in the sandbox: its output goes
   to `/tmp/kiln-runs/<run-id>/out`, which Kiln follows with a separate exec, storing with each
   event the byte offset reached. Restarting Kiln (an upgrade, a reboot of Kiln alone) doesn't
   touch the agent: on startup Kiln checks each running run's agent, re-registers its gateway
   key, and follows the output again from the stored offset, so nothing is lost or logged
   twice. The run's time limit still counts from its start. Runs whose agent is gone (the
   sandbox stopped, the server rebooted) are marked failed, and their work is committed.

   **Messages while it works.** The agent reads its input from a FIFO. The prompt is the first
   message; messages you send are written to it and the agent takes them in at its next step.
   When the agent has finished and has read every message, Kiln closes its input and it exits.

   **Kiln's hooks.** Before each Claude Code run, Kiln copies root-owned files into the sandbox:
   `/etc/claude-code/managed-settings.json` (Claude Code's managed settings, which user and
   project settings can't override or disable), one hook command, `/usr/local/lib/kiln/kiln-hook`,
   and a reply directory. A hook saves its input in the run directory and adds a marker line to
   the agent's output, which Kiln already follows, so no network is involved. A hook that needs
   a decision (allow, deny, block) waits for Kiln's reply, a root-owned file the agent can't
   forge, and carries on without one after a time limit. Kiln uses this to mark context
   compaction in the run log; checks, approvals and limits build on it. Hooks in your own
   repositories keep working alongside Kiln's.

   **Claude gateway.** The agent never sees your Claude token. It gets a random per-run key and
   `ANTHROPIC_BASE_URL=http://kiln-gateway:8080`. `kiln-gateway` is a small, locked-down
   container (read-only, no capabilities, 128 MB) that holds the token; Kiln attaches it to
   the sandbox's network while the run lasts. It forwards requests carrying a registered key
   to the Anthropic API, and nowhere else, with the key swapped for the token. Kiln removes the
   key when the run ends, so a copied key or a process left running stops working. (Setting
   `CLAUDE_TOKEN_MODE=direct` puts the token in the sandbox instead; only as a fallback.)
4. **Limits:** one run per sandbox; wall-clock timeout (default 30 min) and cancel both kill the
   agent's whole process session; turn limit; the memory reserve is re-checked before a run.
5. **Commit** everything the agent left on the branch (also after cancel or timeout, so the
   work can be reviewed).
6. **Push** (only if enabled for the sandbox, the run succeeded, and the repository is on
   github.com): the branch is bundled in the sandbox, streamed out to disk, fetched into a
   fresh repo on the host and pushed by URL, exactly `agent/<run-id>`, never forced. Nothing
   the agent wrote (hooks, `.git/config`, credential helpers) runs alongside the token.
7. **Review.** The Changes tab's diff is computed *on the host* from the same kind of export,
   with the host's git in a fresh repository. A fake `git` in the sandbox, attributes that
   mark files as binary, textconv filters or replace refs can't change what you see.

Every log line is redacted for known secrets (and common token shapes) before it is stored or
streamed.

## Previews

Each sandbox gets a host port from `PREVIEW_PORT_RANGE` (8200–8299) when it is created, mapped
to its template's port (8000 python, 5173 node, 3000 static). It is published on the
**Tailscale IP only** (`PREVIEW_BIND`), never `0.0.0.0`, and ufw allows the range only on
`tailscale0`. A dev server must listen on `0.0.0.0` inside the sandbox.

Preview links use a different hostname from Kiln (`PREVIEW_HOST`): the Tailscale IP when
Kiln is on HTTPS at its MagicDNS name, or the MagicDNS name when Kiln is on the IP.
Kiln refuses logins and API calls on the preview hostname. Browsers send a host's cookies
to *every* port on it, so if you opened Kiln and a preview on the same hostname, code the
agent wrote could read your session cookie. With different hostnames it never gets one.

## Security model

Kiln runs code written by an AI model, which may in turn have read untrusted text
(issues, READMEs, web pages). Treat every sandbox as hostile.

**Isolation**
- **Rootless Docker.** The Docker daemon runs as the unprivileged `agenthub` user, not root.
  Access to its socket is therefore *not* equivalent to root on the host — the usual
  `docker` group trade-off does not apply. Kiln never mounts the Docker socket into a
  sandbox.
- Every container: non-root user, all capabilities dropped, `no-new-privileges`, never
  privileged, no host mounts (only its own named volume), memory/CPU/PID limits, and a
  higher OOM score than everything else so the kernel kills a sandbox first.
- Kiln only touches containers labelled `agent-hub.managed=true`.
- **Each sandbox has its own Docker network**, so sandboxes can't reach each other (a sandbox
  made before this moves to its own network the next time it starts). Kiln's own commands in
  a sandbox use system directories only, never the agent's `~/.local/bin`.

**Network**
- The API listens only on localhost and the Tailscale/LAN interface; previews only on the
  Tailscale IP, on a hostname that never holds a session. Nothing is added to the router or
  any public tunnel.
- Sandboxes can reach the internet (package registries, GitHub, the model API) but **not**
  the home LAN, the Tailscale network, link-local addresses or the host's own services.
  Because rootless container traffic leaves the host as ordinary traffic from the
  `agenthub` user, this is enforced with `iptables` `OUTPUT` rules matched on that user
  (`-m owner`), re-applied at boot, and covered by integration tests.

**Capacity** — global caps protect other workloads on the host: at most 2 running sandboxes,
6 GB total sandbox memory (4 GB max each), a 3 GB free-memory reserve checked before every
start, disk I/O limits, and a free-disk floor for new runs. Sandboxes have no disk quota
(Docker can't enforce one on ext4), so if free disk drops below `DISK_EMERGENCY_FREE` (5 GB)
the monitor stops every running sandbox and notifies you. The Kiln service itself is capped at
1 GB of memory.

**Credentials** — secrets live only in a mode-600 `.env`; the API reports only whether they
are set. Neither token enters a sandbox: the Claude token stays in the Claude gateway, and
clone and push run on the host. Both are redacted from stored and streamed logs (including
URL-encoded and base64 forms). GitHub access is scoped to selected repositories; only
`agent/<run-id>` branches are pushed, never `main`/`master`, never forced, and only when you
enable it per sandbox.

**Access** — single admin account (hashed password), `HttpOnly` `SameSite=Strict` session
cookie, CSRF header on mutating requests, session cookie **and** same-origin `Origin` checks
on the terminal WebSocket, login rate limiting, fail-closed when no password is set, a strict
Content-Security-Policy (same-origin scripts only, no framing), and an audit log of every
action including terminal open/close. Logging out ends that session for good (a copied cookie
stops working). Requests for any hostname other than Kiln's own are refused, so a web page
can't reach Kiln through a name of its own that resolves to your server (DNS rebinding).

**Server** — program files in `/opt/agent-hub` belong to root; Kiln's account can only write
its data, its settings and a build folder, so a sandbox escape can't plant code that root or
the service later runs. The account has no login shell. Preview file servers don't serve
`.git` (and the Python one hides every dotfile), since preview ports are open to your tailnet.

### Known risks

- **Prompt injection.** An agent can be manipulated by content it reads into doing things you
  didn't ask for *within its sandbox* — deleting work, writing bad code, or exfiltrating
  anything the sandbox can see (including the repo and the model credential) to the
  internet. The sandbox boundary and the network rules limit the blast radius; they do not
  make the agent trustworthy. Review every diff before approving.
- **The agent can use Claude while its run lasts.** It can't read your token, but anything in
  the sandbox can send requests through the gateway with the run's key until the run ends.
- **The agent can edit its own log.** Its output files in `/tmp/kiln-runs` belong to the
  sandbox user, so it could rewrite what Kiln shows (it could already write to its own output
  stream). The diff, computed on the host, is what to trust.
- **Open egress.** Sandboxes may contact any public host. A domain allowlist (egress proxy)
  is planned, not built.
- **Container escape.** Rootless Docker limits the damage of a kernel or runtime escape to the
  `agenthub` user. gVisor is a possible further hardening step.

## Server hardening

```bash
sudo bash deploy/security-audit.sh   # read-only snapshot: firewall, open ports, SSH, updates, Kiln
```

- **Reboot after kernel updates** (`/var/run/reboot-required`): a container escape usually goes
  through the kernel, so its fixes matter most here.
- **SSH through Tailscale.** Turn on Tailscale SSH (`sudo tailscale set --ssh`, plus an `ssh`
  rule in your tailnet policy), check that `ssh you@server` from your laptop logs in without a
  password, then `sudo bash deploy/ssh-harden.sh` turns off password logins, root logins and X11
  forwarding (`... ssh-harden.sh undo` reverts). LAN SSH then needs a key; the console always works.

## Roadmap

0. Preparation — Docker, caps, scaffold ✓
1. Sandbox API — templates, lifecycle, limits, egress rules, auth, audit log, systemd ✓
2. Live terminal and minimal web UI ✓
3. Agent runs — runner interface, branch workflow, streaming logs, cancel/timeout, redaction ✓
4. Files, diff review, previews, resource display, disk checks and clean-up, phone layout ✓
5. Windows app, iPhone home-screen app, notifications, HTTPS ✓
6. Hardening — Claude gateway, per-sandbox networks, host-side diff, disk guard, root-owned
   install, session revocation, host check *(current)*

**Later:** gVisor, egress domain allowlist, opening pull requests, multiple runners, run
templates, scheduled runs.
