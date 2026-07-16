import {
  HydrationBoundary,
  dehydrate,
  type QueryClient,
} from "@tanstack/react-query";

/**
 * Server-component wrapper that ships a dehydrated cache to the browser so warm
 * navigation renders from `initialData` with no refetch flash (TITAN-FRT-017).
 *
 * Usage in an RSC page:
 * ```tsx
 * const qc = getQueryClient();
 * await qc.prefetchQuery({ queryKey: queryKeys.projects(), queryFn: ... });
 * return <HydrateQuery client={qc}><ProjectsList /></HydrateQuery>;
 * ```
 */
export function HydrateQuery({
  client,
  children,
}: {
  client: QueryClient;
  children: React.ReactNode;
}) {
  return (
    <HydrationBoundary state={dehydrate(client)}>{children}</HydrationBoundary>
  );
}
