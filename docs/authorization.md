# Authorization model (RBAC + object scope)

Phase 9. Sources of truth: Vol 3 §9.3 (TITAN-BCK-021), Vol 8 §3.4/§4 (TITAN-SEC-030/032).

## One enforcement point

Authorization is the per-route `Requires("resource:action")` dependency
(`titan.gateway.rbac`) — spec §3.4's "one enforcement point: a FastAPI
dependency". The RBAC middleware slot from §9.1 is a documented pass-through;
building a second net there would be the "second mechanism" the spec forbids.

`scripts/check_route_guards.py` (wired into `make lint` and CI) fails the
build if any registered route — recursing into included routers and mounts —
lacks a `Requires`/`RequiresAuth` guard and is not explicitly `mark_public`.

## Permission model

Permission strings are `resource:action`; roles are additive bundles in
`roles.permissions` (§9.3), org-wide or per-project via `user_roles.project_id`.
A required `r:a` is granted by `*`, `r:*`, `r:a`, or `*:a` — EXCEPT `*:a` does
not reach `SENSITIVE_RESOURCES` (`audit`, `secret`): the cross-resource read
wildcard cannot read the audit log (Vol 8 §4.2 grants audit read to Owner +
Admin only; only an explicit grant or the full `*` owner wildcard reaches it).

Agents (machine principals) have NO org-wide authority: their operator bundle
applies only to their bound project (TITAN-BCK-021). Until the agent→project
binding lands with the agents table (Phase 26) an agent is fail-closed — 404
on every project route, no cross-project access.

## Object-level authorization (no IDOR)

A resource outside the caller's scope returns **404, never 403**, so existence
is not leaked (TITAN-SEC-032). Two halves:

1. **Path scope** — `Requires(perm, project_param="pid")`: a project the caller
   has no permission in returns 404; visible-but-underpowered returns 403.
2. **Object scope** — `require_object_in_project(...)`: handlers that load an
   object by id MUST call it so an object belonging to a different project than
   the path returns 404.

**Phase-11 requirement:** the object-scope half is a mandatory handler call the
route-guard lint cannot see. When the first domain resource-instance routes
land, add a CI check that any `Requires(project_param=...)` route with a second
object-id path param references `require_object_in_project` (or a repository
method proven to filter by the path project), so object-level IDOR protection
is enforced, not merely available.

## Known spec reconciliation

Vol 3 §9.3 names five roles (owner/admin/maintainer/operator/viewer); Vol 8
§4.2 uses Owner/Admin/Maintainer/**Developer**/Viewer. The seed follows §9.3
(the phase's directed source); §9.3's `operator` is the agent/machine bundle,
narrower than §4.2's human Developer. Admin org member/role management (§4.2)
lands with org-management routes in a later phase.
