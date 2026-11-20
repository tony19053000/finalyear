# D462 plan - per-part ML gate and serving (L2 -> LEAD, 2026-11-21)

Branch: fix/ml-per-term-gate. Waiting for OK before coding.

1. Gate (ml/train/gate.py): each part gets its own pass/fail with the same rules as today (too_few_held_out, coverage,
   family_tolerance, worse_than_reference). Result records approved_terms + rejected_terms (each with its reason).
2. A set goes active if at least one part passes and beats calibration by > 0.02 (the existing not_better check, per part).
   No part passes -> rejected, exactly as today.
3. Registry (ml/registry.py): store saves the approved list in the gate doc; load serves only approved parts, the others
   are marked "no model" with reason "not approved by the gate". Old sets without the list keep serving every part.
4. Planner: almost no change - it already falls back to calibration per part when a part has no model. New MlReason
   not_approved so explanations tell "rejected by the gate" from "never trained". Per-case fallback (OOD, wide interval,
   predictor error) unchanged.
5. Explain / plan JSON: each part already has source + detail.ml.reason; check compute explain / plan show ml or
   calibration per part, add a line listing the approved parts.
6. CLI + dashboard Planner > Models: show approved and rejected parts per set (compute planner ml models too).
7. Docs: ADR-0026 per-part ML gate + decision-log line D462. Cost formula and thresholds unchanged.
8. Tests: unit tests for the six cases in the task; ruff, format, mypy, lint-imports, unit tier; list PostgreSQL tests
   not runnable on Windows. Push branch + lab/coord/L2/D462-HANDOFF.md.
