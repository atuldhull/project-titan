"use client";

/**
 * Deployment Dashboard (Vol 4 §6.9, TITAN-FRT-032).
 *
 * This is the screen where the spec and the system diverge most, so what is
 * real is worth stating precisely:
 *
 * - **Real**: the deployment records themselves. `deployments` holds one row
 *   per Compose stack per project — name, status (pending|running|failed|
 *   stopped, CHECK-constrained), health (unknown|green|red), url, port,
 *   services, and a `detail` string that carries the last failure's reason.
 *   That is what the env cards show.
 * - **Not real — environments.** There is no environment column. Every deploy
 *   is a local Compose stack via a DinD sidecar; `local/staging/prod` cards
 *   have no backing, and the phase brief agrees ("do not fabricate staging/prod
 *   backends"). The cards are therefore the STACKS that exist.
 * - **Not real — version, uptime, pipeline runs.** There is no version column
 *   (`version` is the optimistic-lock counter), no started_at, and no run with
 *   stages: the Phase-41 pipeline is one mutable cursor per project whose 12
 *   macro stages are ceo_vision…handover, not build/test/scan/deploy.
 * - **Not real — Promote/Rollback/Abort.** No endpoints exist, and the deploy
 *   tool is reachable only from inside an agent turn. Rollback is not merely
 *   unimplemented but currently *unimplementable*: `upsert_pending` overwrites
 *   the row in place, so no prior record is kept — and even with history, an
 *   older revision names the same `compose_file` PATH, so redeploying it would
 *   be byte-identical to redeploying now. A real rollback needs the deployed
 *   commit recorded and the workspace restored to it.
 * - **Not real — the 🔒.** See `lib/deployments/gate`: no release can be in an
 *   awaiting-approval state.
 *
 * What IS delivered against FRT-032 is the gate: the RBAC rule and the typed
 * confirmation, decided in one pure tested place, wired to the one destructive
 * action that has a domain function behind it. The buttons are disabled with
 * their reason rather than absent, because "why can I not do this" is the
 * question this screen exists to answer.
 */
import { ExternalLink, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useCan } from "@/lib/auth/use-can";
import { DEPLOY_PERMISSION, evaluateGate } from "@/lib/deployments/gate";
import { useProjectDeployments } from "@/lib/deployments/hooks";
import { cn } from "@/lib/utils";

/** §6.9's health dot. TITAN-FRT-044: never colour alone. */
const HEALTH: Record<
  string,
  { icon: string; className: string; label: string }
> = {
  green: {
    icon: "●",
    className: "text-emerald-600 dark:text-emerald-400",
    label: "healthy",
  },
  red: { icon: "✖", className: "text-destructive", label: "unhealthy" },
  unknown: { icon: "◌", className: "text-muted-foreground", label: "unknown" },
};

const STATUS: Record<string, string> = {
  running: "text-emerald-600 dark:text-emerald-400",
  pending: "text-amber-600 dark:text-amber-400",
  failed: "text-destructive",
  stopped: "text-muted-foreground",
};

export function DeploymentsScreen({
  projectId,
}: {
  readonly projectId: string;
}) {
  const deployments = useProjectDeployments(projectId);
  const canDeploy = useCan(DEPLOY_PERMISSION);

  // FRT-032's first clause: a viewer sees no actions at all. `useCan` reads the
  // session's ORG-WIDE roles, so it can only ever hide — never reveal — which
  // is the safe direction (the server re-checks regardless).
  const gate = evaluateGate("teardown", "local", () => canDeploy);

  if (deployments.isLoading) {
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-8 w-48" />
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>
    );
  }

  if (deployments.isError) {
    return (
      <div className="p-6">
        <p className="rounded-md border bg-card p-4 text-sm text-destructive">
          Couldn’t load deployments.{" "}
          <button className="underline" onClick={() => deployments.refetch()}>
            Retry
          </button>
        </p>
      </div>
    );
  }

  const rows = deployments.data ?? [];

  return (
    <div className="space-y-6 p-6">
      <header>
        <h1 className="text-xl font-semibold">Deployments</h1>
        <p className="text-xs text-muted-foreground">
          Local Compose stacks the DevOps agent has deployed for this project.
        </p>
      </header>

      {rows.length === 0 ? (
        <p className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          No deployments yet — the DevOps Agent deploys after the first passing
          release build.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((d) => {
            const health = HEALTH[d.health] ?? HEALTH.unknown;
            return (
              <li key={d.id} className="rounded-lg border bg-card p-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-mono text-sm font-medium">
                    {d.name}
                  </span>
                  <span
                    className={cn(
                      "flex items-center gap-1 text-xs",
                      health.className,
                    )}
                  >
                    <span aria-hidden>{health.icon}</span>
                    {health.label}
                  </span>
                  <Badge
                    variant="outline"
                    className={cn("text-[10px]", STATUS[d.status])}
                  >
                    {d.status}
                  </Badge>
                  {d.url ? (
                    <a
                      href={d.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="flex items-center gap-1 text-xs text-sky-600 hover:underline dark:text-sky-400"
                    >
                      {d.url}
                      <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                  ) : (
                    // url/port are cleared on failure and teardown, so a dead
                    // URL is never advertised.
                    <span className="text-xs text-muted-foreground">
                      no URL
                    </span>
                  )}

                  <div className="ml-auto flex items-center gap-2">
                    {/* Shown only to owner/admin (FRT-032's "viewer sees no
                        actions"), but always disabled: there is no teardown
                        endpoint — the deploy tool runs inside an agent turn,
                        against a DinD sidecar the API container cannot reach. It
                        is NOT wired to the confirm dialog, because a disabled
                        button that opens a dialog would be dead UI; the gate and
                        the typed-confirm dialog are exercised by their own tests
                        and the Storybook story, where they are real. */}
                    {gate.allowed ? (
                      <span
                        className="inline-flex h-7 cursor-not-allowed items-center gap-1 rounded-md border px-2 text-xs text-muted-foreground opacity-60"
                        title="No teardown endpoint exists: the deploy tool runs inside an agent turn, against a DinD sidecar the API container cannot reach."
                      >
                        <RotateCcw className="h-3 w-3" aria-hidden />
                        Teardown
                      </span>
                    ) : null}
                  </div>
                </div>

                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
                  <dt className="text-muted-foreground">services</dt>
                  <dd className="font-mono">
                    {d.services.length > 0 ? d.services.join(", ") : "—"}
                  </dd>
                  <dt className="text-muted-foreground">compose file</dt>
                  <dd className="truncate font-mono">{d.compose_file}</dd>
                  <dt className="text-muted-foreground">last change</dt>
                  <dd>{new Date(d.updated_at).toLocaleString()}</dd>
                  {d.detail ? (
                    <>
                      {/* §6.9's "rollback causes" — the nearest real thing:
                          the last status change's reason. */}
                      <dt className="text-muted-foreground">detail</dt>
                      <dd className="text-destructive">{d.detail}</dd>
                    </>
                  ) : null}
                </dl>
              </li>
            );
          })}
        </ul>
      )}

      <section className="space-y-2 rounded-lg border border-dashed p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          Promote, Rollback and Abort are not available.
        </p>
        <p>
          Every deployment here is a local Docker Compose stack — there is no
          environment, version or pipeline-run model behind §6.9’s staging/prod
          cards, and no REST endpoint drives the deploy tool (it runs inside an
          agent turn, against a DinD sidecar). Rollback in particular needs the
          deployed commit recorded: a deployment row is overwritten in place, so
          there is no previous version to return to.
        </p>
      </section>
    </div>
  );
}
