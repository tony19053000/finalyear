import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SocketLike } from "../../events/eventStream";
import { LogFollower, type LogSnapshot } from "./logFollower";

class FakeSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closedByClient = false;
  constructor(readonly url: string) {}
  close(): void {
    this.closedByClient = true;
  }
  open(): void {
    this.onopen?.(new Event("open"));
  }
  frame(frame: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(frame) }));
  }
  serverClose(code: number, reason = ""): void {
    this.onclose?.(new CloseEvent("close", { code, reason }));
  }
}

const b64 = (text: string | Uint8Array) =>
  btoa(String.fromCharCode(...(typeof text === "string" ? new TextEncoder().encode(text) : text)));

function setup(options: Partial<ConstructorParameters<typeof LogFollower>[0]> = {}) {
  const sockets: FakeSocket[] = [];
  let last: LogSnapshot | null = null;
  const onUnauthorized = vi.fn();
  const follower = new LogFollower({
    jobId: "job_01",
    origin: "wss://ctl:47110",
    socketFactory: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
    onUpdate: (s) => {
      last = s;
    },
    onUnauthorized,
    ...options,
  });
  const socket = () => {
    const s = sockets.at(-1);
    if (s === undefined) throw new Error("no socket");
    return s;
  };
  const snap = () => {
    if (last === null) throw new Error("no snapshot");
    return last;
  };
  return { follower, sockets, socket, snap, onUnauthorized };
}

const texts = (s: LogSnapshot) => s.lines.map((l) => l.text);

