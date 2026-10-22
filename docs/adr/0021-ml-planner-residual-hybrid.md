# ADR-0021: ML planner as a residual hybrid over the analytical model

- **Status:** Proposed (2026-10-22, V0.15 brief D333–D344; awaiting team)
- **Context links:** ADR-0007, ADR-0018, ADR-0020, ARCHITECTURE §7, §18

## Context

V0.15 must make cost predictions measurably better with machine learning, using real telemetry, without ever making
the live planner worse. Constraints: small data (hundreds to a few thousand runs, tens of labelled groups), a pure
and replayable planner (ADR-0020), a 100 ms decision budget, the remote-code-execution security baseline, and the
requirement that every decision stays explainable.

## Decision

1. ML predicts the **log residual** `ln(actual / analytical_raw)` per cost term; the corrected term is
   `analytical_raw × exp(ŷ)`. The planner (candidates, feasibility, margin test, ranking) is unchanged.
2. Features are taken from the decision's **replay snapshot** (what the planner saw), with a versioned schema.
3. Models are **compared** (ridge, tree, forest, LightGBM, XGBoost, kNN, plus raw-analytical and calibration
   baselines) by leave-one-family-out CV; deterministic training.
4. Use is gated **per term, per candidate**: in-distribution and a narrow split-conformal interval → ML; else
   calibration; else raw analytical. Every term records which one was used and why.
5. Models live in a versioned registry with the D306/D319 gated activation on the D329 stratified slice; artefacts in
   native/array formats, **never pickle**.

## Alternatives

- **Predict absolute times** — needs much more data, extrapolates badly to unseen sizes/laptops, and has no exact
  fallback. Rejected.
- **Direct strategy classifier** (job → best mode/node set) — opaque, cannot explain terms, needs a label per
  configuration space; kept only as an offline experiment (D343).
- **Bayesian / GP models** — good uncertainty, but scale and dependency cost for no clear gain over conformal
  intervals on tree models at this data size.
- **Online learning in the dispatch loop** — non-reproducible and hard to gate; scheduled retraining with a gate
  instead.

## Trade-offs

- Residual learning caps how far ML can move a prediction from physics when data is thin — intended.
- Conformal intervals give marginal (not per-point) coverage; the coverage check on held-out families makes that
  visible.
- Three new controller dependencies (scikit-learn, LightGBM, XGBoost); agents untouched; the controller runs without
  them as V0.14.

## Consequences

- V0.14's evaluator, gates and replay are reused, not duplicated.
- Criterion 1 can honestly end `not_separable` on small lab data; the report then still shows per-term error gains.
- A lab data-collection step (0.15.11) is added to the roadmap.
