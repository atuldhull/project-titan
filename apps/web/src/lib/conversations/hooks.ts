"use client";

/**
 * Data for §6.6's Conversations.
 *
 * **A channel is a project.** §6.6 wants "per-project + org channels" with names
 * like `#standup` and `#backend`. There is no channel entity anywhere: zero hits
 * for one across all 31 migrations, no channels table, no channel_id column, no
 * channel route. Messages are scoped by exactly three things — `project_id` (a
 * real FK, and the only hard scope), `thread_id`, and an optional `task_id`.
 *
 * **Threads are real, and they are still not channels.** An earlier draft of
 * this file claimed every message mints its own thread. That is true only of the
 * REST/orchestrator path (`create_message` does `thread_id or uuid7()`, and
 * `_write_transition_message` passes neither). The AGENT BUS does the opposite:
 * `bus.send` sets `thread_id = message.thread_id or message.correlation_id`, so
 * a reply carrying the same correlation_id lands in the SAME thread — which is
 * exactly how `turn.py` reads an exchange back as one transcript. Agent
 * conversations really are threaded.
 *
 * What makes a thread unusable as a channel is not that it is a singleton — it
 * is that nothing can ENUMERATE threads. There is no thread list endpoint, no
 * threads table, no name; `thread_id` is only ever a filter you must already
 * know the value of. And there is no org-wide message endpoint either, so "org
 * channels" have nothing to enumerate from.
 *
 * That leaves the project as the only channel that can be LISTED — so that is
 * what a channel is here, and `/conversations/[channelId]` takes a project id.
 * (A thread filter would be a natural sub-view; it needs a way to discover
 * threads first.)
 */

import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";

import { unwrap } from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

const PAGE = 50;

/**
 * One channel's history, paged backwards on scroll-up.
 *
 * The API sorts newest-first, so "the next page" is OLDER — which is exactly
 * what a scroll-up wants. The list is flipped into reading order by
 * `orderOldestFirst`, not here.
 */
export function useChannelMessages(projectId: string | null) {
  return useInfiniteQuery({
    queryKey: projectId
      ? queryKeys.projectMessages(projectId)
      : ["project", "none", "messages"],
    enabled: projectId !== null,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/messages", {
          params: {
            path: { project_id: projectId as string },
            query: { limit: PAGE, cursor: pageParam ?? undefined },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/**
 * Post as a human.
 *
 * §6.6 says "posts as role `client`". There is no `client` role — roles are
 * owner/admin/maintainer/operator/viewer — and a message's sender is never
 * client input anyway: the API derives it from the authenticated principal
 * (`_sender`), so a human's row carries `sender_user_id` and no
 * `sender_agent_id`. The human-ness lives in the KIND, and `human` is a real
 * value in the backend's `MessageKind` Literal.
 *
 * `message:write` is held by admin, maintainer and operator — not viewer.
 */
export function usePostMessage(projectId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (text: string) =>
      unwrap(
        await browserApi().POST("/api/v1/projects/{project_id}/messages", {
          params: { path: { project_id: projectId as string } },
          body: { kind: "human", body: { text } },
        }),
      ),
    onSuccess: () => {
      // The message.created frame invalidates this too; doing it here as well
      // means the composer clears against fresh data even if the socket is down.
      if (projectId !== null) {
        void client.invalidateQueries({
          queryKey: queryKeys.projectMessages(projectId),
        });
      }
    },
  });
}