describe("LogFollower (job logs over /v1/jobs/{id}/logs/ws)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("connects with zero offsets, splits lines per stream, keeps partial lines", () => {
    const { follower, socket, snap } = setup();
    follower.start();
    expect(socket().url).toBe("wss://ctl:47110/v1/jobs/job_01/logs/ws?stream=both&stdout_offset=0&stderr_offset=0");
    socket().open();
    expect(snap().status).toBe("live");
    socket().frame({ stream: "stdout", offset: 0, data: b64("hello\nwor") });
    socket().frame({ stream: "stderr", offset: 0, data: b64("oops\n") });
    socket().frame({ stream: "stdout", offset: 9, data: b64("ld\n") });
    expect(texts(snap())).toEqual(["hello", "oops", "world"]);
    expect(snap().lines.map((l) => l.stream)).toEqual(["stdout", "stderr", "stdout"]);
    expect(snap().offsets).toEqual({ stdout: 12, stderr: 5 });
  });

  it("decodes a UTF-8 character split across two frames once, whole", () => {
    const { follower, socket, snap } = setup();
    follower.start();
    socket().open();
    const bytes = new TextEncoder().encode("é!\n"); // c3 a9 21 0a
    socket().frame({ stream: "stdout", offset: 0, data: b64(bytes.subarray(0, 1)) });
    socket().frame({ stream: "stdout", offset: 1, data: b64(bytes.subarray(1)) });
    expect(texts(snap())).toEqual(["é!"]);
  });

  it("pause closes the socket; resume reconnects from the received offsets (nothing lost or repeated)", () => {
    const { follower, socket, snap, sockets } = setup();
    follower.start();
    socket().open();
    socket().frame({ stream: "stdout", offset: 0, data: b64("a\nb\n") });
    follower.pause();
    expect(sockets[0]?.closedByClient).toBe(true);
    expect(snap().status).toBe("paused");
    follower.resume();
    expect(socket().url).toContain("stdout_offset=4&stderr_offset=0");
    socket().open();
    // The server replays from 4: a frame overlapping what we have is trimmed.
    socket().frame({ stream: "stdout", offset: 2, data: b64("b\nc\n") });
    expect(texts(snap())).toEqual(["a", "b", "c"]);
  });

  it("a dropped connection reconnects with backoff from the offsets", () => {
    const { follower, socket, snap, sockets } = setup();
    follower.start();
    socket().open();
    socket().frame({ stream: "stderr", offset: 0, data: b64("x\n") });
    socket().serverClose(1006);
    expect(snap().status).toBe("reconnecting");
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(600);
    expect(sockets).toHaveLength(2);
    expect(socket().url).toContain("stderr_offset=2");
  });

  it("4008 slow consumer reconnects too; 4029 says why", () => {
    const { follower, socket, snap, sockets } = setup();
    follower.start();
    socket().open();
    socket().serverClose(4029);
    expect(snap().detail).toMatch(/Too many viewers/);
    vi.advanceTimersByTime(1000);
    socket().open();
    socket().serverClose(4008);
    vi.advanceTimersByTime(1000);
    expect(sockets.length).toBeGreaterThanOrEqual(3);
  });

  it("4401 ends for good and asks the app to re-check the session", () => {
    const { follower, socket, snap, sockets, onUnauthorized } = setup();
    follower.start();
    socket().open();
    socket().serverClose(4401);
    expect(snap().status).toBe("unauthorized");
    expect(onUnauthorized).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it("a handshake refused before open re-checks the session and retries", () => {
    const { follower, socket, sockets, onUnauthorized } = setup();
    follower.start();
    socket().serverClose(1006);
    expect(onUnauthorized).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
  });

  it("4404 (no such job / logs not kept) stops with a reason", () => {
    const { follower, socket, snap } = setup();
    follower.start();
    socket().open();
    socket().serverClose(4404, "logs_not_kept");
    expect(snap().status).toBe("not_found");
    expect(snap().detail).toMatch(/does not keep logs/);
  });

  it("a retry frame writes a notice and restarts the offsets for the new attempt", () => {
    const { follower, socket, snap } = setup();
    follower.start();
    socket().open();
    socket().frame({ stream: "stdout", offset: 0, data: b64("first\npart") });
    socket().frame({
      retry: true,
      attempt_id: "att_2",
      previous_attempt_id: "att_1",
      previous_state: "lost",
      previous_failure_type: "node_lost",
    });
    socket().frame({ stream: "stdout", offset: 0, data: b64("second\n") });
    expect(texts(snap())).toEqual([
      "first",
      "part",
      "── attempt att_1 lost (node_lost); retrying as att_2 ──",
      "second",
    ]);
    expect(snap().lines[2]?.notice).toBe(true);
    expect(snap().offsets.stdout).toBe(7);
  });

  it("the end frame flushes the last partial line, records the outcome and stops", () => {
    const { follower, socket, snap, sockets } = setup();
    follower.start();
    socket().open();
    socket().frame({ stream: "stdout", offset: 0, data: b64("no newline") });
    socket().frame({
      end: true,
      state: "succeeded",
      attempt_state: "done",
      exit_code: 0,
      failure_type: null,
      gaps: { stdout: [[10, 20]] },
      final: { stdout: true, stderr: true },
    });
    expect(texts(snap())).toEqual(["no newline"]);
    expect(snap().status).toBe("ended");
    expect(snap().end).toEqual({
      state: "succeeded",
      attemptState: "done",
      exitCode: 0,
      failureType: null,
      final: { stdout: true, stderr: true },
      gaps: { stdout: [[10, 20]] },
    });
    socket().serverClose(1000);
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it("is bounded: keeps the last maxLines lines and counts the dropped ones", () => {
    const { follower, socket, snap } = setup({ maxLines: 100 });
    follower.start();
    socket().open();
    const many = Array.from({ length: 1000 }, (_, i) => `line ${String(i)}`).join("\n") + "\n";
    socket().frame({ stream: "stdout", offset: 0, data: b64(many) });
    expect(snap().lines).toHaveLength(100);
    expect(snap().lines[0]?.text).toBe("line 900");
    expect(snap().dropped).toBe(900);
  });

  it("cuts an endless line and ignores garbage frames", () => {
    const { follower, socket, snap } = setup({ maxLineChars: 10 });
    follower.start();
    socket().open();
    socket().onmessage?.(new MessageEvent("message", { data: "not json" }));
    socket().frame({ stream: "stdin", offset: 0, data: b64("x\n") });
    socket().frame({ stream: "stdout", offset: 0, data: "%%%not-base64" });
    socket().frame({ stream: "stdout", offset: 0, data: b64("abcdefghijklmnop") });
    expect(texts(snap())).toEqual(["abcdefghij … [line cut]"]);
  });
});
