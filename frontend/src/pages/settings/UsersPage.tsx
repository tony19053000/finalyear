/**
 * Settings > Users (D176 "users (admin)"). The controller has NO REST API for
 * users in V0.8: they are managed on the controller host with
 * `meshforge-controller user ...` (V0.7 G4, D141), which works on the
 * database directly. The brief says "nothing the API lacks", so this page
 * shows no user list and no buttons - it says exactly where users are
 * managed. (Reported to the Lead as a G5 STOP item: a users endpoint would be
 * new backend work.)
 */
import { Link } from "react-router-dom";

import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { EmptyState } from "../../components/States";

export function UsersPage({ isAdmin }: { isAdmin: boolean }) {
  useDocumentTitle("Users");
  if (!isAdmin) {
    return <EmptyState title="Users are managed by admins">Ask a fabric admin to create or change an account.</EmptyState>;
  }
  return (
    <Card title="Users" data-testid="users-card">
      <p className="text-sm text-fg-muted">
        Users are managed on the controller&apos;s own machine: the controller offers no web API for creating, listing or
        disabling users yet, so the dashboard cannot do it. Disabling a user or resetting a password revokes all of that
        user&apos;s tokens and browser sessions at once.
      </p>
      <CodeBlock
        className="mt-3"
        label="user management commands"
        code={[
          "meshforge-controller user list",
          "meshforge-controller user create NAME [--admin] [--password-stdin]",
          "meshforge-controller user disable NAME",
          "meshforge-controller user enable NAME",
          "meshforge-controller user reset-password NAME",
        ].join("\n")}
      />
      <p className="mt-3 text-sm text-fg-muted">
        Every user&apos;s API tokens and sessions are listed (and can be revoked) under{" "}
        <Link className="text-accent underline underline-offset-2" to="/settings/tokens">
          API tokens
        </Link>{" "}
        with &ldquo;Every user&rdquo;.
      </p>
    </Card>
  );
}
