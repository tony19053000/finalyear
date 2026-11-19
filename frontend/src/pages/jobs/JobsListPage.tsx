import { useInfiniteQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useNodes, useSession } from "../../api/hooks";
import { FilterIcon } from "../../app/icons";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { EmptyState, ErrorState } from "../../components/States";
import { JobsTable } from "../../components/jobs/JobsTable";
import { DateTimeField } from "../../components/jobs/DateTimeField";
import { ListboxField, type ListboxOption } from "../../components/jobs/ListboxField";
import { formatDayTime } from "../../components/jobs/format";
import { useServerNow } from "../../lib/serverClock";
import { jobKeys } from "./jobKeys";
import { filtersFromSearch, hasFilters, searchFromFilters } from "./jobFilters";
import { listJobs, type JobFilters } from "./jobsApi";
import { STATE_TABS, countByTab, nodeNames, tabOf } from "./jobView";

function useJobPages(filters: JobFilters) {
  return useInfiniteQuery({
    queryKey: jobKeys.list(filters),
    queryFn: ({ pageParam, signal }) => listJobs(filters, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

function Filter({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="nd-label">
        {label}
      </label>
      {children}
    </div>
  );
}

/** Open/close for the filter popover: outside click, focus leaving it, or Escape closes. */
function useDisclosure() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
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
  return { open, setOpen, root, trigger };
}

/**
 * The jobs list (0.8.5): server keyset pages (D174), filters in the URL.
 * Layout: the Nodes list's language - the big title with frame-B text tabs
 * by state centred on its row (every state group always shown, with its
 * count), then the frosted table. The other filters sit behind one round
 * button on the title row (a frosted popover); the active ones show as plain
 * removable text beside it.
 */
export function JobsListPage() {
  useDocumentTitle("Jobs");
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => filtersFromSearch(params), [params]);
  const session = useSession();
  const isAdmin = session.data?.user.role === "admin";
  const nodes = useNodes();
  const names = useMemo(() => nodeNames(nodes.data?.nodes ?? []), [nodes.data]);
  const now = useServerNow();

  const query = useJobPages(filters);
  const jobs = useMemo(() => query.data?.pages.flatMap((p) => p.jobs) ?? [], [query.data]);

  // The tab counts: the same filters without the state (the same query - and
  // request - when no state tab is picked).
  const allFilters = useMemo(() => ({ ...filters, states: [] }), [filters]);
  const all = useJobPages(allFilters);
  const allJobs = useMemo(() => all.data?.pages.flatMap((p) => p.jobs) ?? [], [all.data]);
  const counts = countByTab(allJobs);
  const activeTab = tabOf(filters.states);

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  const loadMore = useCallback(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const update = (patch: Partial<JobFilters>) => {
    setParams(searchFromFilters({ ...filters, ...patch }), { replace: true });
  };
  const ids = { owner: useId(), node: useId(), after: useId(), before: useId(), panel: useId() };
  const { open: filtersOpen, setOpen: setFiltersOpen, root: filtersRoot, trigger: filtersTrigger } = useDisclosure();

  const nodeOptions = useMemo<ListboxOption[]>(() => {
    const list = nodes.data?.nodes ?? [];
    const known = list.map((n) => ({ value: n.node_id, label: names.get(n.node_id) ?? n.node_id, detail: n.node_id }));
    const unknown = filters.node !== "" && !list.some((n) => n.node_id === filters.node) ? [{ value: filters.node, label: filters.node }] : [];
    return [{ value: "", label: "Any node" }, ...unknown, ...known];
  }, [nodes.data, names, filters.node]);

  // The filters the round button holds, as plain removable text beside it.
  const active: { key: string; text: string; clear: Partial<JobFilters> }[] = [];
  if (filters.states.length > 0 && activeTab === null) {
    active.push({ key: "state", text: `state ${filters.states.join(", ")}`, clear: { states: [] } });
  }
  if (filters.node !== "") active.push({ key: "node", text: `node ${names.get(filters.node) ?? filters.node}`, clear: { node: "" } });
  if (filters.owner !== "") active.push({ key: "owner", text: `owner ${filters.owner}`, clear: { owner: "" } });
  if (filters.createdAfter !== "") {
    active.push({ key: "after", text: `after ${formatDayTime(filters.createdAfter) || filters.createdAfter}`, clear: { createdAfter: "" } });
  }
  if (filters.createdBefore !== "") {
    active.push({ key: "before", text: `before ${formatDayTime(filters.createdBefore) || filters.createdBefore}`, clear: { createdBefore: "" } });
  }

  let content: ReactNode;
  if (query.isPending) {
    content = (
      <div role="status" className="py-8 text-center text-sm text-fg-muted">
        Loading jobs…
      </div>
    );
  } else if (query.data === undefined) {
    content = (
      <ErrorState
        title="Could not load jobs"
        error={query.error}
        onRetry={() => {
          void query.refetch();
        }}
      />
    );
  } else if (jobs.length === 0 && !hasFilters(filters)) {
    content = (
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
    );
  } else {
    content = (
      <>
        <section aria-label="Jobs" className="nl-plain jb-plain ov-table-fit jb-list overflow-hidden">
          <JobsTable jobs={jobs} onNearEnd={loadMore} names={names} now={now} />
        </section>
        {hasNextPage && (
          <div className="mt-5 flex items-center justify-between gap-3 px-2 text-sm text-fg-muted">
            <span role="status" aria-live="polite" className="font-mono text-[0.6875rem] tracking-[0.12em] uppercase">
              {jobs.length} job{jobs.length === 1 ? "" : "s"} loaded; more on the server
            </span>
            <button type="button" className="jb-quiet-btn" onClick={loadMore} disabled={isFetchingNextPage} aria-busy={isFetchingNextPage || undefined}>
              {isFetchingNextPage ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
        {query.isFetchNextPageError && (
          <ErrorState className="mt-3" title="Could not load the next page" error={query.error} onRetry={loadMore} />
        )}
      </>
    );
  }

  const more = all.hasNextPage ? "+" : "";
  return (
    <>
      <div className="mt-3 mb-16 grid grid-cols-[1fr_auto_1fr] items-end gap-x-8">
        <h1 className="font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">Jobs</h1>
        <div role="group" aria-label="Filter by state" className="flex items-end justify-center gap-10 pb-1.5">
          {STATE_TABS.map((t) => {
            const on = activeTab?.key === t.key;
            // the open tab counts its own (filtered) list; the others the unfiltered one
            const n = on && t.states.length > 0 ? jobs.length : (counts.get(t.key) ?? 0);
            const plus = on && t.states.length > 0 ? (hasNextPage ? "+" : "") : more;
            const count = `${String(n)}${plus}`;
            return (
              <button
                key={t.key}
                type="button"
                aria-pressed={on}
                aria-label={`${t.label} ${count}`}
                onClick={() => {
                  update({ states: [...t.states] });
                }}
                className={`relative pb-2 text-[0.9375rem] transition-colors duration-300 ${on ? "font-semibold text-fg" : "text-fg-muted hover:text-fg"}`}
              >
                {t.label}
                <span className="ml-1.5 font-mono text-[0.6875rem] text-fg-subtle tabular-nums">{count}</span>
                <span
                  aria-hidden="true"
                  className={`absolute bottom-0 left-0 h-0.5 rounded-full bg-fg transition-all duration-300 ${on ? "w-5 opacity-100" : "w-0 opacity-0"}`}
                />
              </button>
            );
          })}
        </div>
        <div className="flex min-w-0 items-center justify-end gap-5 pb-1">
          {active.length > 0 && (
            <ul aria-label="Active filters" className="flex min-w-0 flex-wrap justify-end gap-x-4 gap-y-1">
              {active.map((f) => (
                <li key={f.key}>
                  <button
                    type="button"
                    className="jb-active-filter"
                    aria-label={`Remove filter: ${f.text}`}
                    onClick={() => {
                      update(f.clear);
                    }}
                  >
                    {f.text}
                    <span aria-hidden="true" className="jb-active-x">
                      ×
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div ref={filtersRoot} className="relative shrink-0">
            <button
              ref={filtersTrigger}
              type="button"
              className="jb-filter-btn"
              aria-label={active.length > 0 ? `Filters, ${String(active.length)} active` : "Filters"}
              title="Filters"
              aria-expanded={filtersOpen}
              aria-controls={ids.panel}
              onClick={() => {
                setFiltersOpen((v) => !v);
              }}
            >
              <FilterIcon />
              {active.length > 0 && <span aria-hidden="true" className="jb-filter-dot" />}
            </button>
            {filtersOpen && (
              <form
                id={ids.panel}
                role="search"
                aria-label="Filter jobs"
                className="ov-card ov-frost jb-filter-pop"
                onSubmit={(event) => {
                  event.preventDefault();
                }}
              >
                <Filter id={ids.node} label="Ran on node">
                  <ListboxField
                    id={ids.node}
                    value={filters.node}
                    options={nodeOptions}
                    onChange={(node) => {
                      update({ node });
                    }}
                  />
                </Filter>
                {isAdmin && (
                  <Filter id={ids.owner} label="Owner (user id)">
                    <input
                      id={ids.owner}
                      className="jb-field w-full font-mono"
                      placeholder="usr_…"
                      value={filters.owner}
                      onChange={(event) => {
                        update({ owner: event.target.value.trim() });
                      }}
                    />
                  </Filter>
                )}
                <Filter id={ids.after} label="Created after">
                  <DateTimeField
                    id={ids.after}
                    value={filters.createdAfter}
                    onChange={(createdAfter) => {
                      update({ createdAfter });
                    }}
                  />
                </Filter>
                <Filter id={ids.before} label="Created before">
                  <DateTimeField
                    id={ids.before}
                    value={filters.createdBefore}
                    defaultTime="23:59"
                    onChange={(createdBefore) => {
                      update({ createdBefore });
                    }}
                  />
                </Filter>
                {hasFilters(filters) && (
                  <div className="flex justify-end">
                    <button
                      type="button"
                      className="jb-clear-btn"
                      onClick={() => {
                        setParams(new URLSearchParams(), { replace: true });
                      }}
                    >
                      Clear filters
                    </button>
                  </div>
                )}
              </form>
            )}
          </div>
          <Link to="/jobs/new" className="jb-submit-btn shrink-0" aria-label="Submit job" title="Submit job">
            <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </Link>
        </div>
      </div>

      {content}
    </>
  );
}
