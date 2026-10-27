# V1.0 soak - 1.0 h (compressed dry run) - **FAIL**

> Generated 2026-10-27T20:55:41.766477+00:00 by `lab/soak.py report` from `verdict.json` (the JSON is the evidence; this file is its rendering).

## Run

| Field | Value |
|---|---|
| Seed / nonce | 7 / 563b0afc |
| Start (t0) / end | 2026-10-27T19:49:41+00:00 / 2026-10-27T20:55:41+00:00 |
| Load + chaos / quiet windows / idle tail | 1.0 h / 0.1 h each / 0.1 h |
| Link | wifi |
| Laptops | ctl (linux, controller), a0 (linux, agent), a1 (linux, agent), a2 (linux, agent) |
| Git / package | 86878909 / 0.1.0.dev0 |

## Rules (D410, D411)

| Rule | Id | Verdict | Summary |
|---|---|---|---|
| D410-1 | `lost_jobs` | **PASS** | 177 jobs: 0 lost, 0 late, 2 failed with an explained cause |
| D410-2 | `crashes` | **PASS** | 4 restarts seen: 4 caused by chaos, 0 unexplained; 0 processes never came back |
| D410-3 | `memory` | **FAIL** | 4 processes; worst quiet-to-quiet RSS growth 24.68 % |
| D410-4 | `handles` | **FAIL** | 8 counters (FDs on Linux, handles on Windows, threads) |
| D410-5 | `db_growth` | **PASS** | database 9.2 -> 15.2 MiB |
| D410-6 | `disk` | **PASS** | 3 folders judged (3 PASS) |
| D410-7 | `heartbeat` | **PASS** | 7 OFFLINE events, 0 unexplained; 0 unexplained controller outages |
| D411 | `chaos` | **PASS** | 9 chaos actions applied (>= 2 needed) |
| D410 | `coverage` | **PASS** | 1 samplers |

## Details

### lost_jobs - PASS

States: `{'cancelled': 3, 'failed': 4, 'succeeded': 170}`; outputs content-checked: 50.
- lost: 0
- late: 0
- failed_explained: 2
  - `{'seq': 30, 'job_id': 'job_01M5ZF674A9Y9NYTQZFS61RJM9', 'kind': 'm1_long', 'by': 'A001', 'failure_type': 'user_error'}`
  - `{'seq': 143, 'job_id': 'job_01M5ZH8HSJAD419M7YQEQJC2C9', 'kind': 'm1_short', 'by': 'A009', 'failure_type': 'user_error'}`
- duplicate_submissions: 0
- unresolved_submissions: 0

### crashes - PASS

- unexplained: 0
- never_returned: 0
- explained: 4
  - `{'sampler': 'ctl', 'target': 'controller', 'role': 'controller', 'host': 'ctl', 'at': '2026-10-27T20:05:31.514+00:00', 'old_pid': 7594, 'pid': 61661, 'by': 'A002'}`
  - `{'sampler': 'ctl', 'target': 'agent1', 'role': 'agent', 'host': 'a1', 'at': '2026-10-27T20:23:01.625+00:00', 'old_pid': 7940, 'pid': 116848, 'by': 'A006'}`
  - `{'sampler': 'ctl', 'target': 'agent1', 'role': 'agent', 'host': 'a1', 'at': '2026-10-27T20:27:51.612+00:00', 'old_pid': 116848, 'pid': 129304, 'by': 'A006'}`
  - `{'sampler': 'ctl', 'target': 'agent0', 'role': 'agent', 'host': 'a0', 'at': '2026-10-27T20:40:36.614+00:00', 'old_pid': 7939, 'pid': 138297, 'by': 'A010'}`

### memory - FAIL

| series | role | lifetimes | growth_basis | quiet1_median_mib | quiet2_median_mib | growth_pct | slope_mb_h | samples | verdict |
|---|---|---|---|---|---|---|---|---|---|
| ctl/agent0 | agent | 2 | longest_lifetime | 122.42 | 148.09 | 20.97 | 27.058 | [54, 54] | FAIL |
| ctl/agent1 | agent | 3 | longest_lifetime | 120.4 | 150.12 | 24.68 | 58.305 | [54, 54] | FAIL |
| ctl/agent2 | agent | 1 | quiet_windows | 119.63 | 144.96 | 21.17 | 22.532 | [54, 72] | FAIL |
| ctl/controller | controller | 2 | longest_lifetime | 211.16 | 224.57 | 6.35 | 18.173 | [54, 54] | FAIL |

### handles - FAIL

| series | metric | lifetimes | growth_basis | quiet1_median | quiet2_median | bound | samples | verdict |
|---|---|---|---|---|---|---|---|---|
| ctl/agent0 | fds | 2 | longest_lifetime | 28.0 | 24.0 | 29.4 | [54, 54] | PASS |
| ctl/agent0 | threads | 2 | longest_lifetime | 26.5 | 41.0 | 27.82 | [54, 54] | FAIL |
| ctl/agent1 | fds | 3 | longest_lifetime | 24.0 | 24.0 | 25.2 | [54, 54] | PASS |
| ctl/agent1 | threads | 3 | longest_lifetime | 23.0 | 41.0 | 24.15 | [54, 54] | FAIL |
| ctl/agent2 | fds | 1 | quiet_windows | 24.0 | 28.0 | 25.2 | [54, 72] | FAIL |
| ctl/agent2 | threads | 1 | quiet_windows | 24.0 | 42.0 | 25.2 | [54, 72] | FAIL |
| ctl/controller | fds | 2 | longest_lifetime | 28.0 | 28.0 | 29.4 | [54, 54] | PASS |
| ctl/controller | threads | 2 | longest_lifetime | 39.0 | 40.0 | 40.95 | [54, 54] | PASS |

