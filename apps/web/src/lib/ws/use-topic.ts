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
