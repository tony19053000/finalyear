"""Tester r2 (V0.8 G4): committed r2 differential corpora still equal what the CLI says."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "_support"))

import v08_g4_r2_corpus as r2  # noqa: E402
import v08_g4_walk_corpus as walk_corpus  # noqa: E402
import v08_g4_yaml_corpus as yaml_corpus  # noqa: E402

DIR = ROOT / "frontend" / "src" / "pages" / "jobs" / "submit"


def test_r2_yaml_corpus_matches_the_cli_loader() -> None:
    committed = json.loads(
        (DIR / "yamlCorpus_v08_g4_tester_r2.json").read_text(encoding="utf-8")
    )
    assert len(committed) > 10000
    stale = [
        c["text"] for c in committed if yaml_corpus.classify(c["text"]) != c["expect"]
    ]
    assert stale[:5] == []


def test_r2_walk_corpus_matches_the_cli_build_tree() -> None:
    committed = json.loads(
        (DIR / "walkCorpus_v08_g4_tester_r2.json").read_text(encoding="utf-8")
    )
    assert len(committed) >= 1200 + len(r2.order_cases())
    stale = [
        c["files"]
        for c in committed
        if walk_corpus.case_runs_here(c)
        and walk_corpus.verdict({"files": c["files"]}) != c["expect"]
    ]
    assert stale[:3] == []


def test_r2_walk_corpus_exercises_the_cli_both_ways() -> None:
    committed = json.loads(
        (DIR / "walkCorpus_v08_g4_tester_r2.json").read_text(encoding="utf-8")
    )
    ignored = sum(1 for c in committed if c["expect"].get("ignored", 0) > 0)
    kept_all = sum(1 for c in committed if c["expect"].get("ignored", 1) == 0)
    assert ignored > 300
    assert kept_all > 20
