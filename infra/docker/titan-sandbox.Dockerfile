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

# A non-root identity matching the controller's default user (10001). The
# controller still enforces user 10001 at runtime; this makes the home dir and
# git config writable under /tmp when needed.
RUN adduser -D -u 10001 titan || true
ENV HOME=/tmp

# Keep the container alive for the task; the controller overrides the command.
CMD ["sleep", "86400"]
