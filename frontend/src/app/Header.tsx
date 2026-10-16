import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useLogout, useNodes, useSession } from "../api/hooks";
import { Button } from "../components/Button";
import { StatusDot } from "../components/Status";
import { useToast } from "../components/toastContext";
import type { EventStreamStatus } from "../events/eventStream";
import type { StatusTone } from "../lib/status";
import type { LoginState } from "../pages/loginMessages";
import { applyTheme, saveTheme, storedTheme, type Theme } from "./theme";

const STREAM: Record<EventStreamStatus, { tone: StatusTone; label: string }> = {
  live: { tone: "ok", label: "Live" },
  connecting: { tone: "unknown", label: "Connecting…" },
  reconnecting: { tone: "degraded", label: "Reconnecting…" },
  stopped: { tone: "unknown", label: "Not connected" },
};

/** "2 of 3 nodes online", from `GET /v1/nodes` (refreshed by node.status events). */
function FabricStatus() {
  const nodes = useNodes();
  if (nodes.isPending) {
    return <span className="text-fg-subtle">Loading nodes…</span>;
  }
  if (nodes.isError) {
    return <StatusDot tone="failed" label="Nodes unavailable" />;
  }
  const total = nodes.data.nodes.length;
  const online = nodes.data.nodes.filter((n) => n.state === "online").length;
  const tone: StatusTone = total === 0 ? "unknown" : online === total ? "ok" : online === 0 ? "failed" : "degraded";
  return (
    <span data-testid="fabric-status">
      <StatusDot
        tone={tone}
        label={total === 0 ? "No nodes yet" : `${online} of ${total} node${total === 1 ? "" : "s"} online`}
      />
    </span>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => storedTheme());
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <Button
      variant="ghost"
      size="sm"
      aria-label={`Switch to ${next} theme`}
      onClick={() => {
        applyTheme(next);
        saveTheme(next);
        setTheme(next);
      }}
    >
      {theme === "dark" ? "Light" : "Dark"}
    </Button>
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
  const stream = STREAM[streamStatus];
  const user = session.data?.user;

  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-4 border-b border-border bg-surface px-6">
      <div className="flex items-center gap-5 text-sm" aria-label="Fabric status" role="group">
        <FabricStatus />
        <span className="text-fg-muted" data-testid="stream-status" data-status={streamStatus}>
          <StatusDot tone={stream.tone} label={<span className="text-xs">{stream.label}</span>} />
        </span>
      </div>
      <div className="flex items-center gap-2">
        {user && (
          <span className="text-sm text-fg-muted" data-testid="current-user">
            <span className="text-fg">{user.name}</span>
            <span className="ml-1.5 text-xs">({user.role})</span>
          </span>
        )}
        <ThemeToggle />
        <Button
          variant="secondary"
          size="sm"
          loading={logout.isPending}
          onClick={() => {
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
          }}
        >
          Sign out
        </Button>
      </div>
    </header>
  );
}
