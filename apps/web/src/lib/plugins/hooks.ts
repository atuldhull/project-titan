"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from "@tanstack/react-query";

import {
  ApiError,
  unwrap,
  type McpPreviewOut,
  type McpServerSpec,
  type PluginConfigOut,
  type PluginInstallOut,
  type PluginOut,
} from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

/**
 * Plugin Manager data layer (Vol 4 §6.12).
 *
 * **Polled, not pushed.** §6.12 specifies `plugin.status_changed` updating the
 * health chips live. That event does not exist: grepping the whole repo finds
 * it only in spec prose, there is no producer, and the WS fanout has no handler
 * for it. So health advances on an interval, and the interval is honest about
 * being one.
 *
 * **The runtime is off by default.** `TITAN_PLUGINS_ENABLED` defaults to false,
 * so most lifecycle routes answer 503 on a default stack. `retry: false` keeps
 * that from being retried into a multi-second hang before the screen can render
 * its "runtime disabled" state.
 */
const HEALTH_REFRESH_MS = 15_000;

export function useInstalledPlugins() {
  return useQuery({
    queryKey: queryKeys.pluginInstalls(),
    queryFn: async (): Promise<PluginInstallOut[]> =>
      unwrap(await browserApi().GET("/api/v1/plugins/installs")),
    refetchInterval: HEALTH_REFRESH_MS,
    retry: false,
  });
}

export function usePluginCatalogue() {
  return useQuery({
    queryKey: queryKeys.plugins(),
    queryFn: async (): Promise<PluginOut[]> =>
      unwrap(await browserApi().GET("/api/v1/plugins")),
    retry: false,
  });
}

/** Schema + redacted values + secret status for one plugin's config form. */
export function usePluginConfig(pluginId: string | null) {
  return useQuery({
    queryKey: queryKeys.pluginConfig(pluginId ?? ""),
    queryFn: async (): Promise<PluginConfigOut> =>
      unwrap(
        await browserApi().GET("/api/v1/plugins/{plugin_id}/config", {
          params: { path: { plugin_id: pluginId ?? "" } },
        }),
      ),
    // Driven by the KEY, never by an imperative refetch after a setState: the
    // state update is asynchronous, so refetching alongside it fetches the
    // PREVIOUS plugin's config (the exact bug Phase 63's search shipped with).
    enabled: Boolean(pluginId),
    retry: false,
  });
}

/** Whether the plugin runtime is switched on at all. */
export function isRuntimeDisabled(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

interface LifecycleVariables {
  readonly pluginId: string;
  readonly action: "install" | "enable" | "disable" | "upgrade";
}

export function usePluginLifecycle(): UseMutationResult<
  unknown,
  Error,
  LifecycleVariables
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ pluginId, action }: LifecycleVariables) =>
      unwrap(
        await browserApi().POST(`/api/v1/plugins/{plugin_id}/${action}`, {
          params: { path: { plugin_id: pluginId } },
        }),
      ),
    onSuccess: () => {
      // The `plugins` prefix covers the catalogue, the install list and every
      // per-plugin key beneath it — an install moves a row from one tab to the
      // other, so both must refetch.
      void client.invalidateQueries({ queryKey: queryKeys.plugins() });
    },
  });
}

export function useRemovePlugin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (pluginId: string) =>
      unwrap(
        await browserApi().DELETE("/api/v1/plugins/{plugin_id}", {
          params: { path: { plugin_id: pluginId } },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.plugins() });
    },
  });
}

export function useSavePluginConfig(pluginId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (config: Record<string, unknown>) =>
      unwrap(
        await browserApi().PUT("/api/v1/plugins/{plugin_id}/config", {
          params: { path: { plugin_id: pluginId } },
          body: { config },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: queryKeys.pluginConfig(pluginId),
      });
    },
  });
}

/** Connect to a candidate MCP server and list its tools, without installing. */
export function useMcpPreview() {
  return useMutation({
    mutationFn: async (spec: McpServerSpec): Promise<McpPreviewOut> =>
      unwrap(
        await browserApi().POST("/api/v1/plugins:preview-mcp", { body: spec }),
      ),
  });
}

export function useAddMcpServer() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (spec: McpServerSpec): Promise<PluginOut> =>
      unwrap(
        await browserApi().POST("/api/v1/plugins:add-mcp", { body: spec }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.plugins() });
    },
  });
}
