# `compute` - command-line reference (V0.8)

`compute` is the fabric's command-line tool. It talks only to the controller's REST API
(`https://HOST:47110/v1`), the same API the dashboard uses, so everything here works from any laptop with a
plain install (`uv sync`). Setup and login: [OPERATIONS.md](OPERATIONS.md) ("Users and logging in").

This page is the contract for **scripts**: what goes to stdout and stderr, the `--json` documents, the exit
codes and shell completion. Unit tests keep it in step with the code (`tests/unit/cli/test_v08_g6_*.py`).

## 1. Commands

| Command | Does | `--json` |
|---|---|---|
| `compute status` | where the controller is, whether it answers, node counts | yes |
| `compute nodes [NODE_ID] [--watch]` | the node table, or one node's detail and hardware scores | yes |
| `compute jobs [JOB_ID] [--state S] [--limit N]` | recent jobs, or one job (attempts, inputs, transfers) | yes |
| `compute explain JOB_ID [--attempt N]` | why the planner placed the job where it did | yes |
| `compute watch JOB_ID [--interval S]` | a split job's progress until it ends: a bar (items, chunks, failed tries, ETA, elapsed vs predicted) and one line per node (state, chunks / items committed, measured items/s, chunks held now); exits with the job's outcome like `run -f` (V0.10 G7); `--json` prints the progress once | yes |
| `compute network` | measured bandwidth/RTT between nodes | yes |
| `compute whoami` | who you are logged in as | yes |
| `compute tokens list [--all]` | your API tokens and browser sessions | yes |
| `compute secret list` | your secrets' names (never values) | yes |
| `compute data ls` / `compute data show NAME` | named inputs (datasets, models) | yes |
| `compute fabric invites` | join-code invites (codes are never shown again) | yes |
| `compute bench workloads` | the benchmark workload families and their parameters (local: no controller call) | yes |
| `compute bench plan SPEC [--node ID] [--show N]` | expand a campaign spec ([BENCHMARKS.md](BENCHMARKS.md)) against the live nodes (or the `--node` ids, offline): run count, estimated hours, first runs - runs and stores nothing | yes |
| `compute bench create SPEC` | store a campaign on the controller, admin only (prints its `camp_...` id) | yes |
| `compute bench campaigns [CAMPAIGN_ID]` | benchmark campaigns with run counts per state, or one with its spec (admin) | yes |
| `compute bench runs CAMPAIGN_ID [--state S]` | a campaign's runs in execution order (admin) | yes |
| `compute bench labels CAMPAIGN_ID [-o FILE]` | per-group labels of a campaign: best configuration, `tie`, `insufficient` or `pending`, with the margin ([TELEMETRY.md](TELEMETRY.md) "Labels"; admin); `-o` downloads them as Parquet, never overwriting FILE | yes |
| `compute bench report CAMPAIGN_ID [--strict]` | the campaign's data quality report: runs by state, infrastructure failure rate, pre-emptions, condition / workload failures, ok records the label filter rejected (by reason), other failures - every record in exactly one bucket - criterion verdicts, then per configuration repetitions, CV, CI, outliers and missing fields, flagged ones first ([TELEMETRY.md](TELEMETRY.md) "Quality report"; admin); exit 0, `--strict` exits 1 unless both criteria are met (pending = not met) | yes |
| `compute bench submit CAMPAIGN_ID [-n K] [--wait [--timeout S]]` | submit the next K planned runs as pinned lowest-priority jobs (admin; idempotent per run); `--wait` exits 126 if one failed; refused once the campaign is started | yes |
| `compute bench start CAMPAIGN_ID [--per-node N] [--cooldown S] [--retries R]` | hand the campaign to the controller's runner (V0.9 G5; admin; idempotent) | yes |
| `compute bench pause CAMPAIGN_ID` | stop submitting the campaign's runs; running jobs finish (admin) | yes |
| `compute bench resume CAMPAIGN_ID` | resume a paused campaign (admin) | yes |
| `compute bench cancel CAMPAIGN_ID` | cancel its benchmark jobs and skip the open runs (admin) | yes |
| `compute bench status CAMPAIGN_ID` | counts, done %, ETA, infrastructure failure rate (owner pre-emptions excluded) | yes |
| `compute telemetry export -o FILE [--campaign ID] [--since TIME]` | the telemetry dataset (run records) as one Parquet file, admin only; never overwrites FILE ([TELEMETRY.md](TELEMETRY.md)) | yes |
| `compute planner calibrate` | fit a new planner calibration version from the recorded runs, gate it against the active one on the newest 20 % of runs (it goes live only if it is at most 5 % worse) and print it with its per-term factors, or why nothing was written (V0.14; admin; [OPERATIONS.md](OPERATIONS.md) "Planner calibration") | yes |
| `compute planner calibration [--version N]` | one calibration version's factors (default: the active one): per term and key (node + workload family, node, global) the factor, its run count `n` and the raw median ratio, plus what the planner uses now (`auto` / `off` / pinned) | yes |
| `compute planner eval --campaign ID [--campaign ID ...] [--holdout FAMILY] [--allow-leak] [--md PATH] [--timeout S]` | evaluate benchmark campaigns: every policy re-planned on what the planner saw for each labelled group, its regret vs the best measured configuration (mean, bootstrap 95 % CI, share within 15 %), the criterion 1 / 2 verdicts, the decision latency of the campaigns' dispatched decisions and the criterion 3 verdict (G6); calibrated rows use leave-one-family-out refits (`--allow-leak`: the live calibration, every verdict `leaked`); stores the report; `--md` also writes its Markdown and never overwrites PATH (V0.14; admin; [OPERATIONS.md](OPERATIONS.md) "Evaluating the planner"). V0.15 G6b (D378): the controller evaluates in the background - the CLI starts it (`POST`, 202) and polls the report every 2 s for up to `--timeout` seconds (default 3 600; giving up exits 1, the evaluation keeps running); a report that ends `failed` prints its reason and exits 1. Admins can also start one from the dashboard's Planner page (Run evaluation) | yes |
| `compute planner reports [--limit N]` | the stored planner evaluation reports, newest first: id, time, campaigns, held-out family, leaked, evaluated groups, criterion 1, 2 and 3 verdicts (V0.14). Also in the dashboard: Planner page (`/planner`), Evaluation | yes |
| `compute planner ml dataset [--out FILE] [--summary]` | the ML training dataset (V0.15; admin; [OPERATIONS.md](OPERATIONS.md) "ML training dataset"): one row per matched (decision, outcome) run and learnable term, features from the decision's replay snapshot, target `ln(actual / raw)`. Without `--out`: its summary - rows per term and family, skipped decisions/terms by reason, digest, whether `ml_dataset_max_runs` cut older runs. `--out FILE`: the Parquet file (a new file; its digest checked) and `FILE.json`, the sidecar (written only when the summary names the same digest). CLI-only in G1 | yes |
| `compute planner ml compare --dataset FILE [--terms a,b] [--algorithms a,b] [--with-family] [--min-rows N] [--min-families N] [--min-effect X] [--out DIR] [--timeout S]` | offline model comparison (V0.15 G2; no controller needed, the `controller` + `ml` extras on this machine - exit 2 with the install hint without them, or for a dataset version this code does not know; [OPERATIONS.md](OPERATIONS.md) "Comparing ML models"): per term, ridge / kNN / tree / forest / LightGBM / XGBoost against the raw analytical and calibration baselines, evaluated leave-one-family-out with the model chosen on the other families only; prints term, rows, families, status (`trained` / `no_learned_model` / `insufficient_data`), the selected model, held-out log error of model and calibration, the paired-bootstrap verdict vs calibration, conformal coverage and the usable share (in distribution, interval narrow enough); `--out DIR` keeps `report.json` (byte-identical for the same dataset) and `report.md`. `--timeout S` (default 3600; V0.15 G3) kills a comparison that overruns (exit 1). CLI-only | yes |
| `compute planner ml models [--limit N] [--set VERSION]` | the ML model registry (V0.15 G3; [OPERATIONS.md](OPERATIONS.md) "ML model registry and retraining"): what the planner may use now (`ml` mode, the set in use, why a serving set is not loaded), the retrain task (unavailable without the `ml` extra / running / last result) and the newest model sets - version, status (`active` / `rejected` / `superseded` / `pinned`), learned terms, reason. `--set` shows one set's members (algorithm, rows, inner log error, interval width, why a term has no model) and its gate result (rules failed, reference, `regret pending_g4`). CLI-only until G5 | yes |
| `compute planner ml train [--wait] [--timeout S]` | admin: retrain the model set now on the controller (a child process: dataset, model selection per term, activation gate). Without `--wait`: started / already running. `--wait`: polls until it ends and prints the new set `active` or `rejected` with the gate's reason, or why nothing was written (`no_new_runs`, `insufficient_data`, `same_set`, ...); exit 1 when it failed (timeout, child failure) or the controller has no `ml` extra (409). CLI-only until G5 | yes |
| `compute planner ml pin VERSION` / `compute planner ml unpin VERSION` | admin: pin a model set (the planner uses it instead of the active one - the rollback) / end the pin. A rejected set or one whose artefacts the GC removed cannot be pinned (exit 1). A malformed VERSION is exit 2 before any request. CLI-only until G5 | yes |
| `compute logs JOB_ID [-f] [--stream S]` | the job's own output, byte for byte | no - it is the workload's bytes |
| `compute run [PATH] [--node ID / --local] [-f] ...` | submit a project (prints the job id); with `-f` a `split:` job shows `compute watch`'s progress on stderr instead of its logs (`--no-progress`: the logs, for scripts) | no - stdout is the job id |
| `compute run [PATH] --dry-run` | what would be submitted: job, project size, a `split:` job's items (count, first/last, chunking); uploads and submits nothing | no |
| `compute cancel JOB_ID` | cancel a job | no |
| `compute get JOB_ID [--dest DIR] [--force]` | download and unpack a job's outputs | no |
| `compute login` / `logout` | log in (API token kept privately) / revoke it | no |
| `compute tokens revoke ID` | revoke a token | no |
| `compute secret set NAME` / `delete NAME` | store (value from stdin or `--from-file`) / delete | no |
| `compute data push NAME DIR` / `pin REF` / `rm REF` | register / pin / remove a named input | no |
| `compute node prepare|gc [NODE_ID / --all]` | pre-pull images and envs / run a cache GC pass | no |
| `compute node revoke NODE_ID` | revoke a node for good | yes |
| `compute fabric invite [--ttl] [--replace NODE_ID]` | make a single-use join code | yes |
| `compute fabric invites --revoke ID` | revoke an invite | yes |
| `compute fabric join [CODE]` | join this laptop's agent (runs `meshforge-agent join`) | no |

