"use client";

import { useState } from "react";
import { toast } from "sonner";
import { z } from "zod";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useZodForm } from "@/lib/forms/use-zod-form";
import { useAddMcpServer, useMcpPreview } from "@/lib/plugins/hooks";
import type { PluginRow } from "@/lib/plugins/derive";
import { errorMessage } from "@/lib/settings/errors";
import type { McpPreviewOut } from "@titan/api-client";

/**
 * MCP Servers tab (Vol 4 §6.12: "add by URL/command, tool list preview, enable
 * per-agent").
 *
 * Two of those three are real here. **Per-agent enablement is not**: an MCP
 * server's tools are registered globally in the same Tool Registry the bus
 * dispatches, gated by the RBAC scopes the manifest declares — there is no
 * per-agent binding for a server anywhere in the runtime. Rather than render a
 * picker that would persist nowhere, the scopes field is surfaced (that IS the
 * access control) and the gap is stated.
 *
 * Preview connects for real: it performs the MCP handshake through the same
 * transport builder an enable would use, lists the tools, and disconnects. A
 * server reachable only by bypassing the sandbox or egress rules will fail here
 * exactly as it would fail later.
 */
const schema = z.object({
  plugin_id: z
    .string()
    .min(3, "At least 3 characters.")
    .max(200)
    .regex(
      /^[a-z0-9][a-z0-9._-]*$/,
      "Lowercase letters, digits and . _ - only — it becomes a directory name.",
    ),
  name: z.string().min(1, "A name is required.").max(200),
  transport: z.enum(["http", "stdio"]),
  url: z.string().max(2000).optional(),
  command: z.string().max(2000).optional(),
});

type FormShape = z.infer<typeof schema>;

