import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError } from "../api/errors";
import { Button } from "./Button";
import { Card } from "./Card";
import { CodeBlock, LogView } from "./CodeBlock";
import { Dialog } from "./Dialog";
import { Sparkline } from "./Sparkline";
import { EmptyState, ErrorState } from "./States";
import { Badge, StatusDot } from "./Status";
import { Table } from "./Table";
import { Tabs } from "./Tabs";
import { ToastProvider } from "./Toast";
import { useToast } from "./toastContext";

describe("Button", () => {
  it("defaults to type=button so it never submits a form by accident", () => {
    render(<Button>Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toHaveProperty("type", "button");
  });

  it("is disabled and announces busy while loading", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toHaveProperty("disabled", true);
    expect(button.getAttribute("aria-busy")).toBe("true");
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("StatusDot and Badge", () => {
  it("prints the state next to the colour (never colour alone)", () => {
    render(<StatusDot tone="degraded" label="suspect" />);
    const text = screen.getByText("suspect");
    expect(text.parentElement?.getAttribute("data-tone")).toBe("degraded");
  });

  it("badge carries its tone", () => {
    render(<Badge tone="ok">succeeded</Badge>);
    expect(screen.getByText("succeeded").getAttribute("data-tone")).toBe("ok");
  });
});

describe("Card", () => {
  it("is a region named by its title", () => {
    render(
      <Card title="Nodes" actions={<Button>Refresh</Button>}>
        body
      </Card>,
    );
    const region = screen.getByRole("region", { name: "Nodes" });
    expect(within(region).getByRole("button", { name: "Refresh" })).toBeTruthy();
  });
});

describe("Table", () => {
  const rows = [
    { id: "a", name: "alpha", size: 1 },
    { id: "b", name: "beta", size: 22 },
  ];
  const columns = [
    { id: "name", header: "Name", cell: (r: (typeof rows)[number]) => r.name },
    { id: "size", header: "Size", cell: (r: (typeof rows)[number]) => r.size, align: "right" as const, mono: true },
  ];

  it("renders a captioned table with header cells and rows", () => {
    render(<Table caption="Things" columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const table = screen.getByRole("table", { name: "Things" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Name", "Size"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("22").className).toContain("text-right");
  });

  it("shows the empty content when there are no rows", () => {
    render(<Table caption="Things" columns={columns} rows={[]} rowKey={(r) => r.id} empty="No things yet" />);
    expect(screen.getByText("No things yet")).toBeTruthy();
  });

  it("reports row clicks", async () => {
    const onRowClick = vi.fn();
    render(<Table caption="Things" columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={onRowClick} />);
    await userEvent.click(screen.getByText("beta"));
    expect(onRowClick).toHaveBeenCalledWith(rows[1]);
  });
});

describe("Dialog", () => {
  it("opens modally with its title and description, and closes on Escape", () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <Dialog open onClose={onClose} title="Revoke node" description="It can no longer connect." footer={<Button>Revoke</Button>}>
        details
      </Dialog>,
    );
    const dialog = screen.getByRole("dialog", { name: "Revoke node" });
    expect(dialog.hasAttribute("open")).toBe(true);
    expect(dialog.getAttribute("aria-describedby")).toBeTruthy();
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
    rerender(
      <Dialog open={false} onClose={onClose} title="Revoke node">
        details
      </Dialog>,
    );
    expect(dialog.hasAttribute("open")).toBe(false);
  });

  it("closes on a backdrop click but not on a click inside", async () => {
    const onClose = vi.fn();
    render(
      <Dialog open onClose={onClose} title="T">
        <p>inside</p>
      </Dialog>,
    );
    await userEvent.click(screen.getByText("inside"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("Tabs", () => {
  it("switches panels by click and by arrow key", async () => {
    render(
      <Tabs
        label="Job sections"
        items={[
          { value: "plan", label: "Plan", content: "plan table" },
          { value: "logs", label: "Logs", content: "log lines" },
        ]}
      />,
    );
    expect(screen.getByRole("tablist", { name: "Job sections" })).toBeTruthy();
    expect(screen.getByRole("tabpanel").textContent).toBe("plan table");
    await userEvent.click(screen.getByRole("tab", { name: "Logs" }));
    expect(screen.getByRole("tabpanel").textContent).toBe("log lines");
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Plan" }).getAttribute("aria-selected")).toBe("true");
  });
});

describe("Toast", () => {
  function Trigger() {
    const toast = useToast();
    return (
      <Button
        onClick={() => {
          toast({ title: "Job cancelled", description: "job_123", tone: "ok" });
        }}
      >
        notify
      </Button>
    );
  }

  it("shows a toast from useToast()", async () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "notify" }));
    expect(await screen.findByText("Job cancelled")).toBeTruthy();
    expect(screen.getByText("job_123")).toBeTruthy();
  });

  it("useToast() outside the provider fails loudly", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Trigger />)).toThrow(/ToastProvider/);
  });
});

describe("CodeBlock and LogView", () => {
  it("code block is a labelled, focusable pre", () => {
    render(<CodeBlock label="join command" code="meshforge-agent join X" />);
    const pre = screen.getByLabelText("join command");
    expect(pre.tagName).toBe("PRE");
    expect(pre.tabIndex).toBe(0);
  });

  it("renders log bytes as text, never as HTML", () => {
    render(<LogView label="stdout" lines={[{ key: "0", text: "<img src=x onerror=alert(1)>" }]} />);
    const log = screen.getByRole("log", { name: "stdout" });
    expect(log.querySelector("img")).toBeNull();
    expect(log.textContent).toContain("<img src=x onerror=alert(1)>");
  });

  it("keeps only maxLines and says how many were cut; marks stderr", () => {
    const lines = Array.from({ length: 5 }, (_, i) => ({
      key: String(i),
      text: `line ${i}`,
      stream: i === 4 ? ("stderr" as const) : ("stdout" as const),
    }));
    render(<LogView label="out" lines={lines} maxLines={2} />);
    expect(screen.getByText(/3 earlier lines not shown/)).toBeTruthy();
    expect(screen.queryByText("line 0")).toBeNull();
    expect(screen.getByText("line 4").getAttribute("data-stream")).toBe("stderr");
  });

  it("says so when there is no output", () => {
    render(<LogView label="out" lines={[]} />);
    expect(screen.getByText("No output yet.")).toBeTruthy();
  });
});

describe("Sparkline", () => {
  it("summarises the series for screen readers", () => {
    render(
      <Sparkline
        label="CPU %"
        points={[
          { t: 1, v: 10 },
          { t: 2, v: 30 },
          { t: 3, v: 20 },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: "CPU %: now 20.0, min 10.0, max 30.0 over 3 samples" })).toBeTruthy();
  });

  it("draws nothing invented for fewer than two samples", () => {
    render(<Sparkline label="RAM" points={[]} />);
    expect(screen.getByRole("img", { name: "RAM: no samples" }).querySelector("svg")).toBeNull();
  });
});

describe("EmptyState and ErrorState", () => {
  it("empty state shows its title, text and action", () => {
    render(
      <EmptyState title="No jobs yet" action={<Button>Submit</Button>}>
        Submit one with compute run.
      </EmptyState>,
    );
    expect(screen.getByRole("heading", { name: "No jobs yet" })).toBeTruthy();
    expect(screen.getByText("Submit one with compute run.")).toBeTruthy();
  });

  it("error state shows the API message and code, and retries", async () => {
    const onRetry = vi.fn();
    render(
      <ErrorState
        title="Could not load nodes"
        error={new ApiError(503, "network_error", "the user database is unavailable; retry")}
        onRetry={onRetry}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("the user database is unavailable; retry")).toBeTruthy();
    expect(within(alert).getByText("code: network_error")).toBeTruthy();
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("error state explains a network failure", () => {
    render(<ErrorState title="Offline" error={new NetworkError("The controller did not answer.")} />);
    expect(screen.getByText(/did not answer\. Check that the controller is running/)).toBeTruthy();
  });
});
