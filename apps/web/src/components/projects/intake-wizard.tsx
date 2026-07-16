"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { Bot, Sparkles } from "lucide-react";

import { unwrap } from "@titan/api-client";
import { isDomainFrame } from "@titan/ws-protocol";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/input";
import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import {
  useAnswerClarifications,
  useClarifications,
} from "@/lib/projects/hooks";
import { useWsClient } from "@/lib/ws/manager";
import { createRafBatcher } from "@/lib/ws/raf-batch";

type Phase = "brief" | "thinking" | "questions" | "done";

/** No tokens within this window → intake is probably disabled (see below). */
const SILENCE_HINT_MS = 45_000;

/**
 * The New Project intake wizard (Vol 4 §6.2, TITAN-FRT-023).
 *
 * How it maps onto what the backend really does:
 *  - `POST /projects` with a brief launches the CEO intake turn (behind
 *    `ceo_intake_enabled`). Its tokens arrive as `run.output.delta` — the ONLY
 *    channel that carries text, so that is what streams here token-by-token.
 *  - `clarification.updated` carries counts only, never question text
 *    (TITAN-OPS-016). It is a refetch ping: on it we GET the clarification batch
 *    and swap the streamed prose for the authoritative question cards, which the
 *    Phase-43 engine has already capped at 5 and demoted canonical topics from.
 *  - Answers go back through `POST /clarifications/answers`.
 *
 * Risk R-3 (slow local models): a progress narration and a skeleton roadmap
 * placeholder keep a multi-minute local turn from looking broken.
 *
 * HONEST LIMIT: the wizard cannot end on a rendered roadmap. The roadmap
 * artifact is derived from the H1->H4 planning handoffs, not from intake, so a
 * freshly-created project has none. We navigate to the detail Overview (no
 * reload), where the roadmap panel renders the moment the artifact exists.
 */
