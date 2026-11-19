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
 *
 * Layout: deliberately not a dashboard page - one calm centred column open
 * on the page: the title, the sections as underlined text tabs over a
 * full-width hairline (links: each section keeps its own route), then the
 * section itself, which opens with a plain intro line and its own action.
 */
import { NavLink, Route, Routes } from "react-router-dom";

import { useSession } from "../../api/hooks";
import { cn } from "../../lib/cn";
import { NotFoundPage } from "../PlaceholderPages";
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

/** The sections as underlined text tabs on a full-width hairline. */
function SettingsNav({ isAdmin }: { isAdmin: boolean }) {
  return (
    <nav aria-label="Settings" className="st-tabs">
      {SETTINGS_SECTIONS.filter((s) => isAdmin || s.adminOnly !== true).map((section) => (
        <NavLink key={section.to} to={section.to} end={section.end} className={({ isActive }) => cn("st-tab", isActive && "st-tab-on")}>
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
    <div className="st-page">
      <h1 className="st-title">Settings</h1>
      <SettingsNav isAdmin={isAdmin} />
      <div className="nd-in">
        <Routes>
          <Route index element={<AccountPage />} />
          <Route path="tokens" element={<TokensPage isAdmin={isAdmin} />} />
          <Route path="secrets" element={<SecretsPage />} />
          <Route path="data" element={<DataPage />} />
          <Route path="invites" element={<InvitesPage isAdmin={isAdmin} />} />
          <Route path="users" element={<UsersPage isAdmin={isAdmin} />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </div>
    </div>
  );
}