Every command has `--help`. Global options go before the command: `compute --ca ca.crt --token-file F nodes`.

## 2. Output rules

- **stdout carries the result** (a table, the job id, the JSON document); **stderr carries progress, advice and
  errors**, each line starting `compute: `. `compute run ... > id.txt` therefore captures exactly the job id.
- Tables use plain ASCII borders, so they look the same in Windows Terminal, `cmd.exe`, a pipe and a log file.
- **Colour**: none when stdout is not a terminal, none when `NO_COLOR` is set to any non-empty value
  (<https://no-color.org>). Nothing depends on colour: every state is written out in words.
- Text that came from the network (host names, error messages, job names) has its control characters replaced by
  spaces before printing, so it cannot move the cursor or fake a line.
- Error lines never contain a token, join code or private key: they are scrubbed like the logs.

## 3. `--json`

Every read command (and the write commands marked "yes" above) takes `--json`. The rules:

1. **One line** on stdout, compact separators, **keys sorted** - safe for `jq`, `grep` and line-based tools.
2. The document is the controller's REST answer for that call (the field list and types are in its OpenAPI
   description, `GET /v1/openapi.json`), plus a top-level **`schema_version`** (currently **1**).
   `compute status --json` and `compute bench workloads --json` are the documents the CLI builds itself
   (fields below).
