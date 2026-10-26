/** V0.17 G5 (Q37): the Nodes page shows each agent's version, and a badge when
 * it is one minor behind (accepted) or refused - straight from the API's
 * `agent_version_status`, never computed in the browser. */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentVersion, BEHIND_HELP, UNSUPPORTED_HELP } from "../pages/nodes/AgentVersion";

describe("AgentVersion", () => {
  it("same minor: the version, no badge", () => {
    render(<AgentVersion node={{ agent_version: "0.17.2", agent_version_status: "same" }} />);
    expect(screen.getByText("0.17.2")).toBeTruthy();
    expect(screen.queryByText("update available")).toBeNull();
    expect(screen.queryByText("unsupported")).toBeNull();
  });

  it("one minor behind: still shown as accepted, with an 'update available' badge and why", () => {
    render(<AgentVersion node={{ agent_version: "0.16.4", agent_version_status: "behind" }} />);
    expect(screen.getByText("0.16.4")).toBeTruthy();
    const badge = screen.getByText("update available");
    expect(badge.closest("[title]")?.getAttribute("title")).toBe(BEHIND_HELP);
    expect(badge.closest("[data-tone]")?.getAttribute("data-tone")).toBe("degraded");
  });

  it("refused (too old or too new): an 'unsupported' badge naming version_unsupported", () => {
    render(<AgentVersion node={{ agent_version: "0.18.0", agent_version_status: "unsupported" }} />);
    const badge = screen.getByText("unsupported");
    expect(badge.closest("[title]")?.getAttribute("title")).toBe(UNSUPPORTED_HELP);
    expect(UNSUPPORTED_HELP).toContain("version_unsupported");
    expect(badge.closest("[data-tone]")?.getAttribute("data-tone")).toBe("failed");
  });

  it("no version (an offline node in the list): a dash, never a guess", () => {
    render(<AgentVersion node={{ agent_version: null, agent_version_status: null }} />);
    expect(screen.getByText("—")).toBeTruthy();
  });
});
