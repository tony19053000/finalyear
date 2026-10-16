# ruff: noqa: E501
"""Tester r1 (V0.8 G4): the committed YAML differential corpus still equals what the CLI loader says."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "_support"))

import v08_g4_yaml_corpus as corpus  # noqa: E402

FILE = (
    ROOT
    / "frontend"
    / "src"
    / "pages"
    / "jobs"
    / "submit"
    / "yamlCorpus_v08_g4_tester_r1.json"
)


def test_corpus_matches_the_cli_loader() -> None:
    committed = json.loads(FILE.read_text(encoding="utf-8"))
    assert len(committed) > 4000
    stale = [c["text"] for c in committed if corpus.classify(c["text"]) != c["expect"]]
    assert stale[:5] == [], (
        "regenerate: python tests/_support/v08_g4_yaml_corpus.py > the json"
    )


def test_walk_corpus_matches_the_cli_build_tree() -> None:
    import v08_g4_walk_corpus as walk_corpus

    path = FILE.with_name("walkCorpus_v08_g4_tester_r1.json")
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert len(committed) >= 400
    stale = [
        c["files"]
        for c in committed
        if walk_corpus.verdict({"files": c["files"]}) != c["expect"]
    ]
    assert stale[:3] == []
