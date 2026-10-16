/**
 * Section pages of the shell (0.8.2) not built yet: Jobs arrives in G4 (the
 * Overview and Nodes pages are pages/overview and pages/nodes, G3). Until
 * then each says what it will show and what works today - never invented
 * numbers (checklist 26).
 */
import { Link } from "react-router-dom";

import { CodeBlock } from "../components/CodeBlock";
import { EmptyState } from "../components/States";
import { useDocumentTitle } from "../app/useDocumentTitle";
import { PageHeader } from "./PageHeader";

export function JobsPage() {
  useDocumentTitle("Jobs");
  return (
    <>
      <PageHeader title="Jobs" description="Submitted, queued, running and finished work." />
      <EmptyState title="The jobs view is not built yet">
        <p>Submit and follow jobs from a terminal meanwhile:</p>
        <CodeBlock className="mt-3 text-left" label="Example commands" code={"compute run ./project\ncompute jobs\ncompute logs <job-id> -f"} />
      </EmptyState>
    </>
  );
}

export function NotFoundPage() {
  useDocumentTitle("Not found");
  return (
    <EmptyState title="No such page" action={<Link className="text-accent underline-offset-2 hover:underline" to="/">Go to the overview</Link>}>
      The address does not match any dashboard page.
    </EmptyState>
  );
}
