"""Per-task rootless Docker-in-Docker sidecar for the ``docker.*`` tools
(Phase 22; Vol 7 §4.3, TITAN-SEC-044).

The docker.* tools never touch the HOST Docker daemon. Instead each task gets a
rootless DinD sidecar container (its dockerd runs as a non-root user inside a
user namespace); the tools exec the docker CLI *inside* that sidecar against its
own daemon socket. The host socket is never mounted or reachable (SEC-044), and
containers the sidecar starts are host-reachable only through the fixed set of
ports the controller pre-publishes on the sidecar."""
