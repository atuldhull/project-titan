import { PROTOCOL_VERSION, type ServerFrame } from "@titan/ws-protocol";

import type { ConnectionStatus } from "@/lib/ws/connection-store";
import type { ReconcileEvent, WsClientOptions } from "@/lib/ws/socket";
import { WsClient, type SocketLike } from "@/lib/ws/socket";

/** A hand-driven socket: capture sent frames, fire lifecycle events on demand. */
export class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  closed = false;

  constructor(readonly sent: string[]) {}

  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
    this.onclose?.();
  }
  emitOpen(): void {
    this.onopen?.();
  }
  emitMessage(raw: string): void {
    this.onmessage?.({ data: raw });
  }
  emitClose(): void {
    this.onclose?.();
  }
}

/** Deterministic timer scheduler — advance the virtual clock in tests. */
export class FakeScheduler {
  now = 0;
  private seq = 1;
  private tasks = new Map<number, { at: number; cb: () => void }>();

  set(cb: () => void, ms: number): number {
    const id = this.seq++;
    this.tasks.set(id, { at: this.now + ms, cb });
    return id;
  }
  clear(id: number): void {
    this.tasks.delete(id);
  }
  advance(ms: number): void {
    const target = this.now + ms;
    for (;;) {
      const due = [...this.tasks.entries()]
        .filter(([, t]) => t.at <= target)
        .sort((a, b) => a[1].at - b[1].at);
      if (due.length === 0) {
        break;
      }
      const [id, task] = due[0];
      this.tasks.delete(id);
      this.now = task.at;
      task.cb();
    }
    this.now = target;
  }
}

export function frame(
  type: string,
  options: { seq?: number; projectId?: string | null; payload?: unknown } = {},
): string {
  const f: ServerFrame = {
    v: PROTOCOL_VERSION,
    type: type as ServerFrame["type"],
    seq: options.seq ?? 0,
    ts: "2026-07-16T00:00:00.000Z",
    project_id: options.projectId ?? null,
    payload: options.payload ?? {},
  };
  return JSON.stringify(f);
}

export const flush = (): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, 0));

export interface Harness {
  client: WsClient;
  sockets: FakeSocket[];
  socket(): FakeSocket;
  sent: string[];
  scheduler: FakeScheduler;
  frames: ServerFrame[];
  reconciles: ReconcileEvent[];
  statuses: ConnectionStatus[];
  ticketCount(): number;
}

export function makeHarness(overrides: Partial<WsClientOptions> = {}): Harness {
  const sent: string[] = [];
  const sockets: FakeSocket[] = [];
  const scheduler = new FakeScheduler();
  const frames: ServerFrame[] = [];
  const reconciles: ReconcileEvent[] = [];
  const statuses: ConnectionStatus[] = [];
  let tickets = 0;

  const client = new WsClient({
    wsUrl: (token) => `ws://test/ws?token=${token}`,
    getTicket: async () => {
      tickets += 1;
      return "ticket";
    },
    socketFactory: () => {
      const socket = new FakeSocket(sent);
      sockets.push(socket);
      return socket;
    },
    onFrame: (f) => frames.push(f),
    onReconcile: (e) => reconciles.push(e),
    onStatus: (s) => statuses.push(s),
    scheduler,
    now: () => scheduler.now,
    random: () => 0.5,
    timings: {
      livenessTimeoutMs: 1000,
      backoffBaseMs: 100,
      backoffMaxMs: 2000,
      degradedAfterMs: 600,
    },
    ...overrides,
  });

  return {
    client,
    sockets,
    socket: () => sockets[sockets.length - 1],
    sent,
    scheduler,
    frames,
    reconciles,
    statuses,
    ticketCount: () => tickets,
  };
}
