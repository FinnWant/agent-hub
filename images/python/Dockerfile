# Agent Hub sandbox: Python. Base pinned by digest for reproducible builds.
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git less netcat-openbsd procps build-essential \
 && rm -rf /var/lib/apt/lists/*

# Non-root user the sandbox runs as (UID 1000; never root).
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
ENV PATH=/home/agent/.local/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="agent-hub/python"
CMD ["sleep", "infinity"]
