"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCan } from "@/lib/auth";
import { errorMessage } from "@/lib/settings/errors";
import {
  type LlmSettingsPatch,
  useClearLlmOverrides,
  useLlmSettings,
  useTestProvider,
  useUpdateLlmSettings,
} from "@/lib/settings/hooks";
import type { ProviderTestResult } from "@titan/api-client";

/**
 * Settings → LLM Providers (Vol 4 §6.13, TITAN-FRT-036).
 *
 * **What "switching the provider without a redeploy" rests on.** The gateway
 * rebuilds its router from `ConfigLoader.get()` on every request, and that
 * loader re-reads on mtime change. Saving here writes
 * `config/models.overrides.yaml`, which the loader already watches — so the
 * next agent turn, in this process AND in the worker, resolves the new
 * provider. Nothing restarts and nothing is cached in a way a second process
 * would miss.
 *
 * **Keys are never shown and never entered here.** A provider's `api_key` is a
 * `${ENV_VAR}` reference resolved at load time (Vol 8 SEC-050 keeps secret
 * material out of application storage), so there is no field to type one into
 * and no endpoint that would accept it. The screen reports whether a key
 * RESOLVED, plus a four-character hint, and names the environment variable an
 * operator would set. An input that pretended to save a key would have nowhere
 * honest to put it.
 *
 * §6.13's wireframe shows per-provider `[Add]` buttons for exactly that. They
 * are absent on purpose, with the reason stated in the UI.
 */
/** Radix Select cannot hold an empty-string value, so "no profile" needs a
 *  sentinel of its own. Mapped back to "" on save, which the API reads as
 *  "explicitly cleared" — distinct from "never set", where the process default
 *  still applies. */
const NO_PROFILE = "__default__";

/** The profiles `config/model-policy.yaml` actually defines. Listed here
 *  because the policy file sits behind an lru_cache and has no HTTP surface of
 *  its own; a deployment that adds a profile adds it here too. Selecting one
 *  the file does not define resolves to the tier defaults rather than failing,
 *  so the worst case is a no-op, never a broken gateway. */
const PROFILES = [
  {
    value: "local-free",
    label: "local-free — Ollama for every tier (offline)",
  },
] as const;

