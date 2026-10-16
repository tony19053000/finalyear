/**
 * Follows one job's logs over `/v1/jobs/{id}/logs/ws` (V0.4 D72, cookie auth
 * since D170; the server re-checks the session while it is open, D157).
 *
 * Framework-free (a fake socket in tests). Frames (log_routes.py):
 * `{stream, offset, data: base64}` · `{retry: true, attempt_id, ...}` (the
 * job was retried; later frames are the new attempt's, from offset 0) ·
 * `{end: true, state, exit_code, failure_type, gaps, final}` then a normal
 * close.
 *
 * - **Bounded:** at most `maxLines` lines are kept (older ones are dropped
 *   and counted); a line longer than `maxLineChars` is cut. The full log
 *   stays downloadable (GET /v1/jobs/{id}/logs).
 * - **Pause / resume:** pause closes the socket; resume reconnects with
 *   `stdout_offset`/`stderr_offset` = the bytes already received, so nothing
 *   is lost or repeated. A dropped connection (network, 4008 slow consumer,
 *   4503) reconnects the same way with backoff.
 * - **UTF-8:** each stream has its own streaming decoder, so a character
 *   split across two frames is decoded once, whole.
 * - Text only: lines are rendered as text by LogView, never as HTML.
 */
import type { SocketLike } from "../../events/eventStream";

export type LogStream = "stdout" | "stderr";

export interface LogLineOut {
  key: string;
  text: string;
  stream?: LogStream;
  /** A line the dashboard wrote (retry / end notices), not workload output. */
  notice?: boolean;
}

export interface LogEnd {
  state: string | null;
  attemptState: string | null;
  exitCode: number | null;
  failureType: string | null;
  /** Streams whose final log was uploaded and verified. */
  final: Record<string, boolean>;
  /** Byte ranges the live stream missed and no final log replaced. */
  gaps: Record<string, number[][]>;
}

export type LogStatus =
  | "connecting"
  | "live"
  | "paused"
  | "reconnecting"
  | "ended"
  | "unauthorized"
  | "not_found"
  | "unavailable";

export interface LogSnapshot {
  lines: readonly LogLineOut[];
  /** Lines dropped from the start of the buffer (it is bounded). */
  dropped: number;
  status: LogStatus;
  end: LogEnd | null;
  /** Bytes received per stream of the attempt being followed. */
  offsets: Readonly<Record<LogStream, number>>;
  /** Human-readable reason for the last disconnect, if any. */
  detail: string | null;
}

export interface LogFollowerOptions {
  jobId: string;
  /** `wss://host` (from `location` by default). */
  origin?: string;
  socketFactory?: (url: string) => SocketLike;
  onUpdate: (snapshot: LogSnapshot) => void;
  /** The session ended (4401): the app re-checks it (-> login page). */
  onUnauthorized?: () => void;
  maxLines?: number;
  maxLineChars?: number;
  backoffBaseMs?: number;
  backoffCapMs?: number;
}

export const CLOSE_NORMAL = 1000;
export const CLOSE_INVALID = 4400;
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_NOT_FOUND = 4404;
export const CLOSE_TOO_MANY = 4029;