3. **Stability:** fields are only ever *added* within one `schema_version`. Removing, renaming or changing the
   type of a field listed below raises `schema_version`. A script should ignore fields it does not know.
4. **Errors are not JSON:** a failing command prints nothing on stdout, one `compute: ...` line on stderr, and
   exits with the code from section 4. Check the exit code first. The one exception is `compute status`: telling
   you whether the controller answers is its job, so it always prints its document and still exits 3 or 6.

Guaranteed fields per command (`a[].b` = field `b` of every item of list `a`; `a.b` = field `b` of object `a`):

| Command | Source | Guaranteed fields |
|---|---|---|
| `compute status` | built by the CLI | `controller_url`, `reachable`, `detail`, `live_sessions`, `nodes` |
| `compute nodes` | `GET /v1/nodes` | `nodes`, `count`, `nodes[].node_id`, `nodes[].hostname`, `nodes[].state`, `nodes[].connected`, `nodes[].sample_age_s`, `nodes[].cpu_percent`, `nodes[].ram_used_bytes`, `nodes[].ram_total_bytes`, `nodes[].gpus`, `nodes[].scores`, `nodes[].not_enforced` |
| `compute nodes NODE_ID` | `GET /v1/nodes/{node_id}` | `node_id`, `hostname`, `os`, `platform`, `state`, `connected`, `last_seen_at`, `sample_age_s`, `agent_version`, `hardware`, `scores`, `owner_policy`, `not_enforced` |
| `compute jobs` | `GET /v1/jobs` | `jobs`, `count`, `next_cursor`, `jobs[].job_id`, `jobs[].name`, `jobs[].state`, `jobs[].placement`, `jobs[].failure_type`, `jobs[].created_at` |
| `compute jobs JOB_ID` | `GET /v1/jobs/{job_id}` | `job_id`, `name`, `state`, `failure_type`, `failure_detail`, `placement`, `created_at`, `updated_at`, `attempts`, `inputs`, `transitions` |
| `compute watch JOB_ID` | `GET /v1/jobs/{job_id}/split` | `job_id`, `job_state`, `queue_state`, `items_total`, `items_done`, `chunks.total`, `chunks.pending`, `chunks.leased`, `chunks.done`, `chunks.failed`, `lanes[].node_id`, `lanes[].state`, `lanes[].chunks_done`, `lanes[].items_done`, `lanes[].rate_items_s`, `lanes[].current`, `eta_s`, `elapsed_s`, `predicted_s`, `warnings` |
| `compute explain JOB_ID` | `GET /v1/jobs/{job_id}/decisions` | `job_id`, `decisions`, `count`, `decisions[].policy`, `decisions[].objective` (V0.14; null on older decisions), `decisions[].outcome`, `decisions[].chosen_node_id`, `decisions[].candidates`, `decisions[].actual`; V0.14 G5 (D300, D320): `decisions[].calibration_version`, `decisions[].replay_version`, `decisions[].margin_sentence` (null for a job that runs whole), and on every predicted term its calibration block (factor, n, key level, version, raw seconds), note, and - chosen candidate, after its attempt ended done - actual seconds and actual/predicted ratio (null = no measured phase is honestly the same work); V0.14 G6 (D324): `decisions[].decision_ms` (how long the decision took; null before migration 0033); the Plan tab shows exactly these fields (criterion-4 contract test) |
| `compute network` | `GET /v1/network` | `now`, `stale_after_s`, `links`, `controller_links`, `failures`, `prober` |
| `compute whoami` | `GET /v1/auth/whoami` | `user`, `user.name`, `user.role`, `legacy`, `token_id`, `token_expires_at` |
| `compute tokens list` | `GET /v1/auth/tokens` | `tokens`, `count`, `tokens[].id`, `tokens[].user`, `tokens[].name`, `tokens[].kind`, `tokens[].state`, `tokens[].current`, `tokens[].expires_at` |
| `compute secret list` | `GET /v1/secrets` | `secrets`, `count`, `secrets[].name`, `secrets[].created_at`, `secrets[].updated_at` |
| `compute data ls` | `GET /v1/data` | `inputs`, `count`, `inputs[].ref`, `inputs[].name`, `inputs[].version`, `inputs[].tree_id`, `inputs[].size`, `inputs[].files`, `inputs[].pinned` |
| `compute data show NAME` | `GET /v1/data/{name}` | `name`, `versions`, `versions[].ref`, `versions[].tree_id`, `versions[].pinned` |
| `compute fabric invites` | `GET /v1/fabric/invites` | `invites`, `invites[].id`, `invites[].state`, `invites[].expires_at`, `invites[].used_by_node` |
| `compute fabric invites --revoke ID` | `POST /v1/fabric/invites/{invite_id}/revoke` | `id`, `state`, `revoked_at` |
| `compute fabric invite` | `POST /v1/fabric/invites` | `code`, `invite_id`, `address`, `ca_pin`, `expires_at` |
| `compute node revoke NODE_ID` | `POST /v1/nodes/{node_id}/revoke` | `node_id`, `state`, `already_revoked`, `session_killed` |
| `compute telemetry export` | built by the CLI | `file`, `bytes`, `rows` (the file itself comes from `GET /v1/telemetry/export`) |
| `compute planner calibrate` | `POST /v1/planner/calibrate` | `matched_runs`, `created`, `created.version`, `created.status`, `created.reason`, `created.holdout_error_new`, `created.holdout_error_active`, `created.factors[].term`, `created.factors[].key_level`, `created.factors[].factor`, `created.factors[].n`, `skipped`, `superseded_version`, `in_use.mode`, `in_use.in_use_version` |
| `compute planner calibration` | `GET /v1/planner/calibration` | `in_use`, `in_use.mode`, `in_use.pinned_version`, `in_use.in_use_version`, `active_version`, `calibration`, `calibration.version`, `calibration.status`, `calibration.factors[].term`, `calibration.factors[].key_level`, `calibration.factors[].node_id`, `calibration.factors[].family`, `calibration.factors[].factor`, `calibration.factors[].n`, `calibration.factors[].median`, `term_phase`, `not_calibrated`, `recent`, `recent[].version`, `recent[].status` |
| `compute planner eval --campaign CAMPAIGN_ID` | `GET /v1/planner/eval-reports/{report_id}` | `id`, `created_at`, `campaign_ids`, `holdout`, `leaked`, `status`, `reason`, `detail`, `report`, `report.eval_version`, `report.settings`, `report.counts`, `report.verdicts[].criterion`, `report.verdicts[].status`, `report.verdicts[].would_be`, `report.verdicts[].value`, `report.verdicts[].detail`, `report.verdicts[].comparisons`, `report.latency` (V0.14 G6: decision latency, criterion 3), `report.policies[].key`, `report.policies[].counted_groups`, `report.policies[].mean_regret_pct`, `report.policies[].ci_pct`, `report.policies[].within_threshold_pct`, `report.policies[].unmeasured_groups`, `report.families`, `report.calibrations`, `report.groups[].picks` |
| `compute planner reports` | `GET /v1/planner/eval-reports` | `reports`, `reports[].id`, `reports[].created_at`, `reports[].campaign_ids`, `reports[].holdout`, `reports[].leaked`, `reports[].eval_version`, `reports[].evaluated_groups`, `reports[].verdicts` |
| `compute planner ml dataset` | `GET /v1/planner/ml/dataset` | `dataset_version`, `feature_version`, `digest`, `bytes`, `rows`, `max_runs`, `truncated`, `counts.runs`, `counts.runs_with_rows`, `counts.rows`, `counts.rows_by_term`, `counts.rows_by_family`, `counts.skipped`, `dataset_schema.fingerprint`, `dataset_schema.columns[].name`, `dataset_schema.features[].name`, `dataset_schema.features[].group`, `dataset_schema.terms[].name` (V0.15; the summary, format=summary; with --out FILE the CLI prints file, sidecar, bytes, rows and digest instead) |
| `compute planner ml compare` | built by the CLI | `report_version`, `dataset.digest`, `dataset.dataset_version`, `dataset.feature_version`, `dataset.rows`, `dataset.sidecar`, `settings`, `libraries`, `failure_classifier.node_failures`, `failure_classifier.status`, `terms[].term`, `terms[].status`, `terms[].rows`, `terms[].families`, `terms[].shipped.config.name`, `terms[].shipped.conformal_q`, `terms[].outer[].family`, `terms[].outer[].selected`, `terms[].per_family`, `terms[].pooled.male_model`, `terms[].pooled.male_calibration`, `terms[].pooled.coverage`, `terms[].pooled.usable_rate`, `terms[].vs_calibration.verdict`, `terms[].would_reject`, `terms[].interval_alternatives`, `terms[].importance` (V0.15 G2; the report.json document, built offline by `python -m meshforge.controller.ml.train compare`) |
| `compute planner ml models` | `GET /v1/planner/ml/models` | `in_use.mode`, `in_use.pinned_config`, `in_use.kind`, `in_use.in_use_set`, `in_use.unavailable`, `serving_set`, `sets[].set_version`, `sets[].status`, `sets[].reason`, `sets[].created_at`, `sets[].dataset_digest`, `sets[].feature_version`, `sets[].members[].term`, `sets[].members[].algorithm`, `sets[].members[].version`, `sets[].members[].metrics`, `sets[].members[].reason`, `retrain.available`, `retrain.missing`, `retrain.running`, `retrain.last` (V0.15 G3; with --set VERSION: GET /v1/planner/ml/models/{set_version}, one set plus its gate and inputs) |
| `compute planner ml train` | `POST /v1/planner/ml/train` | `started`, `retrain.available`, `retrain.running`, `retrain.last.created_set`, `retrain.last.status`, `retrain.last.reason`, `retrain.last.skipped`, `retrain.last.error` (V0.15 G3; with --wait the final state of GET /v1/planner/ml/train) |
| `compute planner ml pin SET_VERSION` | `POST /v1/planner/ml/models/{set_version}/pin` | `set_version`, `status`, `reason`, `members`, `gate`, `inputs` (V0.15 G3) |
| `compute planner ml unpin SET_VERSION` | `POST /v1/planner/ml/models/{set_version}/unpin` | `set_version`, `status`, `reason`, `members`, `gate`, `inputs` (V0.15 G3) |
| `compute bench workloads` | built by the CLI | `families`, `count`, `runtime_window_s`, `reference_hw`, `families[].name`, `families[].kind`, `families[].description`, `families[].env_base`, `families[].gpu`, `families[].size_params`, `families[].params_schema`, `families[].smallest`, `families[].smallest_hint_s` |
| `compute bench plan SPEC` | built by the CLI | `name`, `spec_digest`, `runs`, `groups`, `configs`, `repetitions`, `dropped`, `duplicates`, `estimated_hours`, `nodes`, `first`, `first[].seq`, `first[].group_id`, `first[].config_id`, `first[].repetition`, `first[].family`, `first[].params`, `first[].seed`, `first[].strategy`, `first[].node_id`, `first[].origin_node_id` |
| `compute bench create SPEC` | `POST /v1/bench/campaigns` | `campaign_id`, `name`, `state`, `run_count`, `counts`, `summary`, `spec_digest` |
| `compute bench campaigns` | `GET /v1/bench/campaigns` | `campaigns`, `count`, `campaigns[].campaign_id`, `campaigns[].name`, `campaigns[].state`, `campaigns[].run_count`, `campaigns[].counts` |
| `compute bench campaigns CAMPAIGN_ID` | `GET /v1/bench/campaigns/{campaign_id}` | `campaign_id`, `name`, `state`, `run_count`, `counts`, `summary`, `spec` |
| `compute bench runs CAMPAIGN_ID` | built by the CLI | `campaign_id`, `runs`, `count`, `runs[].run_id`, `runs[].seq`, `runs[].state`, `runs[].strategy`, `runs[].node_id`, `runs[].job_id` (every page of GET /v1/bench/campaigns/{campaign_id}/runs) |
| `compute bench labels CAMPAIGN_ID` | `GET /v1/bench/campaigns/{campaign_id}/labels` | `campaign_id`, `labels_version`, `campaign_state`, `rows`, `counts`, `groups`, `groups[].group_id`, `groups[].family`, `groups[].condition`, `groups[].label`, `groups[].best_config_id`, `groups[].best_strategy`, `groups[].best_node_id`, `groups[].margin_pct`, `groups[].tied_config_ids`, `groups[].planner_agrees`, `groups[].reason`, `groups[].configs[].config_id`, `groups[].configs[].total_ms` (with -o: file, bytes, rows, labels_version) |
| `compute bench report CAMPAIGN_ID` | `GET /v1/bench/campaigns/{campaign_id}/quality` | `campaign_id`, `quality_version`, `summary`, `summary.campaign_state`, `summary.pending`, `summary.runs_by_state`, `summary.records`, `summary.bench_role_records`, `summary.infra_failures`, `summary.owner_preempted`, `summary.infra_failure_rate`, `summary.condition_failures`, `summary.workload_failures`, `summary.measured_records`, `summary.rejected_ok`, `summary.other_failures`, `summary.crit1_infra_ok`, `summary.crit3_reps_ok`, `summary.flagged_configs`, `configs`, `configs[].config_id`, `configs[].n`, `configs[].total_ms`, `configs[].compute_per_iter_ns`, `configs[].outlier_attempt_ids`, `configs[].cv_flag`, `configs[].reps_flag`, `configs[].missing` |
| `compute bench submit CAMPAIGN_ID` | `POST /v1/bench/campaigns/{campaign_id}/submit` | `campaign_id`, `outcomes`, `outcomes[].run_id`, `outcomes[].outcome`, `outcomes[].job_id`, `submitted`, `deferred`, `skipped`, `counts` (with --wait also ended: run id -> final state) |
| `compute bench start CAMPAIGN_ID` | `POST /v1/bench/campaigns/{campaign_id}/start` | `campaign_id`, `state`, `runner_owned`, `started_at`, `finished_reason`, `max_concurrent_per_node`, `cooldown_s`, `max_infra_retries`, `counts`, `cancelled_jobs` |
| `compute bench pause CAMPAIGN_ID` | `POST /v1/bench/campaigns/{campaign_id}/pause` | `campaign_id`, `state`, `runner_owned`, `started_at`, `finished_reason`, `max_concurrent_per_node`, `cooldown_s`, `max_infra_retries`, `counts`, `cancelled_jobs` |
| `compute bench resume CAMPAIGN_ID` | `POST /v1/bench/campaigns/{campaign_id}/resume` | `campaign_id`, `state`, `runner_owned`, `started_at`, `finished_reason`, `max_concurrent_per_node`, `cooldown_s`, `max_infra_retries`, `counts`, `cancelled_jobs` |
| `compute bench cancel CAMPAIGN_ID` | `POST /v1/bench/campaigns/{campaign_id}/cancel` | `campaign_id`, `state`, `runner_owned`, `started_at`, `finished_reason`, `max_concurrent_per_node`, `cooldown_s`, `max_infra_retries`, `counts`, `cancelled_jobs` |
| `compute bench status CAMPAIGN_ID` | `GET /v1/bench/campaigns/{campaign_id}/status` | the fields above plus `done_percent`, `median_run_s`, `eta_s`, `infra_failure_rate`, `records`, `infra_failures`, `preempted`, `retries`, `runner_degraded` |

