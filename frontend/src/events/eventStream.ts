/**
 * Client of the controller's one live stream, `GET /v1/events/ws` (D172,
 * ARCHITECTURE §20.1). Framework-free so it is unit-tested with a fake socket.
 *
 * Protocol (server: controller/api/event_routes.py):
 *   first frame  {"type":"hello", epoch, seq, resumed} | {"type":"resync", epoch, seq, reason}
 *   then         {seq, type, at, data}
 * - The first connection sends no position. Every reconnect sends BOTH
 *   `since=<last seq seen>` and `epoch=<bus epoch>`, so the server can replay
 *   exactly what was missed, or answer `resync` (ring overflowed, controller
 *   restarted = new epoch, or a seq from the future) - then `onResync` runs
 *   and the app refetches; it never guesses.
 * - The position advances only on events actually received: events not
 *   visible to this user are never sent, and moving past them on `hello`
 *   could skip a replay still in flight.
 * - Reconnect with exponential backoff and full jitter; reset after a
 *   connection reached `hello`.
 * - Close 4401 (session ended / revoked) stops for good and calls
 *   `onUnauthorized`; a handshake refused before `open` (403 - also a dead
 *   session) calls `onHandshakeFailed` so the app can re-check its session,
 *   then keeps retrying with backoff.
 * Auth is the session cookie the browser attaches to the same-origin
 * handshake; never a token in the URL.
 */

export type EventStreamStatus = "connecting" | "live" | "reconnecting" | "stopped";

export interface FabricEvent {
  seq: number;
  type: string;
  /** Server time, seconds since the epoch. */
  at: number;
  data: Record<string, unknown>;
}

export type ResyncReason = "ring" | "epoch" | "future";

/** The subset of the browser WebSocket the client uses (a fake in tests). */
export interface SocketLike {
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close(code?: number, reason?: string): void;
}

export type SocketFactory = (url: string) => SocketLike;

export interface EventStreamOptions {
  /** `wss://host/v1/events/ws` (built from `location` by default). */
  baseUrl?: string;
  socketFactory?: SocketFactory;
  onEvent: (event: FabricEvent) => void;
  onResync: (reason: ResyncReason) => void;
  onStatus?: (status: EventStreamStatus) => void;
  onUnauthorized?: () => void;
  onHandshakeFailed?: () => void;
  backoffBaseMs?: number;
  backoffCapMs?: number;
  random?: () => number;
}

export const CLOSE_SLOW_CONSUMER = 4008;
export const CLOSE_TOO_MANY = 4029;
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_AUTH_UNAVAILABLE = 4503;

