# Agent Hub sandbox: Node.js (24 LTS). Base pinned by digest for reproducible builds.
FROM node:24-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git less netcat-openbsd procps \
 && rm -rf /var/lib/apt/lists/*

# The base image ships a "node" user with UID 1000; rename it so every template runs as "agent".
RUN usermod --login agent --home /home/agent --move-home node \
 && groupmod --new-name agent node \
 && install -d -o agent -g agent /workspace

# Claude Code, the agent CLI: a pinned release, verified against the checksum below. It is
# root-owned, so the agent can't modify or update its own binary; to upgrade, change both
# values (from downloads.claude.ai/claude-code-releases/<version>/manifest.json) and bump
# images/VERSION.
ARG CLAUDE_CODE_VERSION=2.1.315
ARG CLAUDE_CODE_SHA256=a3b9d66caf61b40d6aad99ed4de80f48f17e0e024ddf1b6e4289efade06105df
RUN curl -fsSL -o /usr/local/bin/claude \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-x64/claude" \
 && echo "${CLAUDE_CODE_SHA256}  /usr/local/bin/claude" | sha256sum -c --quiet - \
 && chmod 755 /usr/local/bin/claude
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1

# busybox provides `httpd` for the one-click static preview server (separate layer, so the
# Claude Code layer above stays cached).
RUN apt-get update \
 && apt-get install -y --no-install-recommends busybox \
 && rm -rf /var/lib/apt/lists/*

USER agent
WORKDIR /workspace
ENV NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.npm-global/bin:$PATH \
    NPM_CONFIG_UPDATE_NOTIFIER=false
LABEL org.opencontainers.image.title="agent-hub/node"
CMD ["sleep", "infinity"]
