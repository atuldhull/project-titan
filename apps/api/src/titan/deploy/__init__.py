"""Local deployment of generated apps — the ``titan.local`` connector's home
(Vol 7 §6.7, TITAN-INT-046; Vol 1 TITAN-FND-027).

A generated project is self-contained: it ships its own ``docker-compose.yml`` and
runs by itself against the workspace's rootless DinD sidecar (Phase 22), pulling
base images through the local registry mirror (Phase 23) — so the default deploy
target is free, local, and needs zero cloud credentials. This module owns the
persistent *deployment record* (status + reachable URL) and orchestrates
bring-up → health-check → record over injected ``ComposeDeployer``/``Prober``
ports, keeping the domain free of any Docker/tool import (the ports are
implemented in the tool layer, so the ``deploy.compose`` tool streams live logs
while this stays hermetically testable).

Only ``service`` crosses the module boundary (TITAN-BCK-004); the models,
repository, and router stay private."""