export function defaultOrigin(loc: Location = window.location): string {
  return `${loc.protocol === "https:" ? "wss:" : "ws:"}//${loc.host}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function decodeBase64(data: string): Uint8Array {
  const binary = atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export class LogFollower {
  private readonly opts: Required<Omit<LogFollowerOptions, "onUnauthorized">> &
    Pick<LogFollowerOptions, "onUnauthorized">;
  private socket: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private stopped = false;
  private lines: LogLineOut[] = [];
  private dropped = 0;
  private status: LogStatus = "connecting";
  private end: LogEnd | null = null;
  private detail: string | null = null;
  private attempt = 0;
  private offsets: Record<LogStream, number> = { stdout: 0, stderr: 0 };
  private partial: Record<LogStream, string> = { stdout: "", stderr: "" };
  private decoders: Record<LogStream, TextDecoder> = this.newDecoders();
  private noticeSeq = 0;

  constructor(options: LogFollowerOptions) {
    this.opts = {
      jobId: options.jobId,
      origin: options.origin ?? defaultOrigin(),
      socketFactory: options.socketFactory ?? ((url) => new WebSocket(url)),
      onUpdate: options.onUpdate,
      onUnauthorized: options.onUnauthorized,
      maxLines: options.maxLines ?? 5000,
      maxLineChars: options.maxLineChars ?? 16_384,
      backoffBaseMs: options.backoffBaseMs ?? 500,
      backoffCapMs: options.backoffCapMs ?? 15_000,
    };
  }

  private newDecoders(): Record<LogStream, TextDecoder> {
    return { stdout: new TextDecoder("utf-8"), stderr: new TextDecoder("utf-8") };
  }

  /** The URL of the next connection (resumes from the received offsets). */
  url(): string {
    const params = new URLSearchParams({
      stream: "both",
      stdout_offset: String(this.offsets.stdout),
      stderr_offset: String(this.offsets.stderr),
    });
    return `${this.opts.origin}/v1/jobs/${encodeURIComponent(this.opts.jobId)}/logs/ws?${params.toString()}`;
  }

  snapshot(): LogSnapshot {
    return {
      lines: this.lines.slice(-this.opts.maxLines),
      dropped: this.dropped + Math.max(0, this.lines.length - this.opts.maxLines),
      status: this.status,
      end: this.end,
      offsets: { ...this.offsets },
      detail: this.detail,
    };
  }

  start(): void {
    this.stopped = false;
    if (this.socket === null && this.timer === null && this.status !== "ended") this.open();
  }

  /** Close for good (unmount). */
  stop(): void {
    this.stopped = true;
    this.closeSocket();
  }

  pause(): void {
    if (this.status === "ended") return;
    this.stopped = true;
    this.closeSocket();
    this.setStatus("paused");
  }

  resume(): void {
    if (this.status !== "paused") return;
    this.stopped = false;
    this.failures = 0;
    this.open();
  }

  private closeSocket(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const socket = this.socket;
    this.socket = null;
    if (socket !== null) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      socket.close(CLOSE_NORMAL, "client closed");
    }
  }

  private setStatus(status: LogStatus): void {
    this.status = status;
    this.emit();
  }

  private emit(): void {
    this.opts.onUpdate(this.snapshot());
  }

  private open(): void {
    this.timer = null;
    this.setStatus(this.failures === 0 ? "connecting" : "reconnecting");
    let opened = false;
    const socket = this.opts.socketFactory(this.url());
    this.socket = socket;
    socket.onopen = () => {
      opened = true;
      this.failures = 0;
      this.detail = null;
      this.setStatus("live");
    };
    socket.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") return;
      let frame: unknown;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      if (isRecord(frame)) this.onFrame(frame);
    };
    socket.onerror = () => {
      // onclose follows and decides.
    };
    socket.onclose = (event: CloseEvent) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.onClose(event.code, event.reason, opened);
    };
  }

  private onClose(code: number, reason: string, opened: boolean): void {
    if (this.status === "ended") return;
    if (code === CLOSE_UNAUTHORIZED) {
      this.detail = "Your session ended.";
      this.setStatus("unauthorized");
      this.opts.onUnauthorized?.();
      return;
    }
    if (code === CLOSE_NOT_FOUND || code === CLOSE_INVALID) {
      this.detail = reason === "logs_not_kept" ? "This controller does not keep logs." : "The job was not found.";
      this.setStatus("not_found");
      return;
    }
    if (this.stopped) return;
    if (!opened) {
      // Refused before the handshake (a 403: no session, or a cross-origin
      // page) or no answer: the session may be gone - let the app re-check.
      this.opts.onUnauthorized?.();
    }
    this.detail =
      code === CLOSE_TOO_MANY
        ? "Too many viewers are following this job; retrying."
        : "The log connection dropped; reconnecting.";
    this.failures += 1;
    const delay = Math.min(this.opts.backoffCapMs, this.opts.backoffBaseMs * 2 ** Math.min(this.failures - 1, 10));
    this.status = "reconnecting";
    this.emit();
    this.timer = setTimeout(() => {
      this.open();
    }, delay);
  }

  private onFrame(frame: Record<string, unknown>): void {
    if (frame.end === true) {
      this.flushPartials();
      const final = isRecord(frame.final) ? frame.final : {};
      const gaps = isRecord(frame.gaps) ? frame.gaps : {};
      this.end = {
        state: str(frame.state),
        attemptState: str(frame.attempt_state),
        exitCode: typeof frame.exit_code === "number" ? frame.exit_code : null,
        failureType: str(frame.failure_type),
        final: Object.fromEntries(Object.entries(final).map(([k, v]) => [k, v === true])),
        gaps: Object.fromEntries(
          Object.entries(gaps).filter((entry): entry is [string, number[][]] => Array.isArray(entry[1])),
        ),
      };
      this.stopped = true;
      this.closeSocket();
      this.setStatus("ended");
      return;
    }
    if (frame.retry === true) {
      this.flushPartials();
      this.attempt += 1;
      const previous = str(frame.previous_state) ?? "ended";
      const why = str(frame.previous_failure_type);
      this.notice(
        `── attempt ${str(frame.previous_attempt_id) ?? "?"} ${previous}${why ? ` (${why})` : ""}; ` +
          `retrying as ${str(frame.attempt_id) ?? "?"} ──`,
      );
      this.offsets = { stdout: 0, stderr: 0 };
      this.decoders = this.newDecoders();
      this.emit();
      return;
    }
    const stream = frame.stream;
    if ((stream !== "stdout" && stream !== "stderr") || typeof frame.data !== "string") return;
    const offset = typeof frame.offset === "number" ? frame.offset : this.offsets[stream];
    let bytes: Uint8Array;
    try {
      bytes = decodeBase64(frame.data);
    } catch {
      return;
    }
    if (offset + bytes.length <= this.offsets[stream]) return; // a replay of what we have
    const skip = Math.max(0, this.offsets[stream] - offset);
    const fresh = skip > 0 ? bytes.subarray(skip) : bytes;
    this.offsets[stream] = offset + bytes.length;
    this.append(stream, this.decoders[stream].decode(fresh, { stream: true }));
    this.emit();
  }

  private append(stream: LogStream, text: string): void {
    const parts = (this.partial[stream] + text).split("\n");
    this.partial[stream] = parts.pop() ?? "";
    if (this.partial[stream].length > this.opts.maxLineChars) {
      // A line without a newline for too long: show what came, keep going.
      parts.push(this.partial[stream]);
      this.partial[stream] = "";
    }
    for (const line of parts) this.push(stream, line.replace(/\r$/, ""));
  }

  private push(stream: LogStream, text: string): void {
    const cut = text.length > this.opts.maxLineChars ? `${text.slice(0, this.opts.maxLineChars)} … [line cut]` : text;
    this.noticeSeq += 1;
    this.lines.push({ key: `${this.attempt}-${stream}-${this.noticeSeq}`, text: cut, stream });
    this.trim();
  }

  private notice(text: string): void {
    this.noticeSeq += 1;
    this.lines.push({ key: `notice-${this.noticeSeq}`, text, notice: true });
    this.trim();
  }

  /** Cut the buffer back to `maxLines` in batches (not one splice per line). */
  private trim(): void {
    const slack = Math.max(64, Math.floor(this.opts.maxLines / 10));
    const over = this.lines.length - this.opts.maxLines;
    if (over > slack) {
      this.lines.splice(0, over);
      this.dropped += over;
    }
  }

  private flushPartials(): void {
    for (const stream of ["stdout", "stderr"] as const) {
      const rest = this.partial[stream] + this.decoders[stream].decode();
      this.partial[stream] = "";
      if (rest !== "") this.push(stream, rest);
    }
  }
}
