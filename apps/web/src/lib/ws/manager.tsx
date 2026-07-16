"use client";

import { createContext, useContext, useEffect, useMemo } from "react";
import type { QueryClient, QueryKey } from "@tanstack/react-query";

import { isDomainFrame, type ServerFrame } from "@titan/ws-protocol";
import type { NotificationOut } from "@titan/api-client";

import { getQueryClient } from "@/lib/query/client";
import { queryKeys } from "@/lib/query/keys";
import { emitNotification } from "@/lib/notifications/bus";
import { fetchWsTicket } from "@/lib/bff/client";
import { useConnectionStore } from "./connection-store";
import { queryKeysForFrame, queryKeysForProject } from "./reconcile";
import { WsClient, type SocketLike } from "./socket";
import { wsUrl } from "./ticket";

const WsContext = createContext<WsClient | null>(null);

/** Adapt the browser WebSocket to the injectable `SocketLike` the client drives. */
function browserSocketFactory(url: string): SocketLike {
  const ws = new WebSocket(url);
  const like: SocketLike = {
    send: (data) => ws.send(data),
    close: (code, reason) => ws.close(code, reason),
    onopen: null,
    onclose: null,
    onerror: null,
    onmessage: null,
  };
  ws.onopen = () => like.onopen?.();
  ws.onclose = () => like.onclose?.();
  ws.onerror = () => like.onerror?.();
  ws.onmessage = (event) => like.onmessage?.({ data: event.data });
  return like;
}

// One socket per tab (TITAN-FRT-018): a module singleton reused across StrictMode
// remounts and route changes, connected for the tab's lifetime.
let singleton: WsClient | null = null;

function getWsClient(qc: QueryClient): WsClient {
  if (singleton) {
    return singleton;
  }
  const invalidate = (keys: QueryKey[]): void => {
    for (const queryKey of keys) {
      void qc.invalidateQueries({ queryKey });
    }
  };

  singleton = new WsClient({
    wsUrl,
    getTicket: fetchWsTicket,
    socketFactory: browserSocketFactory,
    onFrame: (frame: ServerFrame) => {
      if (!isDomainFrame(frame)) {
        return;
      }
      invalidate(queryKeysForFrame(frame));
      if (frame.type === "notification.created") {
        emitNotification(frame.payload as NotificationOut);
      }
    },
    onReconcile: (event) => {
      if (event.type === "reconnect") {
        for (const pid of event.projectIds) {
          invalidate(queryKeysForProject(pid));
        }
        // The project-less channels (notifications, agents, org activity) have
        // no server-side replay buffer, so anything published while we were
        // disconnected is simply gone — refetch them wholesale.
        invalidate([
          queryKeys.notifications(),
          queryKeys.notificationsUnread(),
          queryKeys.agents(),
          queryKeys.orgActivity(),
        ]);
      } else if (event.type === "gap") {
        invalidate(queryKeysForProject(event.projectId));
      } else if (event.projectId) {
        invalidate(queryKeysForProject(event.projectId));
      } else {
        invalidate([
          queryKeys.notifications(),
          queryKeys.notificationsUnread(),
        ]);
      }
    },
    onStatus: (status) => useConnectionStore.getState().setStatus(status),
  });
  return singleton;
}

/**
 * Instantiates the per-tab socket and connects it. `connect()` is idempotent, so
 * a StrictMode double-mount is safe. The socket lives for the tab's lifetime
 * (the browser closes it on unload), so there is no teardown on unmount.
 */
export function WsProvider({ children }: { children: React.ReactNode }) {
  const client = useMemo(
    () =>
      typeof window === "undefined" ? null : getWsClient(getQueryClient()),
    [],
  );
  useEffect(() => {
    client?.connect();
  }, [client]);
  return <WsContext.Provider value={client}>{children}</WsContext.Provider>;
}

export function useWsClient(): WsClient | null {
  return useContext(WsContext);
}
