# titan-sandbox — the per-task code-execution sandbox base (Phase 21).
# Alpine + the offline toolchain the core git/terminal/code tools need
# (Vol 7 §5): git, python3, node/npm, coreutils. Runs non-root uid 10001 with
# ALL the §5 hardening applied by the Sandbox Controller at RUN time (caps
# dropped, read-only rootfs, deny-all network, seccomp, cgroup limits) — the
# image itself just carries the tools.
# Build:  docker build -f infra/docker/titan-sandbox.Dockerfile -t titan-sandbox:local .
# syntax=docker/dockerfile:1

FROM alpine:3.20

RUN apk add --no-cache \
      git \
      python3 \
      py3-pip \
      nodejs \
      npm \
      coreutils \
      bash

# The constitution CI enforcement kit (Phase 45, Vol 6 §6). Baked into the image
# rather than pip-installed at use time because the sandbox runs with deny-all
# egress: there is no index to install from. titan/ci_kit is stdlib-only and
# imports nothing else from titan (enforced by the "CI kit is self-contained"
# import-linter contract), so copying the package root's __init__ plus the kit
# is sufficient for `python3 -m titan.ci_kit...` to resolve.
COPY apps/api/src/titan/__init__.py /opt/titan-ci-kit/titan/__init__.py
COPY apps/api/src/titan/ci_kit /opt/titan-ci-kit/titan/ci_kit
ENV PYTHONPATH=/opt/titan-ci-kit

# The one command the DevOps agent runs via the Phase-21 terminal tool.
RUN printf '#!/bin/sh\nexec python3 -m titan.ci_kit "$@"\n' > /usr/local/bin/titan-ci-init \
    && chmod 0755 /usr/local/bin/titan-ci-init

# A non-root identity matching the controller's default user (10001). The
# controller still enforces user 10001 at runtime; this makes the home dir and
# git config writable under /tmp when needed.
RUN adduser -D -u 10001 titan || true
ENV HOME=/tmp

# Keep the container alive for the task; the controller overrides the command.
CMD ["sleep", "86400"]
