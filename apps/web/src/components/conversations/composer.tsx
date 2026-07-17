"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { usePostMessage } from "@/lib/conversations/hooks";
import { useCan } from "@/lib/auth/use-can";

/**
 * The human input box (§6.6).
 *
 * Posts kind `human`. §6.6 says "posts as role `client`" — there is no such
 * role, and the sender is never client input anyway (the API derives it from
 * the principal), so the human-ness rides on the message KIND, which is a real
 * value in the backend's Literal.
 *
 * `Ctrl/⌘+Enter` sends, per §10 — which also says shortcuts are disabled inside
 * text inputs EXCEPT `Esc` and `⌘Enter`, so this handler is scoped to the
 * textarea rather than the window.
 */
export function Composer({
  projectId,
  onSent,
}: {
  projectId: string | null;
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const post = usePostMessage(projectId);
  // `message:write` — admin, maintainer and operator hold it; viewer does not.
  const canWrite = useCan("message:write");

  if (!canWrite) {
    return (
      <p className="border-t p-2 text-xs text-muted-foreground">
        Your role can read this conversation but not post to it.
      </p>
    );
  }

  const send = (): void => {
    const body = text.trim();
    if (body === "" || projectId === null || post.isPending) {
      return;
    }
    post.mutate(body, {
      onSuccess: () => {
        setText("");
        onSent();
      },
    });
  };

  return (
    <div className="space-y-1 border-t p-2">
      {/* A send that fails must say so. The global mutation handler only toasts
          401/403 — a 500, or the BFF proxy's synthetic 502 when the API is
          unreachable, passes silently, and the composer would just re-enable
          the button with the text still sitting there. */}
      {post.isError ? (
        <p role="alert" className="text-xs text-destructive">
          Couldn’t send that message. It’s still here — try again.
        </p>
      ) : null}
      <div className="flex items-end gap-2">
        <label className="sr-only" htmlFor="composer">
          Message as client
        </label>
        <Textarea
          id="composer"
          rows={2}
          value={text}
          placeholder="Message as client…  (Ctrl/⌘+Enter to send)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              send();
            }
          }}
          className="min-h-0 flex-1 resize-none text-sm"
        />
        <Button
          size="sm"
          onClick={send}
          disabled={text.trim() === "" || post.isPending}
        >
          Send
        </Button>
      </div>
    </div>
  );
}