### db_growth - PASS

```
{'samples': 226, 'bytes_per_job': {'jobs': [88, 88], 'bytes': [3719168, 2465792], 'bytes_per_job': [42263, 28020], 'expected_2nd_half_bytes': 3719168, 'tolerance_bytes': 8388608, 'verdict': 'PASS'}, 'idle_tail': {'from_h': 1.043, 'to_h': 1.097, 'grew_without_jobs': {}, 'time_driven_rows_per_h': {'audit_event': 0.0, 'network_link': 0.0, 'resource_snapshot': 221.5}, 'verdict': 'PASS'}, 'wal': {'max_bytes': 33554432, 'bound_bytes': 1677721600, 'verdict': 'PASS'}}
```

### disk - PASS

| series | max_bytes | cap_bytes | verdict |
|---|---|---|---|
| ctl/cas0 | 19250328 | 67108864 | PASS |
| ctl/cas1 | 21277002 | 67108864 | PASS |
| ctl/cas2 | 19176146 | 67108864 | PASS |

### heartbeat - PASS

- unexplained: 0
- unexplained_outages: 0
- explained: 7
  - `{'at': '2026-10-27T20:10:06.781+00:00', 'node_id': 'node_01M5ZEHMZ9RPQKJCF71PQNMF2G', 'hostname': 'JARVISM3', 'host': 'a0', 'by': 'A002'}`
  - `{'at': '2026-10-27T20:10:06.781+00:00', 'node_id': 'node_01M5ZEHMZFK7BAB9VEP6N81Z1H', 'hostname': 'JARVISM3', 'host': 'a1', 'by': 'A002'}`
  - `{'at': '2026-10-27T20:10:06.781+00:00', 'node_id': 'node_01M5ZEHMZPGEMYFP7TD8QGPR8E', 'hostname': 'JARVISM3', 'host': 'a2', 'by': 'A002'}`
  - `{'at': '2026-10-27T20:27:52.781+00:00', 'node_id': 'node_01M5ZEHMZFK7BAB9VEP6N81Z1H', 'hostname': 'JARVISM3', 'host': 'a1', 'by': 'A006'}`
  - `{'at': '2026-10-27T20:32:49.484+00:00', 'node_id': 'node_01M5ZEHMZ9RPQKJCF71PQNMF2G', 'hostname': 'JARVISM3', 'host': 'a0', 'by': 'A008'}`
  - `{'at': '2026-10-27T20:32:49.484+00:00', 'node_id': 'node_01M5ZEHMZFK7BAB9VEP6N81Z1H', 'hostname': 'JARVISM3', 'host': 'a1', 'by': 'A007'}`
  - `{'at': '2026-10-27T20:32:49.484+00:00', 'node_id': 'node_01M5ZEHMZPGEMYFP7TD8QGPR8E', 'hostname': 'JARVISM3', 'host': 'a2', 'by': 'A008'}`
- observed_h: 1.108

### chaos - PASS

By kind: `{'kill_agent': 2, 'kill_controller': 1, 'kill_job_tree': 2, 'net_down_long': 1, 'net_down_short': 1, 'netem': 1, 'reboot': 1}`; by outcome: `{'done': 9, 'skipped': 1}`.

| id | kind | target | started | effect until | outcome | detail |
|---|---|---|---|---|---|---|
| A001 | kill_job_tree | a1 | 2026-10-27T20:00:36.962614+00:00 | 2026-10-27T20:00:37.371092+00:00 | done | killed=1 |
| A002 | kill_controller | ctl | 2026-10-27T20:05:29.040840+00:00 | 2026-10-27T20:05:29.243032+00:00 | done | controller SIGKILL; restarted by the harness |
| A003 | net_down_long | ctl | 2026-10-27T20:09:36.578714+00:00 | 2026-10-27T20:10:06.578714+00:00 | done | 4 links blackholed for 30 s |
| A004 | netem | a2 | 2026-10-27T20:14:29.784987+00:00 | 2026-10-27T20:15:29.784987+00:00 | done | 200 ms on 1 links (no loss) |
| A005 | cancel_mode3 | ctl | 2026-10-27T20:19:18.298945+00:00 | 2026-10-27T20:20:18.545754+00:00 | skipped | no running Mode 3 job to cancel |
| A006 | kill_agent | a1 | 2026-10-27T20:23:00.424495+00:00 | 2026-10-27T20:23:00.634576+00:00 | done | agent1 SIGKILL |
| A007 | reboot | a1 | 2026-10-27T20:27:48.524260+00:00 | 2026-10-27T20:28:12.524260+00:00 | done | agent1 SIGKILL |
| A008 | net_down_short | ctl | 2026-10-27T20:32:39.348968+00:00 | 2026-10-27T20:32:49.348968+00:00 | done | 4 links blackholed for 10 s |
| A009 | kill_job_tree | a0 | 2026-10-27T20:36:47.974850+00:00 | 2026-10-27T20:36:48.183874+00:00 | done | killed=1 |
| A010 | kill_agent | a0 | 2026-10-27T20:40:32.731557+00:00 | 2026-10-27T20:40:32.941000+00:00 | done | agent0 SIGKILL |

### coverage - PASS

| sampler | ticks | covered_share | largest_gap_s | verdict |
|---|---|---|---|---|
| ctl | 797 | 1.0063 | 10.1 | PASS |

