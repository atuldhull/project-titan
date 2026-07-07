"use client";

// THROWAWAY (Phase 25 walking skeleton). A minimal WebSocket hook that streams a
// project's live activity straight off the Phase-24 `/ws` channel. The production
// realtime data layer (reconnect, seq-gap refetch, React Query cache) lands with
// the dashboard build-out (M6) — this only proves the spine end to end.

import { useEffect, useRef, useState } from "react";

export type Story = {
  id: string;
  title: string;
  spec: string;
  status: string;
};

export type StreamStatus =
  "idle" | "connecting" | "streaming" | "done" | "error";

type Frame = {
  v: number;
  type: string;
  seq: number;
  ts: string;
  project_id: string | null;
  payload: Record<string, unknown>;
};

function wsUrl(token: string): string {
  const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return `${api.replace(/^http/, "ws")}/ws?token=${encodeURIComponent(token)}`;
}

/**
 * Connects (once both `pid` and `token` are present), subscribes to the project,
 * answers server heartbeats, and accumulates the streamed `run.output.delta`
 * tokens plus the `task.created` stories. Returns render state only.
 */
export function useProjectStream(pid: string | null, token: string | null) {
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [streamText, setStreamText] = useState("");
  const [stories, setStories] = useState<Story[]>([]);
  const [error, setError] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!pid || !token) {
      return;
    }
    setStatus("connecting");
    setStreamText("");
    setStories([]);
    setError(null);

    const socket = new WebSocket(wsUrl(token));
    socketRef.current = socket;

    socket.onopen = () => {
      socket.send(
        JSON.stringify({ type: "subscribe", project_id: pid, topics: [] }),
      );
    };
    socket.onmessage = (event) => {
      let frame: Frame;
      try {
        frame = JSON.parse(event.data as string) as Frame;
      } catch {
        return;
      }
      switch (frame.type) {
        case "ping":
          socket.send(JSON.stringify({ type: "pong" }));
          break;
        case "subscribed":
          setStatus("streaming");
          break;
        case "run.output.delta": {
          const delta = frame.payload.delta;
          if (typeof delta === "string") {
            setStreamText((text) => text + delta);
          }
          break;
        }
        case "run.finished":
          setStatus("done");
          break;
        case "task.created": {
          // task.created frames carry the task DTO directly as the payload.
          const task = frame.payload as unknown as Story;
          if (task.id) {
            setStories((current) =>
              current.some((s) => s.id === task.id)
                ? current
                : [...current, task],
            );
          }
          break;
        }
        case "error":
          setError(String(frame.payload.message ?? "subscription error"));
          break;
        default:
          break;
      }
    };
    socket.onerror = () => setError("connection error");
    socket.onclose = () =>
      setStatus((s) => (s === "streaming" || s === "connecting" ? "done" : s));

    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [pid, token]);

  return { status, streamText, stories, error };
}
