"use client";

import { useRef, useState } from "react";
import { Search } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ConsentDialog } from "@/components/plugins/consent-dialog";
import { McpServersTab } from "@/components/plugins/mcp-tab";
import { PluginConfigDialog } from "@/components/plugins/config-dialog";
import { useCan } from "@/lib/auth";
import { usePrimarySearch } from "@/lib/shortcuts/primary-search";
import {
  availableFrom,
  matchesQuery,
  toRow,
  type HealthLevel,
  type PluginRow,
} from "@/lib/plugins/derive";
import {
  isRuntimeDisabled,
  useInstalledPlugins,
  usePluginCatalogue,
  usePluginLifecycle,
} from "@/lib/plugins/hooks";
import { errorMessage } from "@/lib/settings/errors";
import type { PluginOut } from "@titan/api-client";

/**
 * Plugin Manager (Vol 4 §6.12).
 *
 * Three tabs, as specified. What the spec assumes and this product does NOT
 * have, stated once here rather than faked:
 *
 * * **`plugin.status_changed` does not exist.** Zero producers anywhere, and no
 *   WS handler that would route it. Health chips therefore poll (15 s).
 * * **The "catalog" is the local plugins directory**, not a marketplace. There
 *   is no registry fetch in the product, so §6.12's "catalog fetch failure
 *   falls back to the bundled offline catalog" is already the only behaviour:
 *   the bundled catalogue IS the catalogue.
 * * **The runtime is off by default** (`TITAN_PLUGINS_ENABLED=false`), so most
 *   lifecycle routes answer 503 on a stock stack. That is a legitimate state
 *   with its own rendering, not an error.
 */
