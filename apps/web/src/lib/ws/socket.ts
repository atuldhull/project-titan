import {
  encodeClientFrame,
  parseServerFrame,
  PONG_FRAME,
  subscribeFrame,
  unsubscribeFrame,
  type ServerFrame,
} from "@titan/ws-protocol";

import type { ConnectionStatus } from "./connection-store";

/**
 * The single multiplexed realtime client (Vol 4 §5, ADR-FRT-03, TITAN-FRT-018).
 *
 * One socket per tab. Route mounts drive ref-counted topic subscriptions;
 * per-project `seq` gaps and reconnects trigger a targeted refetch
 * (TITAN-FRT-019); the connection state is always observable for the top-bar
 * pill (TITAN-FRT-020). Everything it touches — the socket, timers, the ticket
 * fetch, randomness for jitter — is injected, so the whole state machine runs
 * deterministically under test with zero DOM.
 */

/** Minimal WebSocket surface the client drives (a real socket is adapted). */
export interface SocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
}

export interface TimerScheduler {
  set(callback: () => void, ms: number): number;
  clear(handle: number): void;
}

export type ReconcileEvent =
  | { type: "gap"; projectId: string }
  | { type: "resync"; projectId: string | null }
  | { type: "reconnect"; projectIds: string[] };

export interface WsTimings {
  /** No frame within this window → the socket is presumed dead (server pings
   *  every 25 s, so the default leaves margin). */
  livenessTimeoutMs: number;
  backoffBaseMs: number;
  backoffMaxMs: number;
  /** Continuous reconnecting past this → offline + polling fallback (§5.3). */
  degradedAfterMs: number;
}

export const DEFAULT_TIMINGS: WsTimings = {
  livenessTimeoutMs: 35_000,
  backoffBaseMs: 1_000,
  backoffMaxMs: 30_000,
  degradedAfterMs: 60_000,
};

export interface WsClientOptions {
  wsUrl: (token: string) => string;
  getTicket: () => Promise<string | null>;
  socketFactory: (url: string) => SocketLike;
  onFrame: (frame: ServerFrame) => void;
  onReconcile: (event: ReconcileEvent) => void;
  onStatus: (status: ConnectionStatus) => void;
  scheduler?: TimerScheduler;
  now?: () => number;
  random?: () => number;
  timings?: Partial<WsTimings>;
}

const globalScheduler: TimerScheduler = {
  set: (cb, ms) => setTimeout(cb, ms) as unknown as number,
  clear: (handle) => clearTimeout(handle),
};

function subKey(projectId: string | null, topic: string): string {
  return `${projectId ?? "*"}::${topic}`;
}

export class WsClient {
  private readonly options: WsClientOptions;
  private readonly scheduler: TimerScheduler;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly timings: WsTimings;

  private socket: SocketLike | null = null;
  private _status: ConnectionStatus = "connecting";
  private attempt = 0;
  private reconnectingSince: number | null = null;
  private everConnected = false;
  private destroyed = false;
  private connecting = false;
  /** True only between `onopen` and disconnect — a socket exists but is NOT
   *  writable while it is still CONNECTING (send() would throw). */
  private opened = false;

  private reconnectTimer: number | null = null;
  private livenessTimer: number | null = null;

  /** Desired subscriptions: subKey -> {projectId, topic, refCount}. */
  private readonly subs = new Map<
    string,
    { projectId: string | null; topic: string; refCount: number }
  >();
  /** Last applied seq per project (gap detection + reconnect replay cursor). */
  private readonly lastSeq = new Map<string, number>();

  constructor(options: WsClientOptions) {
    this.options = options;
    this.scheduler = options.scheduler ?? globalScheduler;
    this.now = options.now ?? (() => Date.now());
    this.random = options.random ?? (() => Math.random());
    this.timings = { ...DEFAULT_TIMINGS, ...options.timings };
  }

  get status(): ConnectionStatus {
    return this._status;
  }

  connect(): void {
    if (this.destroyed || this.connecting || this.socket) {
      return;
    }
    this.connecting = true;
    void this.openSocket();
  }

