import Markdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";

/**
 * TITAN-SEC-072: the ONE sanctioned way to render agent/LLM-derived Markdown.
 *
 * All model output is untrusted. react-markdown 9 already refuses raw HTML
 * unless `rehype-raw` is added, and this component adds `rehype-sanitize` on top
 * as an explicit allowlist pipeline — so even if a `rehype-raw` regressed in, the
 * sanitiser strips `<script>`, event-handler attributes (`onerror`, …), and
 * `javascript:` URLs against the default (GitHub-like) schema. Callers pass a
 * plain string and optional element overrides; nothing here interprets HTML.
 *
 * Do not render agent Markdown with a bare `<Markdown>`/`dangerouslySetInnerHTML`
 * — route it through here so the sanitiser is never accidentally omitted.
 */
export function SafeMarkdown({
  children,
  components,
}: {
  children: string;
  components?: Components;
}) {
  return (
    <Markdown rehypePlugins={[rehypeSanitize]} components={components}>
      {children}
    </Markdown>
  );
}
