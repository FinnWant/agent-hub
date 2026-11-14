# Agent Hub sandbox: Python. Base pinned by digest for reproducible builds.
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git less netcat-openbsd procps build-essential \
 && rm -rf /var/lib/apt/lists/*

# Non-root user the sandbox runs as (UID 1000; never root).
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
 && install -d -o agent -g agent /workspace

# The agent CLI is added in phase 3, once its install method is verified against current docs.

USER agent
WORKDIR /workspace
ENV PATH=/home/agent/.local/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="agent-hub/python"
CMD ["sleep", "infinity"]
