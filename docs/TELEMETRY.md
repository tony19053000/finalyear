# Telemetry — run records and the dataset (V0.9, ADR-0018)

The planner learns from recorded runs (V0.14 planner v2, V0.15 ML planner). This page is the human copy of
**telemetry schema version 2** (v1 + three columns, V0.9 G4) — the code is `src/meshforge/controller/telemetry/schema.py`, and a unit test
fails if this table, the `run_record` table, the export and the declaration drift apart.

## Two layers (D211)

- **Record** — PostgreSQL table `run_record` (migration 0021; v2 columns 0023): one row per **terminal** attempt (`done`,
  `failed`, `lost`, `fenced`) of **every** job, benchmark or not. Written once, never updated.
- **Dataset** — a Parquet file built from records by a pure function (`telemetry/export.py`):
  `compute telemetry export [--campaign ID] [--since TIME] -o FILE.parquet` or
  `GET /v1/telemetry/export` (admin only). Nothing writes Parquet while jobs run.

## How a record is written

A sweep (`telemetry/recorder.py`, every `[controller.telemetry] record_interval_s` = 2 s, and at once after an
attempt changes) finds terminal attempts without a record and inserts their records
(`ON CONFLICT (attempt_id) DO NOTHING`). It works **by state**, not by event: no terminal path (agent report,
node loss, restart recovery, backstop, cancel, retry fencing) can be missed, a controller crash just delays
the record to the next sweep, and a telemetry bug never rolls back a job transition.

Everything "at start" (node load, RAM, GPU, queue depth, cache warm/cold per tree, the network row to the node,
hardware scores, clock offset) is copied into the attempt's start snapshot **at dispatch**; the sweep never
reads the current state of those tables.

## Time rules (D213, checklist 28)

- Every duration is **monotonic and measured where the work runs**: `queue_ms` on the controller (its own
  clock), `stage/env/run/collect_ms` on the agent, `compute_ns` by the workload itself
  (`time.perf_counter_ns()` around the kernel only, written to `results/workload.json`). `compute_ns` lies
  inside `run_ms` and is not part of `total_ms`.
- `compute_ns` covers the kernel's **whole** timed loop (all `repeats` / `batches` / `steps`, D225/D228);
  `compute_iterations` says how many iterations that is (1 for a one-pass family such as `cpu.sha256`), so
  one iteration takes `compute_ns / compute_iterations`. The agent accepts it only as a strict integer in
  `1..100000` (the largest loop parameter of any family) and only when `compute_ns` is present; otherwise it
  is null.
- Timestamps (`attempt_created_at`, `attempt_ended_at`, `recorded_at`) are the controller's clock and serve
  ordering only. No column is a difference of two hosts' clocks.
- `clock_offset_ms ± clock_offset_bound_ms` is the node's wall clock minus the controller's, estimated from
  Ping/Pong round trips over a recent window (last 32 exchanges, ≤ 300 s), widened by the agent's clock
  resolution. A bound > 250 ms or contradicting exchanges raise a `clock_uncertain` alert.

## Failure class (D212, checklist 29)

| class | meaning |
|---|---|
| `ok` | the attempt finished (`done`) |
| `workload` | the workload failed: crash / non-zero exit, OOM, its own time limit, benchmark self-check failed (exit 4) or bad parameters (exit 2) |
| `infrastructure` | the fabric failed it: node lost, fenced by a retry, transfer, environment build, disk, internal. Also `failure_type = owner_preempted` (V0.9 G5: the owner guard stopped a benchmark attempt; no schema bump) - the campaign runner requeues those and the campaign's infrastructure failure rate (criterion 1, `compute bench status`) **excludes** them; filter with `failure_type IS DISTINCT FROM 'owner_preempted'` |
| `condition_failed` | a benchmark condition could not be applied or verified (V0.9 G4) |
| `unavailable` | could not run here (benchmark exit 3, e.g. no CUDA) or was cancelled — no measurement |

## Reproducibility (D222)

