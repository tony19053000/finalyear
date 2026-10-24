"""What the ML model did for a planner decision, as fields and as text
(V0.15 G5b, D342, D373).

Pure functions over the **stored decision JSON** (``plan_decision.candidates``,
V0.15 G4: a learnable term's ``detail.ml``, every row's ``ml_model_version``
and, after a predictor failure, ``ml_fallback``). They live in ``common``
because both sides need the same ones: the controller maps the stored blocks
to the decisions answer's typed ``TermOut.ml`` / ``DecisionOut.ml_model_version``
/ ``ml_fallback`` with :func:`term_ml` and :func:`decision_ml`, and
``compute explain`` prints them with :func:`ml_line` and
:func:`ml_decision_text` (the CLI may not import the controller). The
dashboard's Plan tab renders the same text with its own copy of these two
functions (``frontend/src/components/jobs/mlText.ts``); the explain-contract
fixture carries every text this module makes for its payload and both test
suites compare against it, so the two copies cannot drift.

**Text rules (one line per term, ASCII so every console prints it):**

* ML corrected the term: ``ML 2.53 s -> 2.00 s (mem-3a1f00aa, x0.79, +-18 %)``
  - analytical (pre-calibration) seconds -> the seconds the planner used, the
  model set version (first 12 characters after ``sha256:``), the correction
  factor ``e^y_hat`` and the half-width of the interval as a percentage
  (``e^((hi - lo) / 2) - 1``);
* ML did not: ``ML not used: out of distribution (range:hw_cpu_gflops)
  (mem-3a1f00aa, x0.82, +-10 %)`` - the reason in words, the OOD test's own
  reason, and what the model said when it was asked;
* a Mode 3 lane's rate (seconds per item, nested as ``rate``) follows as
  ``; rate: ML 174.00 ms/item -> 137.00 ms/item (...)``.

Numbers are printed with :func:`fixed` - round half away from zero on the
exact binary value, which is what JavaScript's ``Number.toFixed`` does - so
the Python and TypeScript copies print the same digits.

Every value read here may be anything (stored JSON, an older controller):
nothing raises; an unreadable part is ``None`` or left out.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

__all__ = [
    "ML_REASON_TEXT",
    "decision_ml",
    "fixed",
    "ml_decision_text",
    "ml_line",
    "reason_text",
    "short_version",
    "term_ml",
]

ML_REASON_TEXT: Final[Mapping[str, str]] = {
    "no_model": "no model for this term",
    "ood": "out of distribution",
    "interval_too_wide": "interval too wide",
    "exact_by_construction": "exact by construction",
    "below_min_predicted": "below the trained range",
    "predictor_error": "predictor error",
    "combine_node_unknown": "combine node unknown",
}
"""``detail.ml.reason`` (``planner/predictor.MlReason`` + G5a's
``combine_node_unknown``) -> words. An unknown reason is printed as is."""

MAX_Y_HAT: Final = 50.0
"""As ``planner/predictor.MAX_Y_HAT``: a bigger ``|y_hat|`` is not a
prediction and is never turned into a factor."""
_MAX_FIXED: Final = 1e15
"""Larger magnitudes print as ``-`` (``toFixed`` switches to exponent form
at 1e21; well before that a number in a term cell is noise)."""
_VERSION_CHARS: Final = 12
_DASH: Final = "-"


def _num(value: object) -> float | None:
    """A finite number or ``None`` (a JSON string, bool or null never is)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nonneg(value: object) -> float | None:
    number = _num(value)
    return number if number is not None and number >= 0.0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def fixed(value: float | None, digits: int) -> str:
    """``value`` with ``digits`` decimals, rounded half away from zero on its
    exact binary value (``Number.prototype.toFixed``); ``-0.00`` prints as
    ``0.00``; ``None``, non-finite or ``|value| >= 1e15`` -> ``-``."""
    if value is None or not math.isfinite(value) or abs(value) >= _MAX_FIXED:
        return _DASH
    step = Decimal(1).scaleb(-digits)
    out = Decimal(value).quantize(step, rounding=ROUND_HALF_UP)
    if out.is_zero():
        out = abs(out)
    return f"{out:f}"


def short_version(version: object) -> str:
    """A model set version for a cell: ``sha256:`` dropped, 12 characters."""
    text = _text(version)
    if text is None:
        return "?"
    return text.removeprefix("sha256:")[:_VERSION_CHARS]


def reason_text(reason: object) -> str:
    """A fallback reason in words (:data:`ML_REASON_TEXT`; unknown -> as is)."""
    text = _text(reason)
    if text is None:
        return "?"
    return ML_REASON_TEXT.get(text, text)


# ------------------------------------------------------------------ fields


