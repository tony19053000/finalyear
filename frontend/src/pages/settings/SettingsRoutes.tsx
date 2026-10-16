/**
 * The Settings section (0.8.6, D176 "rest"): `/settings/*`, one lazy chunk.
 *
 *   /settings           account, this browser session, controller, owner policy note
 *   /settings/tokens    API tokens + browser sessions: list, create (shown once), revoke
 *   /settings/secrets   job secrets: names only; set (write-only), delete
 *   /settings/data      named inputs: versions, pin/unpin, delete
 *   /settings/invites   admin: join codes (shown once, with QR), list, revoke
 *   /settings/users     admin: where users are managed (no REST API for it)
 *
 * A user sees only what the API lets them: admin sections are not in a
 * user's menu, and a direct visit says why instead of calling a 403 route.
 */
import { NavLink, Route, Routes } from "react-router-dom";

import { useSession } from "../../api/hooks";
import { cn } from "../../lib/cn";
import { NotFoundPage } from "../PlaceholderPages";
import { PageHeader } from "../PageHeader";
import { AccountPage } from "./AccountPage";
import { DataPage } from "./DataPage";
import { InvitesPage } from "./InvitesPage";
import { SecretsPage } from "./SecretsPage";
import { TokensPage } from "./TokensPage";
import { UsersPage } from "./UsersPage";

interface Section {
  to: string;
  label: string;
  adminOnly?: boolean;
  end?: boolean;
}

const SETTINGS_SECTIONS: readonly Section[] = [
  { to: "/settings", label: "Account", end: true },
  { to: "/settings/tokens", label: "API tokens" },
  { to: "/settings/secrets", label: "Secrets" },
  { to: "/settings/data", label: "Data" },
  { to: "/settings/invites", label: "Invites", adminOnly: true },
  { to: "/settings/users", label: "Users", adminOnly: true },
];

function SettingsNav({ isAdmin }: { isAdmin: boolean }) {
  return (
    <nav aria-label="Settings" className="mb-5 flex flex-wrap gap-1 border-b border-border">
      {SETTINGS_SECTIONS.filter((s) => isAdmin || s.adminOnly !== true).map((section) => (
        <NavLink
          key={section.to}
          to={section.to}
          end={section.end}
          className={({ isActive }) =>
            cn(
              "-mb-px flex h-9 items-center border-b-2 px-3 text-sm",
              isActive ? "border-accent font-medium text-fg" : "border-transparent text-fg-muted hover:text-fg",
            )
          }
        >
          {section.label}
        </NavLink>
      ))}
    </nav>
  );
}

export function SettingsRoutes() {
  const session = useSession();
  const isAdmin = session.data?.user.role === "admin";
  return (
    <>
      <PageHeader title="Settings" description="Your account, API tokens, secrets and data; invites and users for admins." />
      <SettingsNav isAdmin={isAdmin} />
      <Routes>
        <Route index element={<AccountPage />} />
        <Route path="tokens" element={<TokensPage isAdmin={isAdmin} />} />
        <Route path="secrets" element={<SecretsPage />} />
        <Route path="data" element={<DataPage />} />
        <Route path="invites" element={<InvitesPage isAdmin={isAdmin} />} />
        <Route path="users" element={<UsersPage isAdmin={isAdmin} />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </>
  );
}
