/**
 * `Textarea` already lived in `input.tsx`, and two screens import it from
 * there (`conversations/composer`, `projects/intake-wizard`). This module
 * exists so `@/components/ui/textarea` resolves — the conventional path, and
 * the one a schema-driven form reaches for — WITHOUT creating a second styled
 * component that would drift from the first the moment either is edited.
 */
export { Textarea } from "@/components/ui/input";
