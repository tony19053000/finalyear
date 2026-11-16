# Planner evaluation report

- eval_version: 2
- campaigns: `camp_01M7JZTGR7VNZ68DCCX6RK0ZQ3`, `camp_01M7JZTH0PDQ61CRYP5ZYW84CW`
- held-out family: every family (leave-one-out)
- criterion policy: `cost_model:calibrated:min_time`; threshold 15 %, target 80 %, at least 10 groups
- bootstrap: 10000 resamples, seed 20261022, level 0.95

## Verdicts

| criterion | status | would be | value | threshold | groups | detail |
|---|---|---|---|---|---|---|
| 1 | PASS | - | 100.00 | 80.00 | 12 | 100.0 % of 12 counted groups within 15 % of the oracle (target >= 80 %) |
| 2 | not_separable | - | 0.00 | 0.00 | 11 | cost_model:calibrated:min_time unmeasured in 12 group(s); difference CI reaches 0 for: random, round_robin, least_loaded, strongest_suitable, resource_score, cache_affinity |
| 3 | PASS | - | 5.95 | 100.00 | 108 | p99 5.946 ms over 108 decision(s) with >= 4 live nodes (budget < 100 ms) |

### Criterion 2, paired: rule policy regret minus criterion policy regret, on the groups counted for both

| rule policy | common groups | mean difference (% points) | 95 % CI | unmeasured |
|---|---|---|---|---|
| `random` | 11 | 0.00 | [0.00, 0.00] | 13 |
| `round_robin` | 11 | 0.00 | [0.00, 0.00] | 13 |
| `least_loaded` | 11 | 0.00 | [0.00, 0.00] | 13 |
| `strongest_suitable` | 11 | 0.00 | [0.00, 0.00] | 13 |
| `resource_score` | 11 | 0.00 | [0.00, 0.00] | 13 |
| `cache_affinity` | 11 | 0.00 | [0.00, 0.00] | 13 |

## Decision latency (criterion 3)

- dispatched decisions timed: 108 (without a time: 0)
- p50 3.97 ms, p99 5.95 ms, max 6.35 ms (nearest rank)
- over the 100 ms budget: 0
- criterion 3 judges snapshots of >= 4 live nodes (online, connected, accepting jobs): 108 decisions, p99 5.95 ms (at least 30 needed)

| snapshot nodes | decisions | p99 ms |
|---|---|---|
| 9 | 108 | 5.95 |

| live nodes | decisions | p99 ms |
|---|---|---|
| 4 | 108 | 5.95 |

## ML planner (cost_model:ml vs calibrated analytical)

- status: evaluated
- fold models trained on runs finished by 2026-11-16T20:51:13.542599+00:00
- criterion row `cost_model:ml:min_time` vs `cost_model:calibrated:min_time`; family tolerance 5.00 % points, families need >= 3 common groups

| verdict | status | would be | groups | mean diff % | CI | detail |
|---|---|---|---|---|---|---|
| ml_criterion_1 | not_separable | - | 12 | 0.00 | [0.00, 0.00] | ML regret minus calibrated regret: CI [0.000, 0.000] % reaches 0 over 12 common groups |
| ml_criterion_2 | PASS | - | 12 | - | - | every family with >= 3 common groups (2) within calibrated + 5 % points |

### ML criterion 2 per family

| family | groups | ML regret % | calibrated regret % | diff | status |
|---|---|---|---|---|---|
| split.csv_aggregate | 6 | 0.00 | 0.00 | 0.00 | pass |
| split.hash_files | 6 | 0.00 | 0.00 | 0.00 | pass |

### Fold models (one per held-out family, never registered)

| family | set version | training rows | model | why not |
|---|---|---|---|---|
| split.csv_aggregate | sha256:ec327758c8aefa21af9744aad0380dd475daee4a36dc2bc1c38f7f25d3392012 | 4589 | chunk_rate, combine, compute, env, lane_env | - |
| split.hash_files | sha256:f62c8cde8ce017fdb7b5c82b7fd06ec442d8e3b510afbbb1d7fd210a126bf1e2 | 4554 | chunk_rate, combine, compute, env, lane_env | - |

### What ML did in the chosen plans

Counted once per decision, on its chosen candidate only (the plan that would run); terms of candidates the planner did not choose are not counted.

| family | decisions | no fold model | predictor error | used rate | terms |
|---|---|---|---|---|---|
| all | 108 | 0 | 0 | 0.00 | below_min_predicted 44, interval_too_wide 60, ood 4 |
| split.csv_aggregate | 54 | 0 | 0 | 0.00 | below_min_predicted 44, interval_too_wide 6, ood 4 |
| split.hash_files | 54 | 0 | 0 | 0.00 | interval_too_wide 54 |

### Fold models' held-out prediction error per term

| term | rows | MALE model | MALE raw | MAPE model | coverage | usable |
|---|---|---|---|---|---|---|
| chunk_rate | 279 | 0.300 | 0.307 | 0.537 | 0.88 | 0.08 |
| combine | 83 | 0.394 | 1.872 | 0.303 | 0.87 | 0.00 |
| lane_env | 113 | 0.954 | 3.808 | 2.322 | 0.63 | 0.00 |

## Policies (regret vs the best measured configuration)

| policy | counted | mean regret % | 95 % CI | mean regret ms | within threshold % | zero regret | unmeasured | no replay |
|---|---|---|---|---|---|---|---|---|
| `random` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `round_robin` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `least_loaded` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `strongest_suitable` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `resource_score` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `cache_affinity` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `cost_model:uncalibrated:min_time` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `cost_model:uncalibrated:min_network` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `cost_model:uncalibrated:prefer_local` | 11 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 11 | 13 | 0 |
| `cost_model:calibrated:min_time` | 12 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 12 | 12 | 0 |
| `cost_model:calibrated:min_network` | 12 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 12 | 12 | 0 |
| `cost_model:calibrated:prefer_local` | 12 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 12 | 12 | 0 |
| `cost_model:ml:min_time` | 12 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 12 | 12 | 0 |
| `cost_model:ml:min_network` | 12 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 12 | 12 | 0 |
| `cost_model:ml:prefer_local` | 12 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 12 | 12 | 0 |
| `oracle` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |

## Per family (criterion policy and oracle)

| family | policy | counted | mean regret % | within % | unmeasured |
|---|---|---|---|---|---|
| split.csv_aggregate | `cost_model:calibrated:min_time` | 6 | 0.00 | 100.0 | 6 |
| split.csv_aggregate | `oracle` | 12 | 0.00 | 100.0 | 0 |
| split.hash_files | `cost_model:calibrated:min_time` | 6 | 0.00 | 100.0 | 6 |
| split.hash_files | `oracle` | 12 | 0.00 | 100.0 | 0 |

## Counts

- groups: 24 (evaluated 24, no replay 0)
- labels: best 22, insufficient 0, pending 0, tie 2
- records: 108 (used 108; skipped: no_replay 0, no_shadow 0, replay_unplannable 0, replay_unreadable 0)

## Calibrations used by the calibrated rows

| family | source | version | fitted runs | held out | factors |
|---|---|---|---|---|---|
| split.csv_aggregate | refit | - | 2024 | 45 | 48 |
| split.hash_files | refit | - | 2027 | 42 | 48 |