  private async openSocket(): Promise<void> {
    const token = await this.options.getTicket();
    if (this.destroyed) {
      return;
    }
    if (token === null) {
      // No ticket yet (session not ready / refresh failed) — back off and retry.
      this.connecting = false;
      this.scheduleReconnect();
      return;
    }
    let socket: SocketLike;
    try {
      socket = this.options.socketFactory(this.options.wsUrl(token));
    } catch {
      this.connecting = false;
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => this.handleOpen();
    socket.onclose = () => this.handleDisconnect();
    socket.onerror = () => this.handleDisconnect();
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        this.handleRaw(event.data);
      }
    };
  }

  private handleOpen(): void {
    this.connecting = false;
    this.opened = true;
    this.setStatus("live");
    this.resubscribeAll();
    this.armLiveness();
    if (this.everConnected) {
      // A reconnect: refetch everything subscribed so nothing is stale (FRT-019).
      const projectIds = this.subscribedProjectIds();
      this.options.onReconcile({ type: "reconnect", projectIds });
    }
    this.everConnected = true;
    // NOTE: backoff is deliberately NOT reset here. The server accepts the
    // socket BEFORE enforcing the connection cap and re-checking the token, so
    // it can accept-then-close(1008) immediately. Resetting on `onopen` would
    // pin the delay at its minimum forever (a ~1 Hz reconnect + refetch storm)
    // and keep `reconnectingSince` null so `offline` was never reached. The
    // reset happens on the first non-error frame instead — see markHealthy().
  }

  /** A frame arrived, so this connection is genuinely usable: reset the backoff
   *  and the degradation clock. */
  private markHealthy(): void {
    this.attempt = 0;
    this.reconnectingSince = null;
  }

  private handleDisconnect(): void {
    if (this.socket) {
      this.socket.onopen = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.onmessage = null;
    }
    this.socket = null;
    this.connecting = false;
    this.opened = false;
    this.clearLiveness();
    if (this.destroyed) {
      return;
    }
    if (this.reconnectingSince === null) {
      this.reconnectingSince = this.now();
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.destroyed || this.reconnectTimer !== null) {
      return;
    }
    if (this.reconnectingSince === null) {
      this.reconnectingSince = this.now();
    }
    const degraded =
      this.now() - this.reconnectingSince >= this.timings.degradedAfterMs;
    this.setStatus(degraded ? "offline" : "reconnecting");

    const cap = Math.min(
      this.timings.backoffMaxMs,
      this.timings.backoffBaseMs * 2 ** this.attempt,
    );
    // Equal jitter: half fixed, half random, within the cap (§5.3).
    const delay = cap / 2 + this.random() * (cap / 2);
    this.attempt += 1;
    this.reconnectTimer = this.scheduler.set(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /** Manual retry from the offline banner: reset backoff and connect now. */
  retry(): void {
    if (this.destroyed) {
      return;
    }
    if (this.reconnectTimer !== null) {
      this.scheduler.clear(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.attempt = 0;
    this.connect();
  }

  destroy(): void {
    this.destroyed = true;
    this.clearLiveness();
    if (this.reconnectTimer !== null) {
      this.scheduler.clear(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      this.socket.onopen = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.onmessage = null;
      try {
        this.socket.close();
      } catch {
        // ignore
      }
      this.socket = null;
    }
  }

  // --- subscriptions --------------------------------------------------------

  subscribe(projectId: string | null, topics: readonly string[]): void {
    for (const topic of topics) {
      const key = subKey(projectId, topic);
      const existing = this.subs.get(key);
      if (existing) {
        existing.refCount += 1;
      } else {
        this.subs.set(key, { projectId, topic, refCount: 1 });
        this.sendSubscribe(projectId, [topic]);
      }
    }
  }

  unsubscribe(projectId: string | null, topics: readonly string[]): void {
    for (const topic of topics) {
      const key = subKey(projectId, topic);
      const existing = this.subs.get(key);
      if (!existing) {
        continue;
      }
      existing.refCount -= 1;
      if (existing.refCount <= 0) {
        this.subs.delete(key);
        if (projectId !== null) {
          this.sendUnsubscribe(projectId, [topic]);
        } else {
          this.send(unsubscribeFramePlaceholder(topic));
        }
      }
    }
  }

  private sendSubscribe(projectId: string | null, topics: string[]): void {
    const lastSeq =
      projectId !== null ? this.lastSeq.get(projectId) : undefined;
    this.send(encodeClientFrame(subscribeFrame(projectId, topics, lastSeq)));
  }

  private sendUnsubscribe(projectId: string, topics: string[]): void {
    this.send(encodeClientFrame(unsubscribeFrame(projectId, topics)));
  }

  private resubscribeAll(): void {
    // Coalesce per project so one subscribe frame carries all its topics, each
    // replaying from the last applied seq.
    const byProject = new Map<string | null, string[]>();
    for (const { projectId, topic } of this.subs.values()) {
      const list = byProject.get(projectId) ?? [];
      list.push(topic);
      byProject.set(projectId, list);
    }
    for (const [projectId, topics] of byProject) {
      this.sendSubscribe(projectId, topics);
    }
  }

  private subscribedProjectIds(): string[] {
    const ids = new Set<string>();
    for (const { projectId } of this.subs.values()) {
      if (projectId !== null) {
        ids.add(projectId);
      }
    }
    return [...ids];
  }

  // --- inbound frames -------------------------------------------------------

  private handleRaw(raw: string): void {
    const frame = parseServerFrame(raw);
    if (!frame) {
      return;
    }
    this.armLiveness();
    if (frame.type !== "error") {
      // Any real frame proves the connection survived the server's post-accept
      // checks (connection cap / token). An `error` frame does not.
      this.markHealthy();
    }
    this.handleFrame(frame);
  }

  private handleFrame(frame: ServerFrame): void {
    switch (frame.type) {
      case "ping":
        this.send(encodeClientFrame(PONG_FRAME));
        return;
      case "pong":
      case "subscribed":
      case "unsubscribed":
      case "authenticated":
        return;
      case "error":
        this.handleErrorFrame(frame);
        return;
      case "sys.resync":
        this.options.onReconcile({
          type: "resync",
          projectId: frame.project_id,
        });
        if (frame.project_id !== null) {
          // The server sends sys.resync with seq 0, so it carries no usable
          // cursor — drop ours and re-baseline off the next live frame. Setting
          // the cursor to frame.seq would rewind it to 0 and make the very next
          // frame look like a gap, forcing a second redundant refetch.
          this.lastSeq.delete(frame.project_id);
        }
        return;
      default:
        this.handleDomainFrame(frame);
    }
  }

  private handleDomainFrame(frame: ServerFrame): void {
    const pid = frame.project_id;
    // Frames with no project (notifications / agent status / org activity) are
    // delivered directly. They DO carry a real per-channel seq (the backend
    // INCRs a per-user/agent/org counter), but those channels have no replay
    // buffer server-side, so a cursor would buy nothing: on reconnect the
    // reconcile refetches them wholesale instead. Dedupe for them is by entity
    // id downstream (the store and sonner both key by notification id).
    if (pid === null || frame.seq <= 0) {
      this.options.onFrame(frame);
      return;
    }
    const prev = this.lastSeq.get(pid);
    if (prev !== undefined && frame.seq <= prev) {
      if (frame.seq === 1 && prev > 1) {
        // The server-side counter restarted (its Redis seq key was lost), so it
        // is streaming from 1 again. Without this, every subsequent frame looks
        // like a duplicate and the project's feed would stay frozen forever.
        // Re-baseline and refetch to converge.
        this.lastSeq.set(pid, frame.seq);
        this.options.onFrame(frame);
        this.options.onReconcile({ type: "gap", projectId: pid });
        return;
      }
      // Duplicate (replay overlap) — drop so nothing is applied twice.
      return;
    }
    this.options.onFrame(frame);
    if (prev !== undefined && frame.seq > prev + 1) {
      // A gap: some frames were missed — refetch to converge (FRT-019).
      this.options.onReconcile({ type: "gap", projectId: pid });
    }
    this.lastSeq.set(pid, frame.seq);
  }

  private handleErrorFrame(frame: ServerFrame): void {
    const payload = frame.payload as { code?: unknown } | null;
    const code =
      payload && typeof payload.code === "string" ? payload.code : "";
    if (code === "token_expired" || code === "connection_cap") {
      // Force a fresh handshake with a new ticket.
      if (this.socket) {
        try {
          this.socket.close();
        } catch {
          // ignore — onclose still drives the reconnect
        }
      }
    }
  }

  // --- timers & io ----------------------------------------------------------

  private armLiveness(): void {
    this.clearLiveness();
    this.livenessTimer = this.scheduler.set(() => {
      // Server went quiet past the ping interval — treat as dead and reconnect.
      if (this.socket) {
        try {
          this.socket.close();
        } catch {
          this.handleDisconnect();
        }
      }
    }, this.timings.livenessTimeoutMs);
  }

  private clearLiveness(): void {
    if (this.livenessTimer !== null) {
      this.scheduler.clear(this.livenessTimer);
      this.livenessTimer = null;
    }
  }

  private send(data: string): void {
    // A socket that exists but has not opened yet is still CONNECTING, and
    // WebSocket.send() THROWS in that state. Dropping the frame is safe:
    // resubscribeAll() replays every desired subscription on open.
    if (!this.socket || !this.opened) {
      return;
    }
    try {
      this.socket.send(data);
    } catch {
      this.handleDisconnect();
    }
  }

  private setStatus(status: ConnectionStatus): void {
    if (status !== this._status) {
      this._status = status;
      this.options.onStatus(status);
    }
  }
}

// Unsubscribing an always-on / org topic (project_id null) still needs a frame;
// the protocol's `unsubscribeFrame` requires a project id, so build it inline.
function unsubscribeFramePlaceholder(topic: string): string {
  return JSON.stringify({
    type: "unsubscribe",
    project_id: null,
    topics: [topic],
  });
}
