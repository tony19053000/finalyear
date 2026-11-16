# Planner evaluation report

- eval_version: 2
- campaigns: `camp_01M7JF80H6759V5SZ8KKW6VM6T`
- held-out family: every family (leave-one-out)
- criterion policy: `cost_model:calibrated:min_time`; threshold 15 %, target 80 %, at least 10 groups
- bootstrap: 10000 resamples, seed 20261022, level 0.95

## Verdicts

| criterion | status | would be | value | threshold | groups | detail |
|---|---|---|---|---|---|---|
| 1 | PASS | - | 100.00 | 80.00 | 24 | 100.0 % of 24 counted groups within 15 % of the oracle (target >= 80 %) |
| 2 | not_separable | - | 0.00 | 0.00 | 24 | cost_model:calibrated:min_time unmeasured in 0 group(s); difference CI reaches 0 for: round_robin, least_loaded, strongest_suitable, resource_score |
| 3 | PASS | - | 5.79 | 100.00 | 324 | p99 5.785 ms over 324 decision(s) with >= 4 live nodes (budget < 100 ms) |

### Criterion 2, paired: rule policy regret minus criterion policy regret, on the groups counted for both

| rule policy | common groups | mean difference (% points) | 95 % CI | unmeasured |
|---|---|---|---|---|
| `random` | 24 | 12.52 | [4.35, 24.67] | 0 |
| `round_robin` | 24 | 0.00 | [0.00, 0.00] | 0 |
| `least_loaded` | 24 | 0.00 | [0.00, 0.00] | 0 |
| `strongest_suitable` | 24 | 0.00 | [0.00, 0.00] | 0 |
| `resource_score` | 24 | 0.00 | [0.00, 0.00] | 0 |
| `cache_affinity` | 24 | 11.18 | [4.97, 20.77] | 0 |

## Decision latency (criterion 3)

- dispatched decisions timed: 324 (without a time: 0)
- p50 3.57 ms, p99 5.79 ms, max 6.59 ms (nearest rank)
- over the 100 ms budget: 0
- criterion 3 judges snapshots of >= 4 live nodes (online, connected, accepting jobs): 324 decisions, p99 5.79 ms (at least 30 needed)

| snapshot nodes | decisions | p99 ms |
|---|---|---|
| 9 | 324 | 5.79 |

| live nodes | decisions | p99 ms |
|---|---|---|
| 4 | 324 | 5.79 |

## ML planner (cost_model:ml vs calibrated analytical)

- status: evaluated
- fold models trained on runs finished by 2026-11-16T19:28:53.311093+00:00
- criterion row `cost_model:ml:min_time` vs `cost_model:calibrated:min_time`; family tolerance 5.00 % points, families need >= 3 common groups

| verdict | status | would be | groups | mean diff % | CI | detail |
|---|---|---|---|---|---|---|
| ml_criterion_1 | not_separable | - | 24 | 0.00 | [0.00, 0.00] | ML regret minus calibrated regret: CI [0.000, 0.000] % reaches 0 over 24 common groups |
| ml_criterion_2 | PASS | - | 24 | - | - | every family with >= 3 common groups (2) within calibrated + 5 % points |

### ML criterion 2 per family

| family | groups | ML regret % | calibrated regret % | diff | status |
|---|---|---|---|---|---|
| cpu.primes | 12 | 0.00 | 0.00 | 0.00 | pass |
| split.image_classify | 12 | 0.00 | 0.00 | 0.00 | pass |

### Fold models (one per held-out family, never registered)

| family | set version | training rows | model | why not |
|---|---|---|---|---|
| cpu.primes | sha256:3ddf807768c1cbcebdaac70370bd483b92f6def3c311c248819f0395ae997e3d | 3700 | chunk_rate, combine, lane_env | - |
| split.image_classify | sha256:8f8c7f2f2e244f2ce19af5e7bcdb0c9ce6b7c3b5f8533b1e1ae07a74b938079c | 2623 | compute, env | - |

### What ML did in the chosen plans

Counted once per decision, on its chosen candidate only (the plan that would run); terms of candidates the planner did not choose are not counted.

| family | decisions | no fold model | predictor error | used rate | terms |
|---|---|---|---|---|---|
| all | 324 | 0 | 0 | 0.00 | exact_by_construction 125, no_model 658 |
| cpu.primes | 144 | 0 | 0 | 0.00 | exact_by_construction 117, no_model 459 |
| split.image_classify | 180 | 0 | 0 | 0.00 | exact_by_construction 8, no_model 199 |

## Policies (regret vs the best measured configuration)

| policy | counted | mean regret % | 95 % CI | mean regret ms | within threshold % | zero regret | unmeasured | no replay |
|---|---|---|---|---|---|---|---|---|
| `random` | 24 | 12.52 | [4.35, 24.67] | 8935.5 | 70.8 | 15 | 0 | 0 |
| `round_robin` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `least_loaded` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `strongest_suitable` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `resource_score` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cache_affinity` | 24 | 11.18 | [4.97, 20.77] | 10038.0 | 79.2 | 12 | 0 | 0 |
| `cost_model:uncalibrated:min_time` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:uncalibrated:min_network` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:uncalibrated:prefer_local` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:calibrated:min_time` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:calibrated:min_network` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:calibrated:prefer_local` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:ml:min_time` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:ml:min_network` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `cost_model:ml:prefer_local` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |
| `oracle` | 24 | 0.00 | [0.00, 0.00] | 0.0 | 100.0 | 24 | 0 | 0 |

## Per family (criterion policy and oracle)

| family | policy | counted | mean regret % | within % | unmeasured |
|---|---|---|---|---|---|
| cpu.primes | `cost_model:calibrated:min_time` | 12 | 0.00 | 100.0 | 0 |
| cpu.primes | `oracle` | 12 | 0.00 | 100.0 | 0 |
| split.image_classify | `cost_model:calibrated:min_time` | 12 | 0.00 | 100.0 | 0 |
| split.image_classify | `oracle` | 12 | 0.00 | 100.0 | 0 |

## Counts

- groups: 24 (evaluated 24, no replay 0)
- labels: best 24, insufficient 0, pending 0, tie 0
- records: 324 (used 324; skipped: no_replay 0, no_shadow 0, replay_unplannable 0, replay_unreadable 0)

## Calibrations used by the calibrated rows

| family | source | version | fitted runs | held out | factors |
|---|---|---|---|---|---|
| cpu.primes | refit | - | 1601 | 381 | 37 |
| split.image_classify | refit | - | 1600 | 382 | 41 |
