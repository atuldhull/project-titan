# infra/

Deployment and platform infrastructure (Vol 3 §13.2, Vol 9):

- `docker/` — hardened service Dockerfiles (Phase 2)
- `keycloak/` — realm import for the dev IdP (Phase 67 hardening; Compose wiring Phase 2)
- `grafana/`, `prometheus/`, `loki/`, `otel/` — observability stack (Phase 4)
- `k8s/` — production manifests (Phase 79+, optional)
