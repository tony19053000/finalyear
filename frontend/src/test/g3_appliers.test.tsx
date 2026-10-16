/**
 * V0.8 G3: the shell's event pipeline - every applier sees every event, in
 * order; one applier that throws never stops the others or the stream (the
 * merge point G4's `applyJobEvent` plugs into, events/appliers.ts).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyEvent, type EventApplier } from "../events/applyEvent";
import { EVENT_APPLIERS } from "../events/appliers";
import type { FabricEvent } from "../events/eventStream";
import { useEventStream } from "../events/useEventStream";
import { FakeSocket, event } from "./fakeFabric";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("event appliers", () => {
  it("the shell's list starts with the fabric applier", () => {
    expect(EVENT_APPLIERS[0]).toBe(applyEvent);
  });

  it("run in order for every event; a throwing one is contained", () => {
    const seen: string[] = [];
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const appliers: readonly EventApplier[] = [
      (_c, e: FabricEvent) => {
        seen.push(`a:${e.type}`);
        throw new Error("bug in a");
      },
      (_c, e: FabricEvent) => {
        seen.push(`b:${e.type}`);
      },
    ];
    const sockets: FakeSocket[] = [];
    const factory = (url: string) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    };
    const noop = () => undefined;
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(
      () =>
        useEventStream({
          enabled: true,
          onUnauthorized: noop,
          appliers,
          baseUrl: "wss://x/v1/events/ws",
          socketFactory: factory, // stable: a new factory would reconnect
        }),
      { wrapper },
    );
    act(() => {
      sockets[0]?.greet();
      sockets[0]?.send(event("job.state", { job_id: "j" }));
      sockets[0]?.send(event("node.status", { node_id: "n" }));
    });
    expect(seen).toEqual(["a:job.state", "b:job.state", "a:node.status", "b:node.status"]);
    expect(result.current).toBe("live");
    expect(sockets).toHaveLength(1); // one stream, never reconnected by the events
    expect(warn).toHaveBeenCalledTimes(2);
  });
});
