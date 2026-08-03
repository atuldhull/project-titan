# `/evolution/**` — human-gated self-modification (Volume 10)

This tree is the single, git-versioned source of truth for everything TITAN may
change *about itself*: agent prompts, routing policy, and (in later phases)
scoring weights, benchmark definitions, and agent specs. Every change lands only
through a pull request (an **Evolution Proposal**, ADR-EVO-01) reviewed and merged
by a human holding the `evolution:approve` RBAC permission. Nothing here is
mutable at runtime, and no prompt text is ever stored in the database.

## Layout

```
evolution/
  manifest.yaml          # the pinned, approved champion (path + sha256 + approval per artifact)
  prompts/
    _base/system.md      # the shared 7-part skeleton every role extends
    <agent>/system.md    # one registry prompt per agent (YAML front-matter + Jinja body)
  routing/policy.yaml    # the versioned routing-policy artifact (§6.4)
  proposals/             # Evolution Proposals (EP-TEMPLATE.md + one file per EP)
```

Each prompt file is `---`-fenced YAML front-matter (`id`, `semver`, `variables`,
`model_constraints`, `eval_requirements`) followed by the Jinja template body. The
composer strips the front-matter and renders the body with exactly the declared
variables (`agent`, `memory`, `task_context`) — no prompt text is assembled
anywhere else (TITAN-EVO-015).

## The manifest is the runtime gate (TITAN-EVO-001/002)

`manifest.yaml` pins each artifact by **content sha256** — the *prompt SHA* logged
on every LLM span and usage-ledger row, so any task is traceable to the exact
prompt that served it. Each entry also carries the approval signature
(`approved_by` user id + signed `approved_at` + `ep` + `diff_url`).

At compose time the resolver (`titan.evolution.resolver`) **refuses**, failing
closed, to serve any prompt whose entry:

- lacks `approved_by`/`approved_at` (the EP is unapproved), or
- does not hash to its pinned `sha256` (the working tree drifted from what was
  approved).

Because the runtime can't phone GitHub to check a merge, the enforceable,
reversible source of truth is this in-repo manifest, whose approval fields a human
sets in the merged EP PR. A `git revert` of an evolution commit restores the prior
bytes byte-for-byte, and the reverted (previously approved) version is served
again — no redeploy gymnastics.

## The DB ledger is the audit record (TITAN-EVO-002)

`evolution_artifacts` (Postgres) mirrors the manifest — Git commit SHA + path +
content hash + approver — as an immutable, queryable record. It is synced from the
manifest at startup and appended to by the approve API
(`POST /api/v1/evolution/artifacts:approve`, `evolution:approve`, human-only),
which writes the row **and** an `evolution.approve` audit event carrying the actor,
artifact hash, and diff link (TITAN-EVO-001c). It never stores prompt text.

## Changing a prompt (the EP flow)

1. Branch. Edit `evolution/prompts/<agent>/system.md` (bump the front-matter
   `semver`).
2. Recompute its sha256 and update the matching `manifest.yaml` entry; set
   `approved_by`/`approved_at` when a permitted human signs off, and link the EP.
3. Open an EP PR (see `proposals/EP-TEMPLATE.md`). Agents may open, benchmark, and
   comment; only a human with `evolution:approve` may merge.
4. CI runs `make check-evolution` — the manifest must be pinned, approved, and
   drift-free, or the merge is blocked.
5. On merge + deploy, the resolver serves the new version and stamps its SHA on
   every span; `POST /artifacts:approve` records the human sign-off in the audit
   ledger.

## Deployment: the registry must be present at runtime

The app resolves the registry from `TITAN_AGENT_PROMPTS_DIR` (default
`evolution/prompts`, walked up to the repo root). It must exist at runtime:

- **Docker Compose** (the local-first primary): `evolution/` is bind-mounted
  read-only at `/app/evolution` for the `api` and `worker` services, alongside the
  existing `config/` mount. Read-only is deliberate — it closes the compose-time
  drift window entirely (the resolver re-verifies every turn regardless).
- **Kubernetes**: the base manifests (Phase 79) do **not** yet mount `config/` or
  `evolution/` into the app pods — a pre-existing limitation (the roster/model-policy
  under `config/` have the same requirement). Before running the app on k8s, both
  `config/` and `evolution/` must be projected in (ConfigMap or a read-only volume)
  and `TITAN_AGENT_PROMPTS_DIR` set. A missing/malformed manifest fails the pod's
  boot loudly (fail-toward-status-quo), so this surfaces immediately rather than
  silently serving nothing.

## Why the runtime gate is the manifest, not the DB

Scout-the-code reshaping (Phase 83): the composer is built synchronously and runs
on every agent turn, and the runtime has no way to verify a GitHub merge. So the
enforceable gate is the file that a merge produces — the manifest — read with no
per-turn DB round-trip. The DB/audit ledger is the compliance record of those
approvals, not a second prompt source.
