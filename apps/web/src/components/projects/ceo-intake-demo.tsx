"use client";

// THROWAWAY (Phase 25 walking skeleton). Submits a product brief, then streams
// the CEO agent's tokens and the resulting user stories live off the WebSocket —
// no page reload. The real conversational intake wizard (Vol 4 §6.2) replaces
// this in M6; here it exists only to prove the spine end to end in a browser.

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useProjectStream } from "@/hooks/use-project-stream";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function slugify(name: string): string {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "project"
  );
}

export function CeoIntakeDemo() {
  const [token, setToken] = useState("");
  const [name, setName] = useState("Task Tracker");
  const [brief, setBrief] = useState(
    "Build a small task-tracking web app where a user can create tasks, mark them done, and filter by status.",
  );
  const [pid, setPid] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const { status, streamText, stories, error } = useProjectStream(
    pid,
    token || null,
  );

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitError(null);
    setPid(null);
    setSubmitting(true);
    try {
      // Phase-25 throwaway demo (token-paste flow, replaced by the Phase-58
      // intake wizard on the Phase-57 data layer). Kept to prove the spine.
      // eslint-disable-next-line no-restricted-syntax
      const res = await fetch(`${API}/api/v1/projects`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          name: `${slugify(name)}-${Math.random().toString(36).slice(2, 7)}`,
          display_name: name,
          brief,
        }),
      });
      if (!res.ok) {
        throw new Error(`create failed: ${res.status}`);
      }
      const project = (await res.json()) as { id: string };
      setPid(project.id); // → the hook connects + subscribes
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "request failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div>
        <h1 className="text-xl font-semibold">New Project — CEO Intake</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Submit a brief; the CEO agent streams a plan and turns it into user
          stories live (Phase 25 walking skeleton).
        </p>
      </div>

      <form
        onSubmit={submit}
        className="space-y-3 rounded-lg border bg-card p-6"
      >
        <label className="block text-sm">
          <span className="text-muted-foreground">Access token (JWT)</span>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="paste an access token from POST /api/v1/auth/token"
            className="mt-1 w-full rounded-md border bg-background px-3 py-2 font-mono text-xs"
          />
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">Project name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm"
          />
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">Brief</span>
          <textarea
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            rows={3}
            className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-sm"
          />
        </label>
        <Button type="submit" disabled={submitting || !token || !brief}>
          {submitting ? "Creating…" : "Create & stream"}
        </Button>
        {submitError ? (
          <p className="text-xs text-destructive">{submitError}</p>
        ) : null}
      </form>

      {pid ? (
        <div className="space-y-4">
          <div className="rounded-lg border bg-card p-6">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-medium">CEO agent output</h2>
              <Badge variant={error ? "destructive" : "secondary"}>
                {error ?? status}
              </Badge>
            </div>
            <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-xs text-muted-foreground">
              {streamText || "Waiting for the model to stream…"}
            </pre>
          </div>

          <div className="rounded-lg border bg-card p-6">
            <h2 className="text-sm font-medium">
              User stories{" "}
              <span className="text-muted-foreground">({stories.length})</span>
            </h2>
            <ul className="mt-3 space-y-2">
              {stories.map((story) => (
                <li key={story.id} className="rounded-md border p-3">
                  <p className="text-sm font-medium">{story.title}</p>
                  {story.spec ? (
                    <pre className="mt-1 whitespace-pre-wrap font-mono text-xs text-muted-foreground">
                      {story.spec}
                    </pre>
                  ) : null}
                </li>
              ))}
              {stories.length === 0 ? (
                <li className="text-xs text-muted-foreground">
                  Stories appear here as the agent commits them — no reload.
                </li>
              ) : null}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