export function defaultEventsUrl(loc: Location = window.location): string {
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}/v1/events/ws`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export class EventStreamClient {
  private readonly opts: Required<Omit<EventStreamOptions, "onUnauthorized" | "onHandshakeFailed" | "onStatus">> &
    Pick<EventStreamOptions, "onUnauthorized" | "onHandshakeFailed" | "onStatus">;
  private socket: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private stoppedForGood = false;
  private statusValue: EventStreamStatus = "stopped";
  /** Position in the bus: the epoch and the last seq received. */
  epoch: string | null = null;
  seq: number | null = null;

  constructor(options: EventStreamOptions) {
    this.opts = {
      baseUrl: options.baseUrl ?? defaultEventsUrl(),
      socketFactory: options.socketFactory ?? ((url) => new WebSocket(url)),
      onEvent: options.onEvent,
      onResync: options.onResync,
      onStatus: options.onStatus,
      onUnauthorized: options.onUnauthorized,
      onHandshakeFailed: options.onHandshakeFailed,
      backoffBaseMs: options.backoffBaseMs ?? 500,
      backoffCapMs: options.backoffCapMs ?? 15_000,
      random: options.random ?? Math.random,
    };
  }

  get status(): EventStreamStatus {
    return this.statusValue;
  }

  /** The URL of the next connection attempt. */
  url(): string {
    if (this.seq === null || this.epoch === null) return this.opts.baseUrl;
    const params = new URLSearchParams({ since: String(this.seq), epoch: this.epoch });
    return `${this.opts.baseUrl}?${params.toString()}`;
  }

  start(): void {
    this.stoppedForGood = false;
    if (this.socket === null && this.timer === null) this.open();
  }

  /** Close and never reconnect (logout, unmount). */
  stop(): void {
    this.stoppedForGood = true;
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const socket = this.socket;
    this.socket = null;
    if (socket !== null) {
      socket.onclose = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onopen = null;
      socket.close(1000, "client stopped");
    }
    this.setStatus("stopped");
  }

  private setStatus(status: EventStreamStatus): void {
    if (status === this.statusValue) return;
    this.statusValue = status;
    this.opts.onStatus?.(status);
  }

  private open(): void {
    this.timer = null;
    this.setStatus(this.failures === 0 && this.seq === null ? "connecting" : "reconnecting");
    let socket: SocketLike;
    try {
      socket = this.opts.socketFactory(this.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    let opened = false;
    let greeted = false;
    socket.onopen = () => {
      opened = true;
    };
    socket.onmessage = (ev: MessageEvent) => {
      if (this.socket !== socket) return;
      const frame = this.parse(ev.data);
      if (frame === null) return;
      if (!greeted) {
        greeted = this.greet(frame);
        return;
      }
      this.dispatch(frame);
    };
    socket.onerror = () => {
      // Always followed by close; handled there.
    };
    socket.onclose = (ev: CloseEvent) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (ev.code === CLOSE_UNAUTHORIZED) {
        this.stoppedForGood = true;
        this.setStatus("stopped");
        this.opts.onUnauthorized?.();
        return;
      }
      if (!opened) this.opts.onHandshakeFailed?.();
      if (greeted) this.failures = 0;
      if (!this.stoppedForGood) this.scheduleReconnect();
    };
  }

  private parse(data: unknown): Record<string, unknown> | null {
    if (typeof data !== "string") return null;
    try {
      const value: unknown = JSON.parse(data);
      return isRecord(value) ? value : null;
    } catch {
      return null;
    }
  }

  /** Handle the first frame; true when it was a valid hello/resync. */
  private greet(frame: Record<string, unknown>): boolean {
    const { type, epoch, seq } = frame;
    if ((type !== "hello" && type !== "resync") || typeof epoch !== "string" || typeof seq !== "number") {
      return false;
    }
    const resumed = type === "hello" && frame.resumed === true;
    this.epoch = epoch;
    if (!resumed) {
      // Fresh start or resync: nothing before `seq` is owed to us.
      this.seq = seq;
    }
    this.failures = 0;
    this.setStatus("live");
    if (type === "resync") {
      const reason = frame.reason;
      this.opts.onResync(reason === "ring" || reason === "epoch" || reason === "future" ? reason : "ring");
    }
    return true;
  }

  private dispatch(frame: Record<string, unknown>): void {
    const { seq, type, at, data } = frame;
    if (typeof seq !== "number" || typeof type !== "string") return;
    if (this.seq !== null && seq <= this.seq) return; // a replay we already had
    this.seq = seq;
    this.opts.onEvent({ seq, type, at: typeof at === "number" ? at : 0, data: isRecord(data) ? data : {} });
  }

  /** Delay before reconnect attempt number `failures` (full jitter). */
  backoffMs(failures: number): number {
    const ceiling = Math.min(this.opts.backoffCapMs, this.opts.backoffBaseMs * 2 ** failures);
    return Math.round(this.opts.random() * ceiling);
  }

  private scheduleReconnect(): void {
    if (this.stoppedForGood) return;
    this.setStatus("reconnecting");
    const delay = this.backoffMs(this.failures);
    this.failures += 1;
    this.timer = setTimeout(() => {
      this.open();
    }, delay);
  }
}
