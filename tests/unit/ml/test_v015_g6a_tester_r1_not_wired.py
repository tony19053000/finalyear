# ruff: noqa: PERF401
"""V0.15 G6a tester r1 (D375 "never registered, never wired into plan()"):
a text-level sweep wider than the implementer's AST test - no planner,
dispatcher, registry, serving or retrain module mentions the classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3] / "src" / "meshforge" / "controller"
FORBIDDEN = (
    "train.classify",
    "classifier_examples",
    "ml_classify",
    "ClassifierExamples",
)
ALLOWED = {
    "ml/train/classify.py",
    "ml/train/__main__.py",
    "ml/train/report.py",
    "planner_eval/classifier_examples.py",
    "planner_eval/classifier_service.py",
    "api/ml_classify_routes.py",
}


def test_only_the_experiment_modules_mention_the_classifier() -> None:
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for word in FORBIDDEN:
            if word in text:
                offenders.append((rel, word))
    # the app wiring may register the admin route, nothing else
    offenders = [
        o
        for o in offenders
        if o[0] not in {"api/app.py", "api/__init__.py", "server.py", "http.py"}
    ]
    assert not offenders, offenders


def test_no_classifier_in_the_model_registry_kinds() -> None:
    reg = (ROOT / "ml" / "registry.py").read_text(encoding="utf-8").lower()
    assert "classif" not in reg
