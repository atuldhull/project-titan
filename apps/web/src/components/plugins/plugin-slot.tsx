"use client";

import { useInstalledPlugins } from "@/lib/plugins/hooks";
import {
  PANEL_SANDBOX,
  panelsForSlot,
  type MountedPanel,
  type SlotName,
} from "@/lib/plugins/slots";

/**
 * A named dashboard extension slot (TITAN-INT-011).
 *
 * Drop `<PluginSlot name="project.sidebar" />` anywhere and every enabled
 * plugin that declares a panel at that mount renders there — no core change per
 * plugin, which is the whole requirement.
 *
 * **The iframe is the trust boundary.** A panel is plugin-authored HTML served
 * from the plugin's own directory, so it is treated as untrusted third-party
 * code running inside an authenticated dashboard:
 *
 * * `sandbox="allow-scripts …"` WITHOUT `allow-same-origin` gives the frame a
 *   unique opaque origin. Its script cannot touch this document, its cookies,
 *   or localStorage — where the session lives. Adding `allow-same-origin`
 *   alongside `allow-scripts` would void the sandbox entirely.
 * * The `src` is rewritten by `panelUrl`, which accepts ONLY a same-origin
 *   `/plugin-ui/<the declaring plugin>/…` path. A manifest cannot frame another
 *   plugin's assets or an external origin.
 * * There is no server CSP to lean on: the BFF proxy forwards a fixed
 *   seven-header allowlist that does not include `content-security-policy`.
 *   The sandbox attribute IS the containment, not a second line of defence.
 *
 * Renders nothing at all when no plugin mounts here — a slot must not leave an
 * empty heading or a border behind on the many pages where it is unused.
 */
export function PluginSlot({
  name,
  className,
}: {
  readonly name: SlotName;
  readonly className?: string;
}) {
  const installs = useInstalledPlugins();
  // A 503 (runtime disabled) or a 403 is the normal state of a default stack.
  // A slot is decoration: it fails silently rather than pushing an error into
  // a screen that has nothing to do with plugins.
  const panels = panelsForSlot(installs.data ?? [], name);
  if (panels.length === 0) return null;

  return (
    <section className={className} aria-label="Plugin panels">
      {panels.map((panel) => (
        <PluginPanel key={`${panel.pluginId}:${panel.id}`} panel={panel} />
      ))}
    </section>
  );
}

function PluginPanel({ panel }: { readonly panel: MountedPanel }) {
  return (
    <div className="rounded-lg border bg-card">
      <header className="flex items-center justify-between gap-2 border-b px-3 py-2">
        <h3 className="text-sm font-medium">{panel.title}</h3>
        <span className="text-[10px] text-muted-foreground">
          {panel.pluginName}
        </span>
      </header>
      {panel.url ? (
        <iframe
          src={panel.url}
          title={`${panel.title} (${panel.pluginName})`}
          sandbox={PANEL_SANDBOX}
          // No `referrerPolicy` inheritance and no allow-list of features: the
          // panel gets a document and nothing else.
          referrerPolicy="no-referrer"
          loading="lazy"
          className="h-64 w-full border-0"
        />
      ) : (
        <p className="p-3 text-xs text-muted-foreground">
          This panel declares a source TITAN will not load. A panel may only
          serve assets from its own plugin, over this origin.
        </p>
      )}
    </div>
  );
}