Same records → same bytes: rows sorted by (campaign_id, config_id, repetition, attempt_id) with nulls first,
columns in the order below, `json` columns as canonical JSON text, no export-time value in the file (its only
custom metadata is the schema version), Parquet writer options pinned (format 2.6, zstd level 3, no dictionary,
statistics on, 65 536-row groups) and one pyarrow version (pinned by `uv.lock`; a pyarrow bump is a dataset
format change).

**Memory.** The matching records are **loaded** (~2.5 KB each), then the file is **streamed** (V0.9 G4
item 0.4): `iter_parquet` builds Arrow data and encodes Parquet one row group (65 536 rows) at a time and the
API sends each row group's bytes as soon as they are ready, chunked - **no `Content-Length`**, and neither the
whole Arrow table nor the whole file is ever held. The pieces joined are the same bytes as writing one table
(unit tests export across row-group boundaries and compare). The export order is an in-place sort of the
loaded list (no copy). The cap, `[controller.telemetry] export_max_rows` (default **100 000**, was 1 000 000
in G1), keeps the loaded records to a few hundred MB; a bigger export is refused with 413 - narrow it by
`--campaign` / `--since`. The size cap and a database failure are answered before the 200; a failure while
encoding afterwards aborts the response, so the client gets a truncated body (no Parquet footer), never a
file that looks complete.

## Privacy and secrets