export function PluginsScreen() {
  const installs = useInstalledPlugins();
  const catalogue = usePluginCatalogue();
  const canAdmin = useCan("plugin:admin");
  const [consentTarget, setConsentTarget] = useState<PluginOut | null>(null);
  const [configTarget, setConfigTarget] = useState<string | null>(null);
  const lifecycle = usePluginLifecycle();

  // §10: `/` focuses "the screen's primary search/filter". This screen had no
  // filter at all, so `/` opened the palette instead — and a plugin directory
  // with a few dozen entries is exactly where a filter earns its place.
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  usePrimarySearch(searchRef);

  const allRows = (installs.data ?? [])
    .filter((install) => install.state !== "removed")
    .map(toRow);
  const allAvailable = availableFrom(catalogue.data ?? [], installs.data ?? []);

  // Filtered for display; the UNFILTERED counts stay out of the tab labels
  // below, because a tab reading "Installed (0)" while three plugins are merely
  // filtered out says the wrong thing about the deployment.
  const rows = allRows.filter((row) =>
    matchesQuery(
      { id: row.pluginId, name: row.name, description: row.description },
      query,
    ),
  );
  const available = allAvailable.filter((plugin) =>
    matchesQuery(
      {
        id: plugin.plugin_id,
        name: plugin.name,
        description: plugin.description ?? "",
      },
      query,
    ),
  );
  const filtering = query.trim() !== "";

  if (installs.isLoading) {
    return (
      <div className="space-y-4 p-6">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  // The list endpoints read the DB directly and never touch the supervisor, so
  // a disabled runtime still lists. Only the lifecycle actions 503 — which is
  // why this banner sits above the tabs rather than replacing them.
  const runtimeOff =
    isRuntimeDisabled(installs.error) || isRuntimeDisabled(catalogue.error);
  // A failed fetch is NOT an empty list. Without this, a 403 or a network
  // failure rendered "No plugins installed — browse the catalog", which reads
  // as a fact about the deployment rather than as a failure to find out.
  const loadFailed = installs.isError && !runtimeOff;

  // Returns whether it worked, so a caller can branch. Swallowing the outcome
  // meant the consent flow opened the config dialog even when the install had
  // failed — producing a second error for a plugin that does not exist.
  const act = async (
    pluginId: string,
    action: "install" | "enable" | "disable",
  ): Promise<boolean> => {
    try {
      await lifecycle.mutateAsync({ pluginId, action });
      toast.success(`${pluginId} ${action}d`);
      return true;
    } catch (error) {
      toast.error(errorMessage(error));
      return false;
    }
  };

  return (
    <div className="space-y-6 p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Plugins</h1>
          <p className="text-xs text-muted-foreground">
            Connectors, MCP servers and the tools they contribute.
          </p>
        </div>
        {/* One filter across all three tabs, so `/` has a single unambiguous
            target however the user got here. `w-full sm:w-64`: at 375px a
            fixed-width input beside a heading is the thing that overflows. */}
        <div className="relative w-full sm:w-64">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            ref={searchRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter plugins…"
            aria-label="Filter plugins"
            // §8's 44px touch minimum below md.
            className="h-11 pl-7 text-xs md:h-9"
          />
        </div>
      </header>

      {runtimeOff ? (
        <p
          role="status"
          className="rounded-md border border-dashed p-4 text-sm text-muted-foreground"
        >
          <span className="font-medium text-foreground">
            The plugin runtime is switched off on this deployment.
          </span>{" "}
          Installed plugins still list, but installing, enabling and configuring
          need <code className="font-mono">TITAN_PLUGINS_ENABLED=true</code> and
          a restart.
        </p>
      ) : null}

      <Tabs defaultValue="installed">
        <TabsList>
          <TabsTrigger value="installed">
            Installed {allRows.length > 0 ? `(${allRows.length})` : ""}
          </TabsTrigger>
          <TabsTrigger value="catalog">Catalog</TabsTrigger>
          <TabsTrigger value="mcp">MCP Servers</TabsTrigger>
        </TabsList>

        <TabsContent value="installed" className="space-y-2 pt-4">
          {loadFailed ? (
            <p
              role="status"
              className="rounded-md border bg-card p-4 text-sm text-destructive"
            >
              Couldn’t load installed plugins. {errorMessage(installs.error)}{" "}
              <button
                className="underline"
                onClick={() => void installs.refetch()}
              >
                Retry
              </button>
            </p>
          ) : rows.length === 0 ? (
            // "Nothing matched your filter" and "nothing is installed" are
            // different facts about the deployment, and showing the second when
            // the first is true is the same mistake the `loadFailed` branch
            // above exists to avoid.
            <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
              {filtering && allRows.length > 0
                ? `No installed plugin matches “${query.trim()}”.`
                : "No plugins installed — browse the catalog."}
            </p>
          ) : (
            rows.map((row) => (
              <InstalledRow
                key={row.pluginId}
                row={row}
                canAdmin={canAdmin}
                pending={lifecycle.isPending}
                onConfigure={() => setConfigTarget(row.pluginId)}
                onToggle={() =>
                  void act(
                    row.pluginId,
                    row.state === "enabled" ? "disable" : "enable",
                  )
                }
              />
            ))
          )}
        </TabsContent>

        <TabsContent value="catalog" className="space-y-2 pt-4">
          {catalogue.isError && !runtimeOff ? (
            <p
              role="status"
              className="rounded-md border bg-card p-4 text-sm text-destructive"
            >
              Couldn’t load the catalog. {errorMessage(catalogue.error)}{" "}
              <button
                className="underline"
                onClick={() => void catalogue.refetch()}
              >
                Retry
              </button>
            </p>
          ) : available.length === 0 ? (
            <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
              {filtering && allAvailable.length > 0 ? (
                `No catalog entry matches “${query.trim()}”.`
              ) : (
                <>
                  Everything discovered is already installed. The catalog is the
                  host’s plugins directory — TITAN has no remote registry.
                </>
              )}
            </p>
          ) : (
            available.map((plugin) => (
              <CatalogCard
                key={plugin.plugin_id}
                plugin={plugin}
                canAdmin={canAdmin}
                onInstall={() => setConsentTarget(plugin)}
              />
            ))
          )}
        </TabsContent>

        <TabsContent value="mcp" className="pt-4">
          <McpServersTab
            rows={rows.filter((row) => row.isMcp)}
            canAdmin={canAdmin}
            filtering={filtering}
          />
        </TabsContent>
      </Tabs>

      <ConsentDialog
        plugin={consentTarget}
        open={consentTarget !== null}
        pending={lifecycle.isPending}
        onCancel={() => setConsentTarget(null)}
        onConfirm={async () => {
          const target = consentTarget;
          if (!target) return;
          setConsentTarget(null);
          // Install then configure, per §6.12's flow: consent → config form →
          // test → enabled. Enabling is a separate, explicit act. Only advance
          // when the install actually SUCCEEDED — otherwise the config dialog
          // opens for a plugin that was never installed and immediately errors
          // again, burying the real message under a second one.
          if (await act(target.plugin_id, "install")) {
            setConfigTarget(target.plugin_id);
          }
        }}
      />

      <PluginConfigDialog
        pluginId={configTarget}
        open={configTarget !== null}
        onClose={() => setConfigTarget(null)}
      />
    </div>
  );
}

const HEALTH_MARK: Record<HealthLevel, string> = {
  healthy: "✔",
  error: "✖",
  idle: "◐",
  unknown: "?",
};

function InstalledRow({
  row,
  canAdmin,
  pending,
  onConfigure,
  onToggle,
}: {
  readonly row: PluginRow;
  readonly canAdmin: boolean;
  readonly pending: boolean;
  readonly onConfigure: () => void;
  readonly onToggle: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-card p-3">
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2 text-sm font-medium">
          {/* Never colour alone (TITAN-FRT-044): the mark and the text carry
              the state, so it survives greyscale and colour blindness. */}
          <span aria-hidden>{HEALTH_MARK[row.health]}</span>
          {row.name}
          <span className="font-mono text-xs text-muted-foreground">
            v{row.version}
          </span>
          {row.isMcp ? <Badge variant="outline">MCP</Badge> : null}
        </p>
        <p className="truncate text-xs text-muted-foreground">
          {row.healthLabel}
          {row.tools.length > 0
            ? ` · ${row.tools.length} tool${row.tools.length === 1 ? "" : "s"}`
            : ""}
          {row.panels > 0 ? ` · ${row.panels} panel` : ""}
        </p>
      </div>

      <Badge variant={row.state === "enabled" ? "default" : "secondary"}>
        {row.state}
      </Badge>

      {canAdmin ? (
        <div className="flex gap-1">
          <Button
            size="sm"
            variant="outline"
            disabled={pending}
            onClick={onToggle}
          >
            {row.state === "enabled" ? "Disable" : "Enable"}
          </Button>
          {row.hasConfig ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={onConfigure}
              aria-label={`Configure ${row.name}`}
            >
              ⚙
            </Button>
          ) : null}
        </div>
      ) : (
        // A disabled Button with an onClick it can never fire is dead UI, and
        // `disabled:pointer-events-none` makes its title unreachable anyway.
        // A plain span keeps the reason hoverable.
        <span
          className="text-xs text-muted-foreground"
          title="Installing and enabling plugins is an operator act — it needs the owner or admin role."
        >
          admin only
        </span>
      )}
    </div>
  );
}

function CatalogCard({
  plugin,
  canAdmin,
  onInstall,
}: {
  readonly plugin: PluginOut;
  readonly canAdmin: boolean;
  readonly onInstall: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-card p-3">
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">
          {plugin.name}{" "}
          <span className="font-mono text-xs text-muted-foreground">
            v{plugin.plugin_version}
          </span>
        </p>
        <p className="text-xs text-muted-foreground">
          {plugin.description || "No description."}
        </p>
      </div>
      {canAdmin ? (
        <Button size="sm" onClick={onInstall}>
          Install
        </Button>
      ) : (
        <span
          className="text-xs text-muted-foreground"
          title="Installing a plugin grants it capabilities — owner or admin only."
        >
          admin only
        </span>
      )}
    </div>
  );
}
