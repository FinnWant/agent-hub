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

# The agent CLI is added in phase 3, once its install method is verified against current docs.

USER agent
WORKDIR /workspace
ENV NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.npm-global/bin:$PATH \
    NPM_CONFIG_UPDATE_NOTIFIER=false
LABEL org.opencontainers.image.title="agent-hub/node"
CMD ["sleep", "infinity"]