export function ProvidersScreen() {
  const settings = useLlmSettings();
  const update = useUpdateLlmSettings();
  const clear = useClearLlmOverrides();
  const test = useTestProvider();
  const canWrite = useCan("settings:write");
  const [results, setResults] = useState<Record<string, ProviderTestResult>>(
    {},
  );

  if (settings.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-48 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (settings.isError || !settings.data) {
    return (
      <p role="status" className="rounded-md border bg-card p-4 text-sm">
        Couldn’t load gateway settings. {errorMessage(settings.error)}{" "}
        <button className="underline" onClick={() => void settings.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  // Every field with a server-side default is OPTIONAL in the generated types
  // (a pydantic `default_factory` field is not `required` in the OpenAPI
  // schema). Normalising once here keeps `?? []` out of the JSX, where a fresh
  // array per render would defeat any memo placed downstream of it later.
  const data = settings.data;
  const aliases = data.aliases ?? [];
  const providers = data.providers ?? [];
  const byAgent = data.by_agent ?? {};
  const aliasOf = (name: string) =>
    aliases.find((a) => a.alias === name) ?? null;

  const save = async (patch: LlmSettingsPatch) => {
    try {
      await update.mutateAsync(patch);
      toast.success("Saved — the next agent action uses it");
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const runTest = async (alias: string) => {
    try {
      const result = await test.mutateAsync(alias);
      setResults((prev) => ({ ...prev, [alias]: result }));
      if (result.ok) {
        toast.success(`${alias}: ${result.latency_ms} ms`);
      } else {
        toast.error(`${alias}: ${result.detail}`);
      }
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const defaultAlias = aliasOf(data.default_alias);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Routing</CardTitle>
          <CardDescription>
            Which model serves an agent turn when nothing more specific applies.
            A change takes effect on the next call — no restart.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="agent-profile">Agent provider profile</Label>
            <Select
              value={data.agent_profile ?? NO_PROFILE}
              disabled={!canWrite || update.isPending}
              onValueChange={(value) =>
                void save({ agent_profile: value === NO_PROFILE ? "" : value })
              }
            >
              <SelectTrigger id="agent-profile" className="max-w-md">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PROFILE}>
                  Default tiers (per model-policy)
                </SelectItem>
                {PROFILES.map((profile) => (
                  <SelectItem key={profile.value} value={profile.value}>
                    {profile.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* This is the switch that actually moves agents, and burying it
                under "Default model" would leave an operator changing that and
                wondering why every agent still used the old provider: an agent
                turn NEVER falls through to routing.default, because it always
                passes an explicit alias resolved from model-policy. */}
            <p className="text-xs text-muted-foreground">
              This re-points <strong>agent</strong> traffic. Agents resolve
              their model from the policy tiers, so the default below does not
              move them — only this does.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="default-alias">Default model</Label>
            <Select
              value={data.default_alias}
              disabled={!canWrite || update.isPending}
              onValueChange={(value) => void save({ default_alias: value })}
            >
              <SelectTrigger id="default-alias" className="max-w-md">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {aliases.map((alias) => (
                  <SelectItem key={alias.alias} value={alias.alias}>
                    {alias.alias} — {alias.provider}/{alias.model}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {defaultAlias ? (
              <p className="text-xs text-muted-foreground">
                Currently routing to{" "}
                <span className="font-medium text-foreground">
                  {defaultAlias.provider}
                </span>{" "}
                <span className="font-mono">{defaultAlias.model}</span>.
              </p>
            ) : null}
            {!canWrite ? (
              <p className="text-xs text-muted-foreground">
                Read-only — changing gateway routing needs the owner or admin
                role.
              </p>
            ) : null}
          </div>

          {data.overridden ? (
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-dashed p-3">
              <p className="flex-1 text-xs text-muted-foreground">
                Some of this comes from Settings rather than from the operator’s{" "}
                <code className="font-mono">models.yaml</code>. Reverting
                discards every override at once.
              </p>
              <Button
                size="sm"
                variant="outline"
                disabled={!canWrite || clear.isPending}
                onClick={async () => {
                  try {
                    await clear.mutateAsync();
                    toast.success("Reverted to models.yaml");
                  } catch (error) {
                    toast.error(errorMessage(error));
                  }
                }}
              >
                Revert to defaults
              </Button>
            </div>
          ) : null}

          {Object.keys(byAgent).length > 0 ? (
            <div className="space-y-1">
              <Label>Per-agent overrides</Label>
              <ul className="space-y-0.5 text-xs text-muted-foreground">
                {Object.entries(byAgent).map(([agent, alias]) => (
                  <li key={agent} className="font-mono">
                    {agent} → {alias}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Providers</CardTitle>
          <CardDescription>
            Connection details come from{" "}
            <code className="font-mono">config/models.yaml</code>; keys are{" "}
            <code className="font-mono">${"{ENV_VAR}"}</code> references
            resolved from the host environment and are never stored by TITAN.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Provider</TableHead>
                <TableHead>Endpoint</TableHead>
                <TableHead>Key</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {providers.map((provider) => (
                <TableRow key={provider.name}>
                  <TableCell className="font-medium">{provider.name}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {provider.base_url ?? "provider default"}
                  </TableCell>
                  <TableCell>
                    {/* Three distinct states, and conflating the last two is
                        the bug worth avoiding: a keyless local runtime
                        (Ollama) is READY, not misconfigured. */}
                    {!provider.key_required ? (
                      <Badge variant="secondary">no key needed</Badge>
                    ) : provider.key_configured ? (
                      <span className="font-mono text-xs">
                        ••••{provider.key_hint ?? ""}
                      </span>
                    ) : (
                      <Badge variant="outline">not set</Badge>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-3 text-xs text-muted-foreground">
            To add or rotate a key, set its environment variable and restart the
            API. There is deliberately no field for one here: TITAN keeps secret
            material out of its database entirely, so a key typed into this
            screen would have nowhere to go.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Models</CardTitle>
          <CardDescription>
            Test performs a real round-trip through the gateway and reports what
            actually served it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Alias</TableHead>
                <TableHead>Provider / model</TableHead>
                <TableHead className="text-right">Test</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {aliases.map((alias) => {
                const result = results[alias.alias];
                return (
                  <TableRow key={alias.alias}>
                    <TableCell className="font-mono text-xs">
                      {alias.alias}
                      {alias.alias === data.default_alias ? (
                        <Badge variant="secondary" className="ml-2">
                          default
                        </Badge>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {alias.provider} / {alias.model}
                      {result ? (
                        <span
                          className={
                            result.ok
                              ? "ml-2 text-foreground"
                              : "ml-2 text-destructive"
                          }
                        >
                          {result.ok ? "✔" : "✖"} {result.latency_ms} ms
                          {result.served_model &&
                          result.served_model !== alias.model
                            ? ` — served by ${result.served_provider}/${result.served_model}`
                            : ""}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-right">
                      {/* settings:write, not read: a test spends real money and
                          writes a row to the usage ledger. */}
                      {canWrite ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={test.isPending}
                          onClick={() => void runTest(alias.alias)}
                        >
                          Test
                        </Button>
                      ) : (
                        <span
                          className="text-xs text-muted-foreground"
                          title="Testing a provider spends real money, so it needs the owner or admin role."
                        >
                          admin only
                        </span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
