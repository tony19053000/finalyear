import { useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useLogout, useSession } from "../api/hooks";
import { Button } from "../components/Button";
import { useToast } from "../components/toastContext";
import type { EventStreamStatus } from "../events/eventStream";
import { cn } from "../lib/cn";
import type { LoginState } from "../pages/loginMessages";
import { BellIcon, SignOutIcon } from "./icons";
import { Brand, MainNav } from "./MainNav";

/** Words for the screen-reader-only connection status (the bar shows no pill). */
const STREAM_LABEL: Record<EventStreamStatus, string> = {
  live: "Live",
  connecting: "Connecting…",
  reconnecting: "Reconnecting…",
  stopped: "Not connected",
};

/** The top bar's round icon buttons (product guide, account). The dashboard is dark only: no theme toggle. */
const ROUND_BASE =
  "inline-flex size-11 shrink-0 items-center justify-center rounded-full border bg-surface transition-colors";
const ROUND = `${ROUND_BASE} border-border text-fg-muted hover:border-border-strong hover:text-fg`;

/** "alice" -> "A", "ada lovelace" / "ada.lovelace" -> "AL". */
function initials(name: string): string {
  const parts = name.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const letters = parts.slice(0, 2).map((p) => p.charAt(0).toUpperCase());
  return letters.join("") || "?";
}

/** The bell opens the product guide (every signed-in user; alerts stay on the Overview). */
function GuideLink() {
  return (
    <Link to="/guide" className={ROUND} aria-label="Product guide" title="Product guide">
      <BellIcon />
    </Link>
  );
}

interface AccountMenuProps {
  name: string;
  role: string;
  signingOut: boolean;
  onSignOut: () => void;
}

/**
 * Round avatar with the user's initials; a click discloses who is signed in
 * and "Sign out". A disclosure (not an ARIA menu): the panel follows the
 * button in the tab order, Escape or a click/focus outside closes it.
 */
function AccountMenu({ name, role, signingOut, onSignOut }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      if (event.target instanceof Node && root.current?.contains(event.target) !== true) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        title={`${name} (${role})`}
        className={cn(
          ROUND_BASE,
          "font-display text-sm font-semibold",
          open ? "border-accent text-accent" : "border-border text-fg hover:border-border-strong",
        )}
        onClick={() => {
          setOpen((v) => !v);
        }}
      >
        <span aria-hidden="true">{initials(name)}</span>
        <span className="sr-only">
          Account:{" "}
          <span data-testid="current-user">
            {name}
            <span>({role})</span>
          </span>
        </span>
      </button>
      {open && (
        <div
          id={panelId}
          className="absolute top-full right-0 z-40 mt-2 w-56 rounded-2xl border border-border bg-surface-raised p-1.5 shadow-lg"
        >
          <div className="px-3 pt-2 pb-2.5">
            <p className="truncate text-sm font-medium text-fg" title={name}>
              {name}
            </p>
            <p className="text-xs text-fg-muted">{role}</p>
          </div>
          <div className="my-1 border-t border-border" />
          <Button
            variant="ghost"
            className="h-9! w-full justify-start! gap-2! rounded-xl! px-3! text-sm!"
            loading={signingOut}
            onClick={onSignOut}
          >
            <SignOutIcon />
            Sign out
          </Button>
        </div>
      )}
    </div>
  );
}

export interface HeaderProps {
  streamStatus: EventStreamStatus;
  /** Called before the logout request, so the shell knows the session end is ours. */
  onSigningOut?: () => void;
}

export function Header({ streamStatus, onSigningOut }: HeaderProps) {
  const session = useSession();
  const logout = useLogout();
  const navigate = useNavigate();
  const toast = useToast();
  const user = session.data?.user;
  // One sign-out per click burst: TanStack publishes `isPending` on its next
  // tick, so a double click could reach `mutate` twice before the button is
  // disabled (V0.8 G5: G2 tester's "double-click sends one DELETE" flaked).
  const signingOut = useRef(false);

  const signOut = () => {
    if (signingOut.current) return;
    signingOut.current = true;
    onSigningOut?.();
    logout.mutate(undefined, {
      onSettled: (_data, error) => {
        const state: LoginState = { reason: "logged_out" };
        void navigate("/login", { replace: true, state });
        logout.forgetUser();
        if (error) {
          toast({
            title: "Signed out in this tab",
            description: "The controller did not confirm the logout; the session ends at its expiry or when revoked.",
            tone: "failed",
          });
        }
      },
    });
  };

  return (
    <header className="flex shrink-0 flex-wrap items-center gap-x-6 gap-y-4 px-6 pt-6 pb-3 lg:grid lg:grid-cols-[1fr_auto_1fr]">
      <div className="lg:justify-self-start">
        <Brand />
      </div>
      <div className="order-last w-full min-w-0 lg:order-none lg:w-auto lg:justify-self-center">
        <MainNav />
      </div>
      <div className="ml-auto flex items-center gap-2.5 lg:ml-0 lg:justify-self-end">
        <span role="status" className="sr-only" data-testid="stream-status" data-status={streamStatus}>
          Live updates: {STREAM_LABEL[streamStatus]}
        </span>
        {user && <GuideLink />}
        {user && <AccountMenu name={user.name} role={user.role} signingOut={logout.isPending} onSignOut={signOut} />}
      </div>
    </header>
  );
}