export function McpServersTab({
  rows,
  canAdmin,
}: {
  readonly rows: readonly PluginRow[];
  readonly canAdmin: boolean;
}) {
  const preview = useMcpPreview();
  const add = useAddMcpServer();
  const [result, setResult] = useState<McpPreviewOut | null>(null);
  const form = useZodForm(schema, {
    defaultValues: {
      plugin_id: "",
      name: "",
      transport: "http",
      url: "",
      command: "",
    },
  });
  const transport = form.watch("transport");

  const toSpec = (values: FormShape) => ({
    plugin_id: values.plugin_id,
    name: values.name,
    transport: values.transport,
    url: values.transport === "http" ? (values.url ?? "") : null,
    command:
      values.transport === "stdio"
        ? (values.command ?? "").split(/\s+/).filter(Boolean)
        : [],
    // An http server is unreachable without an egress allowance for its own
    // origin, so it is derived rather than left as a trap the operator only
    // discovers at enable time.
    network:
      values.transport === "http" && values.url ? [originOf(values.url)] : [],
    scopes: [],
    auth_secret: null,
    description: "",
  });

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Connected servers</h2>
        {rows.length === 0 ? (
          <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
            No MCP servers yet. Add one below.
          </p>
        ) : (
          rows.map((row) => (
            <div
              key={row.pluginId}
              className="flex flex-wrap items-center gap-3 rounded-lg border bg-card p-3"
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{row.name}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {row.healthLabel}
                  {row.tools.length > 0
                    ? ` · ${row.tools.length} tools`
                    : " · no tools registered"}
                </p>
              </div>
              <Badge
                variant={row.state === "enabled" ? "default" : "secondary"}
              >
                {row.state}
              </Badge>
            </div>
          ))
        )}
      </section>

      {canAdmin ? (
        <section className="space-y-3 rounded-lg border bg-card p-4">
          <h2 className="text-sm font-medium">Add an MCP server</h2>
          <form
            className="space-y-3"
            onSubmit={form.handleSubmit(async (values) => {
              try {
                await add.mutateAsync(toSpec(values));
                toast.success(
                  `${values.plugin_id} added — install it to use it`,
                );
                form.reset();
                setResult(null);
              } catch (error) {
                toast.error(errorMessage(error));
              }
            })}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                id="mcp-id"
                label="Server id"
                error={form.formState.errors.plugin_id?.message}
              >
                <Input
                  id="mcp-id"
                  placeholder="acme.docs"
                  {...form.register("plugin_id")}
                />
              </Field>
              <Field
                id="mcp-name"
                label="Display name"
                error={form.formState.errors.name?.message}
              >
                <Input
                  id="mcp-name"
                  placeholder="Acme Docs"
                  {...form.register("name")}
                />
              </Field>
            </div>

            <Field id="mcp-transport" label="Transport">
              <Select
                value={transport}
                onValueChange={(value) =>
                  form.setValue("transport", value as "http" | "stdio", {
                    shouldDirty: true,
                  })
                }
              >
                <SelectTrigger id="mcp-transport" className="max-w-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="http">HTTP (Streamable)</SelectItem>
                  <SelectItem value="stdio">stdio (local command)</SelectItem>
                </SelectContent>
              </Select>
            </Field>

            {transport === "http" ? (
              <Field
                id="mcp-url"
                label="Endpoint URL"
                error={form.formState.errors.url?.message}
              >
                <Input
                  id="mcp-url"
                  placeholder="https://mcp.example.com/v1"
                  {...form.register("url")}
                />
              </Field>
            ) : (
              <Field
                id="mcp-command"
                label="Command"
                error={form.formState.errors.command?.message}
              >
                <Input
                  id="mcp-command"
                  placeholder="npx -y @modelcontextprotocol/server-filesystem ."
                  {...form.register("command")}
                />
              </Field>
            )}

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={preview.isPending}
                onClick={async () => {
                  const values = form.getValues();
                  const valid = await form.trigger();
                  if (!valid) return;
                  try {
                    const outcome = await preview.mutateAsync(toSpec(values));
                    setResult(outcome);
                    if (!outcome.ok) toast.error(outcome.detail);
                  } catch (error) {
                    toast.error(errorMessage(error));
                  }
                }}
              >
                {preview.isPending ? "Connecting…" : "Preview tools"}
              </Button>
              <Button type="submit" disabled={add.isPending}>
                {add.isPending ? "Adding…" : "Add server"}
              </Button>
            </div>
          </form>

          {result ? <PreviewResult result={result} /> : null}

          <p className="text-xs text-muted-foreground">
            Adding writes the server as an ordinary plugin, so it then installs
            and enables through the normal lifecycle. Its tools are gated by the
            RBAC scopes on its manifest — TITAN has no per-agent binding for an
            MCP server, so that is what controls who may call them.
          </p>
        </section>
      ) : (
        <p className="text-sm text-muted-foreground">
          Adding an MCP server needs the owner or admin role.
        </p>
      )}
    </div>
  );
}

function PreviewResult({ result }: { readonly result: McpPreviewOut }) {
  if (!result.ok) {
    return (
      <p role="status" className="text-xs text-destructive">
        ✖ Could not connect: {result.detail}
      </p>
    );
  }
  const tools = result.tools ?? [];
  return (
    <div className="space-y-1 rounded-md border p-3">
      <p className="text-xs">
        ✔ Connected · protocol{" "}
        <span className="font-mono">{result.protocol_version}</span> ·{" "}
        {tools.length} tool{tools.length === 1 ? "" : "s"}
      </p>
      <ul className="space-y-0.5">
        {tools.map((tool) => (
          <li key={tool.name} className="text-xs">
            <span className="font-mono">{tool.name}</span>
            {tool.description ? (
              <span className="text-muted-foreground">
                {" "}
                — {tool.description}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Field({
  id,
  label,
  error,
  children,
}: {
  readonly id: string;
  readonly label: string;
  readonly error?: string;
  readonly children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** The origin of a URL, for the egress allowance. Falls back to the raw value
 *  so a malformed URL is rejected by the server's validator rather than here,
 *  where the message would be worse. */
export function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return url;
  }
}
