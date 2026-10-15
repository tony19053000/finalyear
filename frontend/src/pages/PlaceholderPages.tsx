/**
 * Section pages of the shell (0.8.2). Their content arrives in G3 (Overview,
 * Nodes), G4 (Jobs) and G5 (Settings); until then each says what it will
 * show and what works today - never invented numbers (checklist 26).
 */
import { Link } from "react-router-dom";

import { CodeBlock } from "../components/CodeBlock";
import { EmptyState } from "../components/States";
import { useDocumentTitle } from "../app/useDocumentTitle";
import { PageHeader } from "./PageHeader";

export function OverviewPage() {
  useDocumentTitle("Overview");
  return (
    <>
      <PageHeader title="Overview" description="Fabric health, nodes, and the work running on them." />
      <EmptyState title="The fabric overview is not built yet">
        Health, utilisation, active and recent jobs will appear here. The header already shows how many nodes are
        online, live. From a terminal: <code className="font-mono">compute status</code>.
      </EmptyState>
    </>
  );
}

export function NodesPage() {
  useDocumentTitle("Nodes");
  return (
    <>
      <PageHeader title="Nodes" description="Every machine in the fabric: state, resources, capabilities." />
      <EmptyState title="The node list is not built yet">
        From a terminal: <code className="font-mono">compute nodes</code>.
      </EmptyState>
    </>
  );
}

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