The export is admin-only: it names every user's jobs and nodes. It holds no secret value: no column copies job
secrets, and the agent drops any `results/workload.json` field in which it finds one of the job's secret values
(THREAT_MODEL RE-I6). `params` is matched on its decoded content (every key and string value, so a secret
with non-ASCII characters, `"` or `\` is found too) as well as its JSON text. A secret split across two fields
is not detected (residual, RE-I6).

## Untrusted workload values

`results/workload.json` is written by the workload. The agent bounds every field and drops `params` that hold
a NUL or a lone surrogate (PostgreSQL refuses both); the controller refuses such a report, and the record
builder bounds the flat columns (`p_dtype`, `workload_family`, ... to 64 characters - a longer value is null,
never truncated) and scrubs every text value. `workload_params` keeps **only the keys the family's own params model
defines** (`meshforge.bench.workloads`); any other key the report carries is dropped, and a family the controller
does not know keeps no params at all (null). Only **scalar** values are kept (text, number, bool, null) - a list
or object value is dropped - and a non-finite float (NaN, ±inf) becomes null (V0.9 G4 item 0.9; JSONB and the
canonical export JSON refuse them) - so a value smuggled into the report under a new key never reaches
the dataset, whatever the agent let through. A record PostgreSQL still refuses is skipped alone (the rest of
the sweep is written), counted, logged and raised as a `telemetry_record_skipped` alert (`GET /v1/alerts`); it
is retried after a controller restart. At most 1 000 skipped attempts are excluded by id; past that cap the controller raises
**one** more alert (reason `skip_cap_reached`) and then only counts and logs further skips, and the sweep scans
forward from a watermark so skipped attempts can never hold back newer ones (an older attempt that ends late, or
a skipped one, is looked at again once a forward pass has reached the end, at most once a minute). An attempt whose job row vanished mid-sweep is
skipped the same way (`error_type` `job_missing`) instead of failing the sweep.

## Versions

| version | what changed | migration |
|---|---|---|
| 1 | first published shape (V0.9 G1) | 0021 |
| 2 | adds `result_schema_version`, `condition`, `condition_measured` (V0.9 G4, D233.3/D241) | 0023 |

A row keeps the version it was written under: v1 rows (written before 0023) have `schema_version` 1 and null
in the three v2 columns; the file metadata names the version the export was written with (2). Read each
row by its own `schema_version`.

**Conditions (v2).** `condition` is the run's benchmark condition as its canonical string (`none`,
`cpu_load:2`, `queue:4`, `netem:delay=50ms,loss=1%` ..., at most 128 characters; one condition per run, D241)
and `condition_measured` what was measured when it was applied and verified (JSON object). Both are null for
a job that is not a campaign run (an ordinary job; a queue filler / cache warm-up is tagged, below). One writer fills both:
`controller/telemetry/conditions.py` `condition_outcome`, from the campaign identity the recorder looks up
per job (`condition`, `session_label`, controller-side `condition_facts`) and the agent's `ConditionReport`.
A stored condition that is blank or not a canonical `name[:arg]` (it does not round-trip through the
grammar in `common/bench_conditions.py`) is stored as **null** and the run is `condition_failed`. In
`condition_measured` a non-finite float (NaN, +-inf - also inside what the agent reported) becomes null;
a value PostgreSQL would refuse (NUL, lone surrogate, nesting deeper than 32) makes the whole column null -
the record itself is always kept.

`condition_measured` keys (D243):

| Key | Always? | Meaning |
|---|---|---|
| `verified` | yes | bool - the condition was applied and verified. `false` with `failure_class = condition_failed` when it should have held; `false` with another class when the attempt failed for its own reason first. |
| `reason` | yes | text or null - why it is not verified (agent's or controller's reason). |
| `session_label` | yes | the campaign's `session_label` (e.g. `wifi`, `ethernet`) or null. Kept here, not as a column (D243): filter with `condition_measured->>'session_label'`. |
| `agent` | agent conditions with a report carrying measurements | what the agent measured. `cpu_load`: `cores`, `load_cores`, `window_s`. `gpu_load`: `percent`, `gpu_index`, `gpu_util_percent`, `samples`. `netem`: `interface`, `qdisc`, `rtt_before_ms`, `rtt_after_ms`, `rtt_added_ms` (delay only). `cache`: `mode`, `trees`, `blobs_hit`, `bytes_hit`, `blobs_fetched`, `bytes_fetched`, `manifest_hits`; `cache:cold` also `evicted_blobs`, `evicted_bytes`, `kept_blobs`. |
| `reverted` | agent conditions with a matching report | bool - the agent undid the change after the run. |
| `controller` | controller-side conditions with facts | `queue`: `fillers`, `fillers_ahead`. `cache:warm`: `warmup_state`. `unavailable`: `paused_node_id`, `seconds`, `dispatched_after_s`, `pause_lost`. |

`none` runs carry only `verified` (`true`), `reason` (null) and `session_label`.

A campaign's **queue filler** / **cache warm-up** job (no campaign identity) has `condition` null and
`condition_measured = {"bench_role": "filler" | "warmup", "run_id": "<bench run>"}` (and none of the keys above).
Labels and planner-training queries must exclude rows where `condition_measured->>'bench_role'` is set.

## Columns — schema version 2

Types: `text`, `int64`, `float64`, `bool`, `json` (JSONB / canonical JSON text in Parquet), `timestamp`
(UTC, µs). "null" = the value was not measured or does not apply — never an invented 0.

| column | type | null | meaning |
|---|---|---|---|
| `schema_version` | int64 | no | Telemetry schema version of this row. |
| `attempt_id` | text | no | The attempt (primary key). |
| `job_id` | text | no | The job the attempt belongs to. |
| `attempt_no` | int64 | no | The attempt's fencing token (1 = first). |
| `campaign_id` | text | yes | Benchmark campaign (V0.9 G3); null = not a benchmark. |
| `group_id` | text | yes | Label group: same workload + params + conditions (D218). |
| `config_id` | text | yes | Campaign configuration (group x strategy). |
| `repetition` | int64 | yes | Repetition number within the configuration. |
| `fingerprint` | text | yes | D108 workload fingerprint (64 hex). |
| `node_id` | text | no | The node that ran the attempt. |
| `submitter_node_id` | text | yes | The submitting laptop's own node, if known. |
| `strategy` | text | no | local = ran on the submitter's node; mode1 = elsewhere. |
| `placement_mode` | text | no | pinned (forced node) or auto (planner chose). |
| `policy` | text | yes | Placement policy the planner used. |
| `planner_node_id` | text | yes | Auto jobs: the node the planner chose. Pinned jobs (V0.9 G3 shadow plan, D215/D235, `plan_decision.shadow_chosen_node_id`): the node the planner **prefers** - the same request unpinned with slot occupancy ignored (every slot of a healthy node counted free) but feasibility, health and retry exclusions kept; a *preference* label, not "what would have run that second". Only benchmark (campaign) jobs get the shadow plan (G4 item 0.8): a user's pinned job has null. Null also = no node feasible, or a decision from before G3. |
| `planner_estimate_s` | float64 | yes | The planner's estimate for the node used (s). |
| `workload_family` | text | yes | Benchmark family (results/workload.json); null = not a benchmark. |
| `workload_params` | json | yes | Benchmark parameters as the workload reported them. |
| `workload_seed` | int64 | yes | Benchmark seed. |
| `workload_status` | text | yes | ok / check_failed / unavailable / bad_params. |
| `workload_device` | text | yes | cpu / cuda as the workload reported it. |
| `result_schema_version` | int64 | yes | schema_version of the workload's results/workload.json (v2). |
| `p_n` | int64 | yes | Flattened parameter n. |
| `p_repeats` | int64 | yes | Flattened parameter repeats (compute_ns covers all repeats). |
| `p_size_mb` | int64 | yes | Flattened parameter size_mb. |
| `p_size` | int64 | yes | Flattened parameter size. |
| `p_batch` | int64 | yes | Flattened parameter batch. |
| `p_width` | int64 | yes | Flattened parameter width. |
| `p_depth` | int64 | yes | Flattened parameter depth. |
| `p_seq_len` | int64 | yes | Flattened parameter seq_len. |
| `p_dtype` | text | yes | Flattened parameter dtype. |
| `env_base` | text | yes | Manifest env.base (python-cpu / cuda-pytorch). |
| `gpu_request` | text | yes | Manifest resources.gpu (none / preferred / required). |
| `runtime` | text | yes | docker / venv / subprocess as the agent reported it. |
| `condition` | text | yes | The run's benchmark condition, canonical name[:arg] (none = baseline); null = not a benchmark / older. |
| `condition_measured` | json | yes | What was measured when the condition was applied and verified. |
| `node_state` | text | yes | Node liveness state at dispatch. |
| `node_rtt_ms` | float64 | yes | Controller <-> node RTT EWMA at dispatch (ms). |
| `node_cpu_percent` | float64 | yes | Node CPU utilisation at dispatch (%). |
| `node_ram_free_bytes` | int64 | yes | Node free RAM at dispatch. |
| `node_ram_total_bytes` | int64 | yes | Node total RAM. |
| `node_gpu_util_percent` | float64 | yes | Utilisation of the GPU given to the job (%). |
| `node_gpu_vram_free_bytes` | int64 | yes | Free VRAM of the GPU given to the job. |
| `node_sample_age_s` | float64 | yes | Age of the resource sample used (s). |
| `node_live_attempts` | int64 | yes | Attempts already live on the node (its queue depth). |
| `node_cpu_score` | float64 | yes | Node CPU score (GFLOP/s, all cores). |
| `node_gpu_score` | float64 | yes | Node best GPU score (TFLOP/s fp32). |
| `cache_inputs` | int64 | yes | Trees the job needed (project + named inputs). |
| `cache_warm_inputs` | int64 | yes | Of those, trees the node held whole at dispatch. |
| `cache_state` | text | yes | warm / partial / cold. |
| `cache_detail` | json | yes | Per tree: id, bytes, warm. |
| `net_source` | text | yes | Whose link the network row is: peer (submitter) or controller. |
| `net_rtt_ms` | float64 | yes | Network row to the chosen node: RTT (ms). |
| `net_bw_bytes_s` | float64 | yes | Network row to the chosen node: bandwidth (B/s). |
| `net_samples` | int64 | yes | Samples behind that network row. |
| `queue_ms` | int64 | yes | QUEUED -> PREPARING on the controller; null = skipped. |
| `stage_ms` | int64 | yes | Agent: fetch + verify + unpack; null = skipped. |
| `env_ms` | int64 | yes | Agent: environment build/resolve; null = skipped. |
| `run_ms` | int64 | yes | Agent: workload start to exit; null = skipped. |
| `compute_ns` | int64 | yes | The workload's own kernel time, its whole timed loop (benchmarks); null = none. |
| `compute_iterations` | int64 | yes | Iterations compute_ns covers (one = compute_ns / this); null = not run. |
| `collect_ms` | int64 | yes | Agent: exit to terminal report; null = skipped. |
| `total_ms` | int64 | yes | Sum of the measured phases above (compute excluded). |
| `bytes_in` | int64 | yes | Bytes the node fetched to stage the attempt. |
| `bytes_in_cached` | int64 | yes | Bytes it already held (cache hits). |
| `bytes_out` | int64 | yes | Bytes uploaded: outputs + final logs (DONE only). |
| `attempt_state` | text | no | done / failed / lost / fenced. |
| `exit_code` | int64 | yes | The workload's exit code. |
| `failure_type` | text | yes | Typed failure (ARCHITECTURE section 13). |
| `failure_class` | text | no | ok / workload / infrastructure / condition_failed / unavailable. |
| `clock_offset_ms` | float64 | yes | Node wall clock minus controller's at dispatch (ms). |
| `clock_offset_bound_ms` | float64 | yes | The true offset is within +- this (ms). |
| `clock_consistent` | bool | yes | False: the offset exchanges contradicted each other. |
| `attempt_created_at` | timestamp | no | When the attempt was created. |
| `attempt_ended_at` | timestamp | yes | When the attempt ended (controller clock). |
| `recorded_at` | timestamp | no | When this record was written. |

## Changing the schema

Bump `SCHEMA_VERSION`, add the new column fingerprint to `SCHEMA_FINGERPRINTS` (never edit a published one),
write the migration, update this table. Later versions add workload families and strategies (V0.10+), not
new schemas, unless a column truly must change.

**Two version numbers, one rule.** The telemetry schema version (this table) and the benchmark **result**
version (`schema_version` inside `results/workload.json`, `runner.RESULT_SCHEMA_VERSION`, docs/BENCHMARKS.md)
are separate. Both were frozen at 1 when G1 started recording; from then on **any shape change bumps its
version** - no in-place edits, because a recorded dataset may already hold the old shape (D228). The agent
forwards the result version it read (`result_schema_version` in the attempt's workload report); a result
change that alters a column here bumps both.

## Labels (V0.9 G5, roadmap 0.9.6, D218/D246)

A **label** says, for one **group** of a campaign (same workload family + parameters + condition,
`group_id`), which **configuration** (strategy + node + origin, `config_id`) was cheapest - **only when the
measurements can tell**. A noisy "best" would teach the ML planner (V0.15) noise, so a close call is a `tie`,
never a coin flip. Labels are **derived on demand** from the run records: no table, no migration, nothing
stored; the same records give the same labels and the same Parquet bytes (D222).

`GET /v1/bench/campaigns/{id}/labels[?format=json|parquet]` (admin) and
`compute bench labels CAMPAIGN_ID [-o FILE.parquet] [--json]`. Code: `controller/telemetry/labels.py` (the pure
rule), `labels_store.py` (the row filter, campaign reads), `labels_export.py` (Parquet), `bench/stats.py`
(statistics).

**The row filter** (`labels_store.label_rows` / `label_filter` - the one filter, also for planner training
later). A record counts only if: `campaign_id` is the campaign; `failure_class = 'ok'`; `workload_status = 'ok'`
(the self-check passed - a wrong answer computed fast is not fast); `condition_measured->>'bench_role'` is
**null** (D244: queue fillers and cache warm-ups are scaffolding for another run and never count); and
`condition_measured->>'verified'` is not `false`. Workload failures, infrastructure failures,
`condition_failed` and `unavailable` runs are therefore all excluded. Repetitions are the distinct
`repetition` values: when a (config, repetition) has several ok records (a retried run, D234) only the
**latest** attempt is kept (created last, then highest attempt number, then highest attempt id).

**Costs per configuration**, each with n, median, mean, sample std, CV = std / |mean| (null when n < 2 or mean = 0), the
median's CI and the MAD outlier count (|x - median| > 3.5 x 1.4826 x MAD; MAD = 0 -> none):

| metric | from the record |
|---|---|
| `total_ms` | `total_ms` (sum of measured phases; the label is decided on this) |
| `compute_per_iter_ns` | `compute_ns / compute_iterations` - one kernel iteration (null when either is null) |
| `bytes` | `bytes_in + bytes_out` (null when either is null) |

**The median's confidence interval** is distribution-free (no normality assumption - timings are skewed, and
3-10 repetitions cannot show a shape): the number of values below the true median is Binomial(n, 1/2), so the
interval from the k-th smallest to the k-th largest value covers it with probability 1 - 2 P(B <= k - 1).
n <= 5: [min, max] (coverage 0 %, 50 %, 75 %, 87.5 %, 93.75 % for n = 1..5). n >= 6: the **tightest** such
interval (largest k) whose coverage is still >= 90 %. The achieved coverage is reported, never assumed.

**The label** (the first rule that applies):

| label | when |
|---|---|
| `pending` | a run of the group is `planned` / `submitted`, or ended but its record is not written yet |
| `insufficient` | a configuration has fewer than 3 measured repetitions (a planned configuration with no record has 0), or no configuration can run the group |
| `best` | the lowest-median configuration's CI upper bound is **strictly below every other configuration's CI lower bound**. `margin_pct` = how much higher the runner-up's median is, in % of the best's. A group with one configuration is `best` with `margin_pct` null. |
| `tie` | otherwise; `tied_config_ids` = the lowest-median configuration and every configuration whose CI reaches into its CI |

Why `tie` instead of "lowest median wins": with 3-5 repetitions two configurations within each other's noise
swap places from one night to the next; labelling either as best would be noise the planner learns.

A configuration whose runs were all stored `skipped` as **unsupported** (`on_unsupported: skip`: the node
cannot apply the condition) is reported with `state: unsupported` and left out of the comparison; any other
skip (node gone, cancelled) leaves a planned configuration without data -> `insufficient`.

**Planner signal** (D237: a preference, not "what would have run that second"): each configuration carries the
most frequent `planner_node_id` of its kept records (ties -> the smallest id), the group the most frequent
over all of them, and `planner_agrees` = (best node == that preference) for a `best` label (always true-ish for a single-config group: V0.15 should weight it accordingly) - the regret
signal for V0.14.

**Parquet (`labels_version` 1).** One row per (group, configuration), sorted by (`group_id`, `config_id`): the
group's columns (`labels_version`, `campaign_id`, `group_id`, `family`, `condition`, `params` (json), `label`,
`best_config_id`, `best_strategy`, `best_node_id`, `margin_pct`, `tied_config_ids` (json list),
`group_planner_node_id`, `planner_agrees`, `reason`), then the configuration's (`config_id`, `strategy`,
`node_id`, `origin_node_id`, `config_state`, `repetitions`, `planner_node_id`, `is_best`, `in_tie`) and its
costs (`<metric>_n`, `_median`, `_mean`, `_std`, `_cv`, `_ci_low`, `_ci_high`, `_ci_coverage`, `_outliers`
per metric above). Same writer options and determinism rules as the record export; the only custom file
metadata is `meshforge.labels.version`. The columns are pinned by a fingerprint
(`labels_export.LABEL_FINGERPRINTS`); a shape change bumps `labels_version`. Streamed like the record export
(chunked, no `Content-Length`); more matching records (retries included, counted before the latest-per-repetition rule) than `[controller.telemetry] export_max_rows` -> 413. The labels table (one row per config) is built in memory before streaming, bounded by the same cap.
