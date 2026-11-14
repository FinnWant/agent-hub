# Agent Hub

Run coding agents in isolated sandboxes on a home server, and steer them from a browser or a
desktop app over a private network.

Each sandbox is a locked-down container with its own workspace volume. An agent (Claude Code
in non-interactive mode, behind a pluggable runner) works on a branch inside it; you watch the
stream live, review the diff, and approve. Nothing is exposed to the public internet.

> **Status:** Phase 0 (preparation). Nothing runnable yet.

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

**Credentials** — secrets live only in a mode-600 `.env` and the agent process environment,
are redacted from logs and never sent to the frontend. GitHub access is scoped to selected
repositories; agents may push `agent/*` branches but never to `main`/`master` and never
force-push.

**Access** — single admin account (hashed password), `HttpOnly` `SameSite=Strict` session
cookie, CSRF header on mutating requests, `Origin` checks on WebSockets, login rate
limiting, fail-closed when no password is set, and an audit log of every action.

### Known risks

- **Prompt injection.** An agent can be manipulated by content it reads into doing things you
  didn't ask for *within its sandbox* — deleting work, writing bad code, or exfiltrating
  anything the sandbox can see (including the repo and the model credential) to the
  internet. The sandbox boundary and the network rules limit the blast radius; they do not
  make the agent trustworthy. Review every diff before approving.
- **Open egress.** Sandboxes may contact any public host. A domain allowlist (egress proxy)
  is planned, not built.
- **Container escape.** Rootless Docker limits the damage of a kernel or runtime escape to the
  `agenthub` user. gVisor is a possible further hardening step.

## Roadmap

0. Preparation — Docker, caps, scaffold *(current)*
1. Sandbox API — templates, lifecycle, limits, egress rules, auth, audit log, systemd
2. Live terminal and minimal web UI
3. Agent runs — runner interface, branch workflow, streaming logs, cancel/timeout, redaction
4. Files, diff review, previews, disk checks, phone layout
5. Tauri desktop app with native notifications

**Later:** gVisor, egress domain allowlist, opening pull requests, multiple runners, run
templates, scheduled runs.
