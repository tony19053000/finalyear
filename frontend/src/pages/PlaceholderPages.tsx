/** Fallback page of the shell: unknown routes (every section is a real page since V0.8 G4). */
import { Link } from "react-router-dom";

import { EmptyState } from "../components/States";
import { useDocumentTitle } from "../app/useDocumentTitle";

export function NotFoundPage() {
  useDocumentTitle("Not found");
  return (
    <EmptyState title="No such page" action={<Link className="text-accent underline-offset-2 hover:underline" to="/">Go to the overview</Link>}>
      The address does not match any dashboard page.
    </EmptyState>
  );
}
