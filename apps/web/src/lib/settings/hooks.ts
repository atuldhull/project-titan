"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  unwrap,
  type ApiKeyOut,
  type LlmSettingsOut,
  type MemberOut,
  type NotificationPreferences,
  type Problem,
  type ProfileOut,
  type ProviderTestResult,
  type SessionOut,
} from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

/**
 * Settings data layer (Vol 4 §6.13).
 *
 * Every sub-page loads independently — §6.13 asks for exactly that — so there
 * is no combined "settings" query. Each hook owns one endpoint and one key, and
 * a 403 on one page leaves the others working.
 *
 * Nothing here polls. `member.updated` and `budget.threshold` are named in
 * §6.13's realtime line and neither exists (no producer, no frame type), and
 * settings are changed by a human at human speed — an interval refetch would be
 * cost with no signal behind it.
 */

// --- profile + sessions ------------------------------------------------------

export function useProfile() {
  return useQuery({
    queryKey: queryKeys.profile(),
    queryFn: async (): Promise<ProfileOut> =>
      unwrap(await browserApi().GET("/api/v1/auth/profile")),
    retry: false,
  });
}

export function useUpdateProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (displayName: string): Promise<ProfileOut> =>
      unwrap(
        await browserApi().PATCH("/api/v1/auth/profile", {
          body: { display_name: displayName },
        }),
      ),
    onSuccess: (profile) => {
      client.setQueryData(queryKeys.profile(), profile);
    },
  });
}

export function useSessions() {
  return useQuery({
    queryKey: queryKeys.sessions(),
    queryFn: async (): Promise<SessionOut[]> =>
      unwrap(await browserApi().GET("/api/v1/auth/sessions")),
    retry: false,
  });
}

export function useRevokeSession() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (familyId: string): Promise<void> => {
      // NOT `unwrap`: this endpoint answers 204 with an empty body, so `data`
      // is undefined on SUCCESS and unwrap throws — every revoke reported
      // failure and the list never refreshed, while the session was in fact
      // revoked. 204 is the only status this route returns on success; any
      // other outcome is a real error carrying a problem+json body.
      const result = await browserApi().DELETE(
        "/api/v1/auth/sessions/{family_id}",
        { params: { path: { family_id: familyId } } },
      );
      if (!result.response.ok) {
        throw new ApiError(result.response.status, problemOf(result.error));
      }
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.sessions() });
    },
  });
}

/** The problem+json body of a failed request, when it has one. */
function problemOf(error: unknown): Problem | null {
  return typeof error === "object" && error !== null
    ? (error as Problem)
    : null;
}

// --- API keys ----------------------------------------------------------------

export function useApiKeys() {
  return useQuery({
    queryKey: queryKeys.apiKeys(),
    queryFn: async (): Promise<ApiKeyOut[]> =>
      unwrap(await browserApi().GET("/api/v1/auth/api-keys")),
    retry: false,
  });
}

export function useCreateApiKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (
      name: string,
    ): Promise<{ id: string; api_key: string }> =>
      unwrap(
        await browserApi().POST("/api/v1/auth/api-keys", { body: { name } }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.apiKeys() });
    },
  });
}

export function useRevokeApiKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (keyId: string): Promise<ApiKeyOut> =>
      unwrap(
        await browserApi().DELETE("/api/v1/auth/api-keys/{key_id}", {
          params: { path: { key_id: keyId } },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.apiKeys() });
    },
  });
}

// --- members -----------------------------------------------------------------

export function useMembers() {
  return useQuery({
    queryKey: queryKeys.members(),
    queryFn: async (): Promise<MemberOut[]> =>
      unwrap(await browserApi().GET("/api/v1/members")),
    retry: false,
  });
}

export function useRoles() {
  return useQuery({
    queryKey: queryKeys.roles(),
    // The roles the DB actually defines — §9.3's five, not §6.13's four. Read
    // rather than hard-coded so the picker cannot offer `developer`, which does
    // not exist and which assignment would reject.
    queryFn: async (): Promise<string[]> =>
      unwrap(await browserApi().GET("/api/v1/roles")),
    staleTime: Infinity,
    retry: false,
  });
}

export function useSetMemberRoles() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      userId: string;
      roles: string[];
    }): Promise<MemberOut> =>
      unwrap(
        await browserApi().PUT("/api/v1/members/{user_id}/roles", {
          params: { path: { user_id: vars.userId } },
          body: { roles: vars.roles, project_id: null },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.members() });
    },
  });
}

// --- LLM providers + budgets -------------------------------------------------

export function useLlmSettings() {
  return useQuery({
    queryKey: queryKeys.llmSettings(),
    queryFn: async (): Promise<LlmSettingsOut> =>
      unwrap(await browserApi().GET("/api/v1/settings/llm")),
    retry: false,
  });
}

export interface LlmSettingsPatch {
  readonly default_alias?: string;
  /** "" clears it back to the model-policy tier defaults. */
  readonly agent_profile?: string;
  readonly embeddings_default_alias?: string;
  readonly per_project_usd?: number;
  readonly per_task_usd?: number;
  readonly action_on_exceeded?: string;
}

export function useUpdateLlmSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (patch: LlmSettingsPatch): Promise<LlmSettingsOut> =>
      unwrap(await browserApi().PUT("/api/v1/settings/llm", { body: patch })),
    onSuccess: (settings) => {
      // The response IS the new effective config (the server re-reads the
      // merged document), so seeding it avoids a refetch that would show the
      // pre-write value if it raced the filesystem mtime granularity.
      client.setQueryData(queryKeys.llmSettings(), settings);
    },
  });
}

export function useClearLlmOverrides() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<LlmSettingsOut> =>
      unwrap(await browserApi().DELETE("/api/v1/settings/llm")),
    onSuccess: (settings) => {
      client.setQueryData(queryKeys.llmSettings(), settings);
    },
  });
}

export function useTestProvider() {
  return useMutation({
    mutationFn: async (alias: string): Promise<ProviderTestResult> =>
      unwrap(
        await browserApi().POST("/api/v1/settings/llm:test", {
          body: { alias },
        }),
      ),
  });
}

// --- notification preferences ------------------------------------------------

export function useNotificationPreferences() {
  return useQuery({
    queryKey: queryKeys.notificationPreferences(),
    queryFn: async (): Promise<NotificationPreferences> =>
      unwrap(await browserApi().GET("/api/v1/notification-preferences")),
    retry: false,
  });
}

export function useSaveNotificationPreferences() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (
      categories: readonly { category: string; channels: string[] }[],
    ): Promise<NotificationPreferences> =>
      unwrap(
        await browserApi().PUT("/api/v1/notification-preferences", {
          body: { categories: [...categories] },
        }),
      ),
    onSuccess: (preferences) => {
      client.setQueryData(queryKeys.notificationPreferences(), preferences);
    },
  });
}