`compute status --json`: `controller_url` (the address used), `reachable` (`false` only when the controller
could not be reached at all - exit 6), `detail` (the error line, else `null`), `live_sessions` (from
`/healthz`), `nodes` (the `compute nodes` document without `schema_version`, or `null`).

Example:

```bash
compute jobs --state running --json | jq -r '.jobs[].job_id'
```

## 4. Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | OK | done |
| 1 | FAILURE | the command ran and failed for a reason not listed below (the controller refused for another reason, a local file could not be read or written, a node failed to prepare, an internal error) |
| 2 | USAGE_ERROR | wrong arguments or options, or the controller rejected a value we sent (`invalid_input`) |
| 3 | AUTH | not logged in, login expired or revoked, wrong password, not allowed (401/403), or the controller's CA does not match the fingerprint given |
| 4 | CONFIG_ERROR | the local configuration is unusable (`config.toml`, the token file, the CA file, the credentials folder) |
| 6 | UNAVAILABLE | the controller could not be reached, did not answer in time, or cannot answer right now (503) - retrying later may work |
| 8 | NOT_FOUND | the job, node, token, secret, input or invite does not exist |
| 126 | JOB_FAILED | `run -f`, `watch`: the fabric could not run or finish the job (stderr names the failure type) |
| 128 | JOB_CANCELLED | `run -f`, `watch`: the job was cancelled |
| 130 | INTERRUPTED | Ctrl-C while following (`run -f`, `logs -f`, `watch`): the job keeps running |
| 1-125 | (workload) | `run -f`: the job's own program exited with this code (clamped to 125) |

