"use client";

import { useEffect } from "react";

import { CORE_TOPICS, type CoreTopicKey } from "@titan/ws-protocol";

import { useWsClient } from "./manager";

/**
 * Subscribe to a §5.2 core topic for the lifetime of the mounting component
 * (TITAN-FRT-018). Project topics need a `projectId`; always-on topics
 * (notifications, agents) are auto-delivered and need no subscribe. Ref-counting
 * in the client means several screens sharing a topic yield one subscribe, and
 * the last unmount unsubscribes.
 */
export function useTopic(topicKey: CoreTopicKey, projectId?: string): void {
  const client = useWsClient();
  useEffect(() => {
    if (!client) {
      return;
    }
    const topic = CORE_TOPICS[topicKey];
    if (topic.scope === "project") {
      if (!projectId) {
        return;
      }
      client.subscribe(projectId, topic.categories);
      return () => client.unsubscribe(projectId, topic.categories);
    }
    if (topic.scope === "org") {
      client.subscribe(null, ["org:activity"]);
      return () => client.unsubscribe(null, ["org:activity"]);
    }
    // always-on — nothing to subscribe.
    return;
  }, [client, topicKey, projectId]);
}

/**
 * Subscribe to one project topic across SEVERAL projects at once.
 *
 * An org-level screen can still need per-project frames: the agent grid is
 * org-wide, but a run — the only record of what an agent is doing — is
 * project-scoped, so the grid must listen to every project its agents live in.
 * Hooks cannot be called in a loop, hence this rather than N × `useTopic`.
 *
 * The effect keys on the SORTED, JOINED ids rather than on the array itself: a
 * caller that rebuilds an equal array each render (the common case here, since
 * the ids are derived from query data) would otherwise unsubscribe and
 * re-subscribe on every render. The ids are re-derived from that string inside
 * the effect, so there is no stale closure over the caller's array either.
 */
export function useProjectTopics(
  topicKey: CoreTopicKey,
  projectIds: readonly string[],
): void {
  const client = useWsClient();
  const key = [...projectIds].sort().join(",");
  useEffect(() => {
    if (!client) {
      return;
    }
    const topic = CORE_TOPICS[topicKey];
    if (topic.scope !== "project") {
      return;
    }
    const ids = key === "" ? [] : key.split(",");
    for (const pid of ids) {
      client.subscribe(pid, topic.categories);
    }
    return () => {
      for (const pid of ids) {
        client.unsubscribe(pid, topic.categories);
      }
    };
  }, [client, topicKey, key]);
}