export function IntakeWizard() {
  const router = useRouter();
  const client = useWsClient();
  const qc = useQueryClient();

  const [phase, setPhase] = useState<Phase>("brief");
  const [brief, setBrief] = useState("");
  const [name, setName] = useState("");
  const [projectId, setProjectId] = useState<string | null>(null);
  const [stream, setStream] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [silent, setSilent] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const streamRef = useRef<HTMLDivElement>(null);

  const clarifications = useClarifications(projectId ?? "", Boolean(projectId));
  const answerMutation = useAnswerClarifications(projectId ?? "");
  const questions = clarifications.data?.questions ?? [];

  // Stream the CEO turn's tokens, and treat clarification.updated as the signal
  // to refetch the authoritative batch.
  useEffect(() => {
    if (!client || !projectId) {
      return;
    }
    // Subscribe HERE, not at create-time: the listener must be registered before
    // the subscribe, and deltas are published unbuffered (buffered=False) with
    // no backfill, so anything sent before we're listening is gone. Pairing them
    // in one effect also guarantees the unsubscribe — the client refcounts
    // topics, so a stray subscribe would pin them for the tab's whole life.
    const batcher = createRafBatcher<string>((chunks) =>
      setStream((prev) => prev + chunks.join("")),
    );
    const unsubscribe = client.onAnyFrame((frame) => {
      if (!isDomainFrame(frame) || frame.project_id !== projectId) {
        return;
      }
      if (frame.type === "run.output.delta") {
        const delta = (frame.payload as { delta?: unknown }).delta;
        if (typeof delta === "string") {
          batcher.push(delta);
        }
      } else if (frame.type === "clarification.updated") {
        void qc.invalidateQueries({
          queryKey: queryKeys.projectClarifications(projectId),
        });
        setPhase("questions");
      } else if (frame.type === "run.finished") {
        // The backend publishes run.finished with status "failed" when the turn
        // raised. Treating every finish as success would tell the user work is
        // progressing after it crashed.
        const status = (frame.payload as { status?: unknown }).status;
        if (status === "failed") {
          setFailed(true);
          setPhase("done");
        } else {
          setPhase((p) => (p === "thinking" ? "done" : p));
        }
      }
    });
    const topics = ["runs", "clarification", "tasks"];
    client.subscribe(projectId, topics);
    return () => {
      unsubscribe();
      client.unsubscribe(projectId, topics);
      batcher.dispose();
    };
  }, [client, projectId, qc]);

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight });
  }, [stream]);

  /**
   * The CEO intake is gated by `ceo_intake_enabled`, which defaults to FALSE —
   * then `POST /projects` launches nothing at all and no frame ever arrives.
   * Without this, the UI would sit on "Reading your brief…" forever for a model
   * that was never invoked. If nothing has streamed after a grace period, say so
   * plainly rather than implying work is happening.
   */
  useEffect(() => {
    if (phase !== "thinking" || stream.length > 0) {
      setSilent(false);
      return;
    }
    const timer = setTimeout(() => setSilent(true), SILENCE_HINT_MS);
    return () => clearTimeout(timer);
  }, [phase, stream]);

  const submitBrief = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setPhase("thinking");
    try {
      const slug =
        (name || brief)
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "")
          .slice(0, 40) || "project";
      const created = unwrap(
        await browserApi().POST("/api/v1/projects", {
          body: {
            // `name` must match ^[a-z0-9][a-z0-9-]*$ and be unique — G1 rejects a
            // duplicate brief, so the suffix keeps repeat runs distinct.
            name: `${slug}-${Math.random().toString(36).slice(2, 7)}`,
            display_name: name || "New project",
            brief,
            llm_profile: "default",
            mode: "autonomous",
          },
        }),
      );
      // The effect above subscribes once this lands, so the listener is always
      // registered before the subscribe.
      setProjectId(created.id);
    } catch (err) {
      setPhase("brief");
      setError(
        err instanceof Error ? err.message : "Couldn’t create the project",
      );
    }
  };

  const submitAnswers = () => {
    const payload = questions
      .filter((q) => (answers[q.id] ?? "").trim().length > 0)
      .map((q) => ({ question_id: q.id, answer: answers[q.id].trim() }))
      // AnswerSubmission caps at 5 (the engine's ≤5 policy) — a 6th would 422.
      // The engine never issues more than 5, so this is a belt-and-braces clamp.
      .slice(0, 5);
    if (payload.length === 0 || !projectId) {
      return;
    }
    answerMutation.mutate(payload, {
      onSuccess: () => {
        setPhase("done");
        // No reload: client-side navigation to the detail Overview tab.
        router.push(`/projects/${projectId}`);
      },
      onError: (err) =>
        setError(
          err instanceof Error ? err.message : "Couldn’t send your answers",
        ),
    });
  };

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <header>
        <h1 className="text-xl font-semibold">New project</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Describe what you want built. The CEO agent asks only what it
          genuinely needs (at most five questions) and assumes the rest.
        </p>
      </header>

      {phase === "brief" ? (
        <form onSubmit={submitBrief} className="space-y-3">
          <div>
            <label htmlFor="pname" className="text-sm font-medium">
              Project name
            </label>
            <input
              id="pname"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Task Tracker"
              className="mt-1 h-9 w-full rounded-md border bg-background px-3 text-sm"
            />
          </div>
          <div>
            <label htmlFor="brief" className="text-sm font-medium">
              Brief
            </label>
            <Textarea
              id="brief"
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              required
              rows={5}
              className="mt-1"
              placeholder="Build a small task-tracking web app where a user can create tasks, mark them done, and filter by status."
            />
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <Button type="submit" disabled={!brief.trim()}>
            <Sparkles aria-hidden />
            Start
          </Button>
        </form>
      ) : null}

      {phase !== "brief" ? (
        <section className="rounded-lg border bg-card">
          <header className="flex items-center gap-2 border-b px-4 py-2">
            <Bot aria-hidden className="size-4 text-primary" />
            <span className="text-sm font-semibold">CEO agent</span>
            {phase === "thinking" && !silent ? (
              <span className="ml-auto animate-pulse text-xs text-muted-foreground">
                Reading your brief… local models can take a minute.
              </span>
            ) : null}
            {failed ? (
              <span className="ml-auto text-xs text-destructive">
                The CEO turn failed
              </span>
            ) : null}
          </header>
          <div
            ref={streamRef}
            className="max-h-64 overflow-y-auto px-4 py-3 font-mono text-xs whitespace-pre-wrap"
          >
            {stream ||
              (silent ? (
                <span className="text-warning">
                  No output yet. The CEO intake agent is disabled by default
                  (TITAN_CEO_INTAKE_ENABLED); the project was still created, so
                  you can open it and plan manually.
                </span>
              ) : (
                <span className="text-muted-foreground">
                  Waiting for the first tokens…
                </span>
              ))}
          </div>
        </section>
      ) : null}

      {phase === "questions" ? (
        clarifications.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : questions.length > 0 ? (
          <section className="space-y-3 rounded-lg border bg-card p-4">
            <h2 className="text-sm font-semibold">
              A few questions ({questions.length})
            </h2>
            {questions.map((q) => (
              <div key={q.id}>
                <label htmlFor={q.id} className="text-sm">
                  <span className="text-muted-foreground">{q.topic}: </span>
                  {q.question}
                </label>
                <Textarea
                  id={q.id}
                  rows={2}
                  className="mt-1"
                  value={answers[q.id] ?? ""}
                  onChange={(e) =>
                    setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))
                  }
                />
              </div>
            ))}
            <Button onClick={submitAnswers} disabled={answerMutation.isPending}>
              {answerMutation.isPending ? "Sending…" : "Send answers"}
            </Button>
            {error ? <p className="text-sm text-destructive">{error}</p> : null}
          </section>
        ) : clarifications.isError ? (
          // We only reach `questions` because a clarification.updated frame
          // proved a batch EXISTS. If the fetch then fails we must not claim
          // there were no questions — that would be a false all-clear over a
          // batch that is blocking the project.
          <p className="rounded-lg border border-dashed p-4 text-sm text-destructive">
            The CEO agent asked something, but the questions couldn’t be loaded.{" "}
            <button
              type="button"
              className="underline underline-offset-4"
              onClick={() => void clarifications.refetch()}
            >
              Retry
            </button>
          </p>
        ) : (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            The CEO agent had no essential questions — everything was inferable,
            so it recorded assumptions instead.
          </p>
        )
      ) : null}

      {/* R-3: a skeleton roadmap placeholder so a slow local model doesn't read
          as broken. The real roadmap needs the planning handoffs, so it renders
          on the detail Overview once the artifact exists. */}
      {phase === "thinking" || phase === "done" ? (
        <section className="rounded-lg border border-dashed p-4">
          <p className="text-xs font-medium text-muted-foreground">
            Roadmap (generating)
          </p>
          <div className="mt-2 space-y-2">
            <Skeleton className="h-3 w-2/3" />
            <Skeleton className="h-3 w-1/2" />
            <Skeleton className="h-3 w-5/6" />
          </div>
          {projectId ? (
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => router.push(`/projects/${projectId}`)}
            >
              Open project
            </Button>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