`compute fabric join` runs the agent's `join` and exits with the agent's code (same table: 4 configuration,
5 already enrolled, 6 controller unreachable).

The numbers are shared with `meshforge-controller` and `meshforge-agent`, which is why "unreachable" is 6 and
not found is 8 (4 and 5 were already taken by configuration and "already enrolled"). Source of truth:
`src/meshforge/cli/exits.py`.

```bash
compute jobs "$JOB" --json > job.json
case $? in
  0) ;;                       # use job.json
  3) compute login ;;         # log in again
  6) sleep 10; retry ;;       # controller down or busy
  8) echo "no such job" ;;
  *) exit 1 ;;
esac
```

## 5. Errors

Every expected failure is **one line** on stderr that says what failed, why, and what to do, for example:

```text
compute: cannot reach the controller at https://192.168.1.20:47110 (ConnectError) - is it running and is the address right? `compute status` shows which address is used ([cli] controller_url, or the one `compute login` stored)
compute: the controller refused the request (401): you are not logged in, or your login expired or was revoked - run `compute login`
compute: the controller refused the request (404): no such job - check the id or name (`compute jobs`, `compute nodes`, `compute data ls`...)
```

`compute` never prints a Python traceback for these. An unexpected error (a bug) is also one line,
`compute: internal error (TypeName) - this is a bug ...`, exit 1; run the command again with `COMPUTE_DEBUG=1` to
see the traceback when reporting it.

## 6. Shell completion

Completion for commands, subcommands and options:

```bash
compute --install-completion     # bash, zsh, fish or PowerShell - detected from the shell you run it in
compute --show-completion        # print the script instead, to install it yourself
```

Restart the shell afterwards. Per shell:

- **bash** - writes `~/.bash_completions/compute.sh` and sources it from `~/.bashrc`.
- **zsh** - writes `~/.zfunc/_compute` and adds it to `fpath` in `~/.zshrc`.
- **fish** - writes `~/.config/fish/completions/compute.fish`.
- **PowerShell** (Windows, native) - adds a `Register-ArgumentCompleter` block to your PowerShell profile
  (`$PROFILE`) once; running it again does not add a second copy. It **never changes the execution policy**
  (Typer's own installer sets it to `Unrestricted`; `compute` replaces that step). If PowerShell then says
  running scripts is disabled, allow your own profile once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Completion runs `compute` itself with no network access, so it works with the controller down.

If installing fails - a file it must write is not writable, or PowerShell is missing or does not print a profile
path - `compute` prints one what / why / what-to-do line and **exits 4** (`CONFIG_ERROR`, §4), whatever the
shell. Re-running the install never adds a second line to `~/.bashrc`, whatever encoding the file is in.
