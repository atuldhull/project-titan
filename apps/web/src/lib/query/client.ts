import {
  MutationCache,
  QueryClient,
  defaultShouldDehydrateQuery,
  isServer,
} from "@tanstack/react-query";

/**
 * React Query client factory (Vol 4 §4.1, ADR-FRT-01).
 *
 * `staleTime` > 0 keeps RSC-hydrated data from immediately refetching on the
 * client (no flash — TITAN-FRT-017). Invalidation is driven by the WebSocket
 * confirmation event, not polling; `refetchOnWindowFocus` is off because the
 * socket already keeps caches fresh. The 30 s `refetchInterval` safety net is
 * applied per-screen by the WS degradation layer, not globally.
 */
/**
 * A mutation cache that routes every mutation error through the global handler
 * (permission toast on 403, re-login on 401). Browser-only — loaded lazily so
 * sonner never enters the server bundle.
 */
function browserMutationCache(): MutationCache {
  return new MutationCache({
    onError: (error) => {
      void import("./errors").then((m) => m.handleMutationError(error));
    },
  });
}

export function makeQueryClient(mutationCache?: MutationCache): QueryClient {
  return new QueryClient({
    mutationCache,
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: 1,
      },
      dehydrate: {
        // Dehydrate in-flight queries too, so a page that streams its RSC
        // prefetch hydrates the pending state instead of refetching.
        shouldDehydrateQuery: (query) =>
          defaultShouldDehydrateQuery(query) ||
          query.state.status === "pending",
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

/**
 * The request-scoped client on the server (a fresh instance per RSC render so
 * requests never share cache) and a stable singleton in the browser (so React
 * Query state survives navigations within a tab).
 */
export function getQueryClient(): QueryClient {
  if (isServer) {
    return makeQueryClient();
  }
  if (!browserQueryClient) {
    browserQueryClient = makeQueryClient(browserMutationCache());
  }
  return browserQueryClient;
}
