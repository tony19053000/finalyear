# Telemetry — run records and the dataset (V0.9, ADR-0018)

The planner learns from recorded runs (V0.14 planner v2, V0.15 ML planner). This page is the human copy of
**telemetry schema version 1** — the code is `src/meshforge/controller/telemetry/schema.py`, and a unit test
fails if this table, the `run_record` table, the export and the declaration drift apart.

## Two layers (D211)

- **Record** — PostgreSQL table `run_record` (migration 0021): one row per **terminal** attempt (`done`,
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
| `infrastructure` | the fabric failed it: node lost, fenced by a retry, transfer, environment build, disk, internal |
| `condition_failed` | a benchmark condition could not be applied or verified (V0.9 G4) |
| `unavailable` | could not run here (benchmark exit 3, e.g. no CUDA) or was cancelled — no measurement |

## Reproducibility (D222)

Same records → same bytes: rows sorted by (campaign_id, config_id, repetition, attempt_id) with nulls first,
columns in the order below, `json` columns as canonical JSON text, no export-time value in the file (its only
custom metadata is the schema version), Parquet writer options pinned (format 2.6, zstd level 3, no dictionary,
statistics on, 65 536-row groups) and one pyarrow version (pinned by `uv.lock`; a pyarrow bump is a dataset
format change).

**Memory.** An export is **buffered in memory**, not streamed: the matching records are loaded (~2.5 KB each)
and the finished file is returned whole (the API sends its `Content-Length`). The Arrow data is built one row
group at a time (`write_records`, a `ParquetWriter`), so it never holds more than 65 536 rows; the bytes are the
same as writing one table (a unit test exports across row-group boundaries and compares). The cap,
`[controller.telemetry] export_max_rows` (default **100 000**, was 1 000 000 in G1), keeps the worst case to a
few hundred MB; a bigger export is refused with 413 - narrow it by `--campaign` / `--since`.

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
never truncated) and scrubs every text value. A record PostgreSQL still refuses is skipped alone (the rest of
the sweep is written), counted, logged and raised as a `telemetry_record_skipped` alert (`GET /v1/alerts`); it
is retried after a controller restart. At most 1 000 skipped attempts are excluded by id; past that cap the controller raises
**one** more alert (reason `skip_cap_reached`) and then only counts and logs further skips, and the sweep scans
forward from a watermark so skipped attempts can never hold back newer ones (an older attempt that ends late, or
a skipped one, is looked at again on a full rescan once a minute). An attempt whose job row vanished mid-sweep is
skipped the same way (`error_type` `job_missing`) instead of failing the sweep.

## Columns — schema version 1

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
| `planner_node_id` | text | yes | Node the planner chose (auto jobs; null when pinned — G3 adds the would-have-chosen node). |
| `planner_estimate_s` | float64 | yes | The planner's estimate for the node used (s). |
| `workload_family` | text | yes | Benchmark family (results/workload.json); null = not a benchmark. |
| `workload_params` | json | yes | Benchmark parameters as the workload reported them. |
| `workload_seed` | int64 | yes | Benchmark seed. |
| `workload_status` | text | yes | ok / check_failed / unavailable / bad_params. |
| `workload_device` | text | yes | cpu / cuda as the workload reported it. |
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
