"use client";

/**
 * The Architecture Viewer (Vol 4 §6.8, TITAN-FRT-031).
 *
 * What is real here and what §6.8 asked for that is not — the full reasoning is
 * in `lib/graph/architecture.ts`; the short version:
 *
 * - **Real:** the components + their dependencies (the H5 `ArchitecturePackage`),
 *   the version timeline (every Architect resubmission is a preserved row), the
 *   diff between any two versions, and the ADR records.
 * - **Not real:** C4 node types and the Context→Container→Component switcher.
 *   The package is single-level and its vocabulary is Clean Architecture layers.
 *   The switcher is a LAYER filter — the same affordance over the vocabulary that
 *   exists. Also no `architecture.updated` event: it appears in no code, and the
 *   fan-out silently drops unknown types. The banner is driven by
 *   `handoff.evaluated`, which really does fire when G5 judges this stage.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { GitCompare } from "lucide-react";

import { CanvasSkeleton } from "@/components/graph/canvas";
import { GraphShell } from "@/components/graph/graph-shell";
import { AdrPanel } from "@/components/graph/adr-panel";
import { ArchitectureEmpty, BoardError } from "@/components/graph/graph-states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  ARCHITECTURE_STAGE,
  useArtifactVersion,
  useArtifactVersions,
} from "@/lib/graph/hooks";
import {
  LAYERS,
  architectureModel,
  diffModel,
  layerGroup,
  parsePackage,
} from "@/lib/graph/architecture";
import { useTopic } from "@/lib/ws/use-topic";
import { useWsClient } from "@/lib/ws/manager";

export function ArchitectureScreen({ projectId }: { projectId: string }) {
  const versions = useArtifactVersions(projectId, ARCHITECTURE_STAGE);
  const [viewing, setViewing] = useState<number | null>(null);
  const [compareTo, setCompareTo] = useState<number | null>(null);
  const [layer, setLayer] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [newer, setNewer] = useState<number | null>(null);

  const all = useMemo(() => versions.data?.versions ?? [], [versions.data]);
  const latest = all[0]?.version ?? null;

  // PIN the viewed version as soon as one is known. `viewing ?? latest` looks
  // equivalent and does the OPPOSITE of what §6.8 requires: with `viewing` still
  // null, a refetch that discovers v8 moves `latest` 7 -> 8, so `shown` follows
  // it — the canvas silently swaps under the reader, and the "New version
  // available" banner can never fire because `latest > shown` is never true.
  // Pinning makes the banner the only way the view advances, which is the whole
  // point of the rule.
  useEffect(() => {
    if (viewing === null && latest !== null) {
      setViewing(latest);
    }
  }, [viewing, latest]);
  const shown = viewing;

  const current = useArtifactVersion(projectId, ARCHITECTURE_STAGE, shown);
  const previous = useArtifactVersion(projectId, ARCHITECTURE_STAGE, compareTo);

  useTopic("projectHandoffs", projectId);
  const client = useWsClient();

  // §6.8: a new version shows a BANNER, it never mutates the canvas under the
  // reader. There is no `architecture.updated` event — `handoff.evaluated` is the
  // real signal that G5 judged this stage, so refetch the version list on it and
  // let the banner appear if the list grew.
  //
  // `refetch` goes through a ref: a react-query result is a NEW object every
  // render, so depending on `versions` would tear down and re-register this
  // socket listener on every single render.
  const refetchVersions = useRef(versions.refetch);
  refetchVersions.current = versions.refetch;
  useEffect(() => {
    if (!client) {
      return;
    }
    return client.onAnyFrame((frame) => {
      if (
        frame.type === "handoff.evaluated" &&
        frame.project_id === projectId
      ) {
        void refetchVersions.current();
      }
    });
  }, [client, projectId]);

  useEffect(() => {
    if (latest !== null && shown !== null && latest > shown) {
      setNewer(latest);
    } else {
      setNewer(null);
    }
  }, [latest, shown]);

  // --- deep link: ?node= ----------------------------------------------------
  useEffect(() => {
    const read = (): void =>
      setSelectedId(new URLSearchParams(window.location.search).get("node"));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  const select = useCallback((id: string | null): void => {
    const url = new URL(window.location.href);
    if (id === null) {
      url.searchParams.delete("node");
    } else {
      url.searchParams.set("node", id);
    }
    window.history.replaceState({}, "", url);
    setSelectedId(id);
  }, []);

  const currentPkg = useMemo(
    () => parsePackage(current.data?.body),
    [current.data],
  );
  const previousPkg = useMemo(
    () => parsePackage(previous.data?.body),
    [previous.data],
  );
  const diffing = compareTo !== null && previous.data !== undefined;

  const model = useMemo(() => {
    const full = diffing
      ? diffModel(previousPkg, currentPkg)
      : architectureModel(currentPkg);
    if (layer === null) {
      return full;
    }
    // The "level switcher", honestly: a filter over the layers that exist.
    const kept = new Set(
      full.nodes.filter((n) => n.data.layer === layer).map((n) => n.id),
    );
    return {
      nodes: full.nodes.filter((n) => kept.has(n.id)),
      edges: full.edges.filter((e) => kept.has(e.from) && kept.has(e.to)),
    };
  }, [diffing, previousPkg, currentPkg, layer]);

  if (versions.isLoading) {
    return <CanvasSkeleton />;
  }
  if (versions.isError) {
    return <BoardError onRetry={() => void versions.refetch()} />;
  }
  if (all.length === 0) {
    return <ArchitectureEmpty projectId={projectId} />;
  }
  if (current.isLoading) {
    return <CanvasSkeleton />;
  }

  const selected = selectedId
    ? model.nodes.find((n) => n.id === selectedId)
    : undefined;

  return (
    <div className="space-y-2">
      {newer !== null ? (
        <div
          role="status"
          className="flex items-center gap-2 rounded-lg border border-primary bg-accent p-2 text-sm"
        >
          New version v{newer} available
          <Button size="sm" variant="outline" onClick={() => setViewing(newer)}>
            View
          </Button>
        </div>
      ) : null}

      <div className="grid gap-3 lg:grid-cols-[1fr_320px]">
        <GraphShell
          model={model}
          selectedId={selectedId}
          onSelect={(id) => select(id)}
          groupOf={layerGroup}
          exportName={`architecture-v${shown ?? 0}`}
          emptyLabel={
            layer
              ? `No components in the ${layer.replace(/_/g, " ")} layer.`
              : "This version has no components."
          }
          toolbar={
            <>
              <label
                className="text-xs text-muted-foreground"
                htmlFor="arch-version"
              >
                Version
              </label>
              <select
                id="arch-version"
                className="rounded-md border bg-background px-2 py-1 text-xs"
                value={shown ?? ""}
                onChange={(e) => setViewing(Number(e.target.value))}
              >
                {all.map((v) => (
                  <option key={v.version} value={v.version}>
                    v{v.version}
                    {v.version === latest ? " (current)" : ""} ·{" "}
                    {new Date(v.created_at).toLocaleDateString()}
                  </option>
                ))}
              </select>

              {/* §6.8 wants a Context→Container→Component switcher. The package
                  is single-level; this filters the layers it really has. */}
              <label
                className="text-xs text-muted-foreground"
                htmlFor="arch-layer"
              >
                Layer
              </label>
              <select
                id="arch-layer"
                className="rounded-md border bg-background px-2 py-1 text-xs"
                value={layer ?? ""}
                onChange={(e) => setLayer(e.target.value || null)}
              >
                <option value="">All layers</option>
                {LAYERS.map((l) => (
                  <option key={l} value={l}>
                    {l.replace(/_/g, " ")}
                  </option>
                ))}
              </select>

              <label
                className="text-xs text-muted-foreground"
                htmlFor="arch-diff"
              >
                <GitCompare aria-hidden className="mr-1 inline size-3" />
                Diff vs
              </label>
              <select
                id="arch-diff"
                className="rounded-md border bg-background px-2 py-1 text-xs"
                value={compareTo ?? ""}
                onChange={(e) =>
                  setCompareTo(e.target.value ? Number(e.target.value) : null)
                }
              >
                <option value="">Off</option>
                {all
                  .filter((v) => v.version !== shown)
                  .map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version}
                    </option>
                  ))}
              </select>
            </>
          }
          inspector={
            selected ? (
              <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-2 text-sm">
                <span className="font-medium">{selected.label}</span>
                <Badge variant="secondary">
                  {selected.data.layer.replace(/_/g, " ") || "unclassified"}
                </Badge>
                {selected.data.diff ? (
                  <Badge variant="outline">{selected.data.diff}</Badge>
                ) : null}
                {/* §6.8's wireframe shows "tech: FastAPI · owns: auth, catalog".
                    A Component is {name, layer, depends_on} — there is no tech
                    and no ownership on it, so neither is shown. */}
              </div>
            ) : null
          }
        />

        <AdrPanel projectId={projectId} packageAdrs={currentPkg?.adrs ?? []} />
      </div>
    </div>
  );
}