def _factor(y_hat: float | None) -> float | None:
    # NaN fails every comparison: test the bound positively (as
    # ml.predictions._point does), so NaN / inf give no factor (G6b tester T4)
    if y_hat is None or not abs(y_hat) <= MAX_Y_HAT:
        return None
    return math.exp(y_hat)


def _block(block: Mapping[str, Any], seconds: float | None) -> dict[str, Any]:
    """One ``detail.ml`` block as the answer's fields (``rate`` excluded).
    ``seconds`` = the term's own seconds (the corrected value when ML was
    used); ``None`` for a nested rate block, whose corrected value is
    ``raw_s x e^y_hat`` (seconds per item)."""
    used = block.get("used") is True
    y_hat, lo, hi = (_num(block.get(k)) for k in ("y_hat", "lo", "hi"))
    raw_s = _nonneg(block.get("raw_s"))
    corrected: float | None = None
    if used:
        if seconds is not None:
            corrected = seconds
        else:
            factor = _factor(y_hat)
            corrected = (
                None if raw_s is None or factor is None else round(raw_s * factor, 9)
            )
    return {
        "model_version": _text(block.get("model_version")),
        "used": used,
        "reason": None if used else _text(block.get("reason")),
        "ood_reason": None if used else _text(block.get("ood_reason")),
        "y_hat": y_hat,
        "lo": lo,
        "hi": hi,
        "raw_s": raw_s,
        "corrected_s": corrected,
    }


def term_ml(term: object) -> dict[str, Any] | None:
    """The answer's ``TermOut.ml`` for a stored term (module docstring);
    ``None`` when the term has no readable ``detail.ml`` (a decision planned
    without a model, a term ML never covers, or a pre-V0.15 row)."""
    if not isinstance(term, Mapping):
        return None
    detail = term.get("detail")
    block = detail.get("ml") if isinstance(detail, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    out = _block(block, _nonneg(term.get("seconds")))
    rate = block.get("rate")
    out["rate"] = _block(rate, None) if isinstance(rate, Mapping) else None
    return out


def decision_ml(candidates: object) -> tuple[str | None, str | None]:
    """``(ml_model_version, ml_fallback)`` of a stored decision: G4 writes
    both on **every** row (or on none), so the first row that has one names
    it; ``(None, None)`` for a decision planned without a model."""
    version: str | None = None
    fallback: str | None = None
    rows = candidates if isinstance(candidates, list | tuple) else ()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        version = version or _text(row.get("ml_model_version"))
        fallback = fallback or _text(row.get("ml_fallback"))
        if version is not None and fallback is not None:
            break
    return version, fallback


# ------------------------------------------------------------------ text


def _said(ml: Mapping[str, Any]) -> str:
    """``(mem-3a1f00aa, x0.79, +-18 %)`` - the version, factor and spread
    when the model answered."""
    parts = [short_version(ml.get("model_version"))]
    factor = _factor(_num(ml.get("y_hat")))
    if factor is not None:
        parts.append(f"x{fixed(factor, 2)}")
    lo, hi = _num(ml.get("lo")), _num(ml.get("hi"))
    if lo is not None and hi is not None:
        spread = (math.exp(min((hi - lo) / 2.0, MAX_Y_HAT)) - 1.0) * 100.0
        parts.append(f"+-{fixed(spread, 0)} %")
    return "(" + ", ".join(parts) + ")"


def _amount(value: object, *, per_item: bool) -> str:
    number = _num(value)
    if per_item:
        return f"{fixed(None if number is None else number * 1000.0, 2)} ms/item"
    return f"{fixed(number, 2)} s"


def _one(ml: Mapping[str, Any], *, per_item: bool) -> str:
    if ml.get("used") is True:
        return (
            f"ML {_amount(ml.get('raw_s'), per_item=per_item)} -> "
            f"{_amount(ml.get('corrected_s'), per_item=per_item)} {_said(ml)}"
        )
    why = reason_text(ml.get("reason"))
    ood = _text(ml.get("ood_reason"))
    if ood is not None:
        why += f" ({ood})"
    return f"ML not used: {why} {_said(ml)}"


def ml_line(ml: object) -> str | None:
    """The term's ML line from its typed ``ml`` (module docstring); ``None``
    when there is none."""
    if not isinstance(ml, Mapping):
        return None
    line = _one(ml, per_item=False)
    rate = ml.get("rate")
    if isinstance(rate, Mapping):
        line += "; rate: " + _one(rate, per_item=True)
    return line


def ml_decision_text(version: object, fallback: object) -> str | None:
    """The decision's ML header part: ``ML set mem-3a1f00aa`` and, after a
    predictor failure (D363), ``- fell back to calibration: predictor
    error``; ``None`` for a decision planned without a model."""
    if _text(version) is None and _text(fallback) is None:
        return None
    text = f"ML set {short_version(version)}"
    if _text(fallback) is not None:
        text += f" - fell back to calibration: {reason_text(fallback)}"
    return text
