import { useInfiniteQuery } from "@tanstack/react-query";
import { useCallback, useId, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useNodes, useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { EmptyState, ErrorState } from "../../components/States";
import { JobsTable } from "../../components/jobs/JobsTable";
import { isoToLocalInput, localInputToIso } from "../../components/jobs/format";
import { PageHeader } from "../PageHeader";
import { jobKeys } from "./jobKeys";
import { filtersFromSearch, hasFilters, searchFromFilters, stateChoice, statesFromChoice } from "./jobFilters";
import { JOB_STATES, listJobs, type JobFilters } from "./jobsApi";

const FIELD =
  "h-8 rounded-md border border-border-strong bg-surface-sunken px-2 text-sm text-fg focus-visible:outline-2";

/** The jobs list (0.8.5): server keyset pages (D174), filters in the URL. */
export function JobsListPage() {
  useDocumentTitle("Jobs");
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => filtersFromSearch(params), [params]);
  const session = useSession();
  const isAdmin = session.data?.user.role === "admin";
  const nodes = useNodes();

  const query = useInfiniteQuery({
    queryKey: jobKeys.list(filters),
    queryFn: ({ pageParam, signal }) => listJobs(filters, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const jobs = useMemo(() => query.data?.pages.flatMap((p) => p.jobs) ?? [], [query.data]);

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  const loadMore = useCallback(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const update = (patch: Partial<JobFilters>) => {
    setParams(searchFromFilters({ ...filters, ...patch }), { replace: true });
  };
  const ids = { state: useId(), owner: useId(), node: useId(), after: useId(), before: useId() };

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Submitted, queued, running and finished work - newest first."
        actions={
          <Link
            to="/jobs/new"
            className="inline-flex h-8 items-center rounded-md border border-transparent bg-accent px-3 text-sm font-medium text-accent-fg hover:bg-accent-hover"
          >
            Submit job
          </Link>
        }
      />

      <form
        role="search"
        aria-label="Filter jobs"
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor={ids.state} className="text-2xs font-medium text-fg-muted uppercase">
            State
          </label>
          <select
            id={ids.state}
            className={FIELD}
            value={stateChoice(filters.states)}
            onChange={(event) => {
              update({ states: statesFromChoice(event.target.value) });
            }}
          >
            <option value="">Any state</option>
            <option value="active">Active (not finished)</option>
            <option value="finished">Finished</option>
            {JOB_STATES.map((state) => (
              <option key={state} value={state}>
                {state}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={ids.node} className="text-2xs font-medium text-fg-muted uppercase">
            Ran on node
          </label>
          <select
            id={ids.node}
            className={FIELD}
            value={filters.node}
            onChange={(event) => {
              update({ node: event.target.value });
            }}
          >
            <option value="">Any node</option>
            {filters.node !== "" && !nodes.data?.nodes.some((n) => n.node_id === filters.node) && (
              <option value={filters.node}>{filters.node}</option>
            )}
            {nodes.data?.nodes.map((node) => (
              <option key={node.node_id} value={node.node_id}>
                {node.hostname ? `${node.hostname} (${node.node_id})` : node.node_id}
              </option>
            ))}
          </select>
        </div>
        {isAdmin && (
          <div className="flex flex-col gap-1">
            <label htmlFor={ids.owner} className="text-2xs font-medium text-fg-muted uppercase">
              Owner (user id)
            </label>
            <input
              id={ids.owner}
              className={`${FIELD} w-48 font-mono`}
              placeholder="usr_…"
              value={filters.owner}
              onChange={(event) => {
                update({ owner: event.target.value.trim() });
              }}
            />
          </div>
        )}
        <div className="flex flex-col gap-1">
          <label htmlFor={ids.after} className="text-2xs font-medium text-fg-muted uppercase">
            Created after
          </label>
          <input
            id={ids.after}
            type="datetime-local"
            className={FIELD}
            value={isoToLocalInput(filters.createdAfter)}
            onChange={(event) => {
              update({ createdAfter: localInputToIso(event.target.value) });
            }}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={ids.before} className="text-2xs font-medium text-fg-muted uppercase">
            Created before
          </label>
          <input
            id={ids.before}
            type="datetime-local"
            className={FIELD}
            value={isoToLocalInput(filters.createdBefore)}
            onChange={(event) => {
              update({ createdBefore: localInputToIso(event.target.value) });
            }}
          />
        </div>
        {hasFilters(filters) && (
          <Button
            variant="ghost"
            onClick={() => {
              setParams(new URLSearchParams(), { replace: true });
            }}
          >
            Clear filters
          </Button>
        )}
      </form>

      {query.isPending ? (
        <div role="status" className="py-8 text-center text-sm text-fg-muted">
          Loading jobs…
        </div>
      ) : query.data === undefined ? (
        <ErrorState
          title="Could not load jobs"
          error={query.error}
          onRetry={() => {
            void query.refetch();
          }}
        />
      ) : jobs.length === 0 && !hasFilters(filters) ? (
        <EmptyState
          title="No jobs yet"
          action={
            <Link to="/jobs/new" className="text-accent underline-offset-2 hover:underline">
              Submit a project folder
            </Link>
          }
        >
          Submit a project from this page, or from a terminal with <code className="font-mono">compute run ./project</code>.
        </EmptyState>
      ) : (
        <>
          <JobsTable jobs={jobs} onNearEnd={loadMore} />
          <div className="mt-3 flex items-center justify-between gap-3 text-sm text-fg-muted">
            <span role="status" aria-live="polite">
              {jobs.length} job{jobs.length === 1 ? "" : "s"} loaded
              {hasNextPage ? "; more on the server" : jobs.length > 0 ? "; that is all" : ""}
            </span>
            {hasNextPage && (
              <Button onClick={loadMore} loading={isFetchingNextPage}>
                Load more
              </Button>
            )}
          </div>
          {query.isFetchNextPageError && (
            <ErrorState
              className="mt-3"
              title="Could not load the next page"
              error={query.error}
              onRetry={loadMore}
            />
          )}
        </>
      )}
    </>
  );
}
