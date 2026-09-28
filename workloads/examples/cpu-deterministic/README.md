# cpu-deterministic

A small CPU job whose output is **byte-identical on every machine**: exact
integer arithmetic on numbers from a seeded `random.Random` (a Monte Carlo
estimate of pi kept as a fraction, a prime sieve, a histogram, Collatz lengths).
No floating-point reductions, whose last bits can depend on the CPU.

```bash
compute run workloads/examples/cpu-deterministic --node <NODE_ID> -f
compute get <JOB_ID>            # -> ./outputs/<JOB_ID>/results/result.json
compute run workloads/examples/cpu-deterministic --local -f
```

The two `result.json` files (and the two attempts' `output_digest`, shown by
`compute jobs --json` / `GET /v1/jobs/<id>`) must be identical - V0.4 success
criterion 1. numpy is used when present and is not required: both paths give the
same bytes (`tests/unit/workloads/test_cpu_example.py` checks it). Runtime: a
few seconds.
