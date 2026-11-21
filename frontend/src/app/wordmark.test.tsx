// C2: the brand is the "hive." wordmark only - the hive mark is gone.
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import * as icons from "./icons";
import { Brand } from "./MainNav";
import { Wordmark } from "./Wordmark";

describe("hive. wordmark", () => {
  it("is the word plus the honey hex dot, hidden from assistive tech", () => {
    const { container } = render(<Wordmark />);
    const mark = screen.getByTestId("hive-wordmark");
    expect(mark.getAttribute("aria-hidden")).toBe("true");
    expect(mark.textContent).toBe("hive");
    expect(mark.querySelector("span.bg-accent")).not.toBeNull();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("sizes: bar 34px (was 25px), login 40px", () => {
    render(
      <>
        <Wordmark />
        <Wordmark size="login" />
      </>,
    );
    const [bar, login] = screen.getAllByTestId("hive-wordmark");
    expect(bar?.className).toContain("text-[34px]");
    expect(login?.className).toContain("text-[40px]");
  });

  it("Brand links home with an accessible name and no mark", () => {
    render(
      <MemoryRouter>
        <Brand />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: "Hive home" });
    expect(link.getAttribute("href")).toBe("/");
    expect(link.querySelector("svg")).toBeNull();
    expect(within(link).getByTestId("hive-wordmark")).toBeTruthy();
  });

  it("the HiveMark icon is removed (LogoMark for setup stays)", () => {
    expect("HiveMark" in icons).toBe(false);
    expect("LogoMark" in icons).toBe(true);
  });
});
