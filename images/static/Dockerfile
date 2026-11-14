# Agent Hub sandbox: static sites (HTML/CSS/JS). Small; busybox provides `httpd` for previews.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash busybox ca-certificates curl git less netcat-openbsd procps \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
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

USER agent
WORKDIR /workspace
LABEL org.opencontainers.image.title="agent-hub/static"
CMD ["sleep", "infinity"]
