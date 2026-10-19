"""V0.10 G1 (0.10.1, D260): the ``split:`` manifest spec.

* every ``over`` kind validates and round-trips through the canonical form;
* problems carry their YAML path (``split.over.range.step``);
* placeholders: ``{items_file}``/``{chunk_index}``/``{out}`` only in a split
  job's entrypoint, ``{parts}``/``{out}`` only in ``combine.script``;
* a manifest without ``split`` hashes exactly as before V0.10 (fixtures taken
  from the pre-change code).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from meshforge.common.manifest import (
    MAX_SPLIT_LIST,
    Manifest,
    ManifestError,
    SplitSpec,
    parse_manifest_data,
    parse_manifest_text,
)

BASE: dict[str, Any] = {"name": "s", "entrypoint": "python run.py {items_file} {out}"}


def _parse(**fields: Any) -> Manifest:
    return parse_manifest_data({**BASE, **fields})


def _problems(**fields: Any) -> list[tuple[str, str]]:
    with pytest.raises(ManifestError) as info:
        _parse(**fields)
    return info.value.problems


def _roundtrip(manifest: Manifest) -> None:
    again = parse_manifest_data(json.loads(manifest.canonical_json()))
    assert again == manifest
    assert again.canonical_json() == manifest.canonical_json()


# ------------------------------------------------------------- hash stability

# SHA-256 of canonical_json() computed with the code BEFORE V0.10 G1 (commit
# 8da7218): adding `split` must not change any of them.
OLD_HASHES = [
    (
        {"entrypoint": "python main.py"},
        "99bd18169f8d3eb61359e88076b8e25da3055eae8d7deee2ef15095f5848bba4",
    ),
    (
        {
            "name": "img",
            "entrypoint": ["python", "run.py", "--fmt", "x"],
            "outputs": ["results/"],
        },
        "c64c9503e7df15beb9f724cd927f3cc1030b4d20f04fef3b8ff07f28a06ab5ea",
    ),
    (
        {
            "name": "full",
            "entrypoint": "python train.py --epochs 3",
            "env": {
                "base": "cuda-pytorch",
                "python": "3.12",
                "requirements": "requirements.txt",
            },
            "resources": {"gpu": "preferred", "vram_gb": 4, "ram_gb": 8, "cpus": 4},
            "outputs": ["results/", "*.csv"],
            "inputs": [{"ref": "imagenet-mini@3", "mount": "data/imagenet"}],
            "limits": {"timeout": "2h", "network": "on"},
            "priority": 3,
            "policy": "cost_model",
            "retry": {"max_attempts": 2, "backoff_s": 0, "on_lost": False},
            "secrets": ["API_KEY", {"name": "DB_PASS", "as_env": True}],
        },
        "8283b927b7b18d08ca06d153511bc06dafc39d0132dbd4dbd4f4a72d8a4b9f3b",
    ),
]


@pytest.mark.parametrize(("data", "expected"), OLD_HASHES)
def test_manifests_without_split_hash_exactly_as_before(
    data: dict[str, Any], expected: str
) -> None:
    manifest = parse_manifest_data(data)
    assert "split" not in manifest.canonical_dict()
    digest = hashlib.sha256(manifest.canonical_json().encode("utf-8")).hexdigest()
    assert digest == expected


# ------------------------------------------------------------- every over kind


@pytest.mark.parametrize(
    ("over", "kind"),
    [
        ({"glob": "images/**/*.jpg"}, "glob"),
        ("images/*.jpg", "glob"),  # bare string = glob
        ({"list": ["a", "b c", "ü"]}, "list"),
        ({"list_file": "items.txt"}, "list_file"),
        ({"range": {"stop": 10}}, "range"),
        ({"range": {"start": 10, "stop": -10, "step": -3}}, "range"),
        ({"lines": "data/rows.csv"}, "lines"),
        ({"lines": "data/rows.csv", "header": True}, "lines"),
    ],
)
def test_every_over_kind_validates_and_round_trips(over: Any, kind: str) -> None:
    manifest = _parse(split={"over": over})
    assert manifest.split is not None
    assert manifest.split.over.kind == kind
    _roundtrip(manifest)
    canonical = manifest.canonical_dict()["split"]["over"]
    assert list(canonical) == ([kind, "header"] if kind == "lines" else [kind])


def test_defaults_are_explicit_in_the_canonical_form() -> None:
    split = _parse(split={"over": "a/*"}).canonical_dict()["split"]
    assert split == {
        "over": {"glob": "a/*"},
        "chunk": "auto",
        "combine": "concat",
        "min_nodes": 2,
        "max_nodes": None,
        "speculative": True,
        "deterministic": True,
        # V0.10 G4 (D261/D278): explicit, like every other split default
        "max_chunk_output_bytes": 256 * 1024 * 1024,
    }


def test_full_spec_round_trips_through_yaml_and_json() -> None:
    text = """
name: s
entrypoint: python run.py {items_file} {chunk_index} {out}
split:
  over: {lines: data.csv, header: true}
  chunk: {items: 50}
  combine: {script: "python merge.py {parts} {out}"}
  min_nodes: 3
  max_nodes: 4
  speculative: false
  deterministic: false
"""
    manifest = parse_manifest_text(text)
    split = manifest.split
    assert split is not None
    assert split.chunk.items == 50 and not split.chunk.auto
    assert split.combine.script == ("python", "merge.py", "{parts}", "{out}")
    assert (split.min_nodes, split.max_nodes) == (3, 4)
    assert split.speculative is False and split.deterministic is False
    _roundtrip(manifest)
    assert manifest.canonical_dict()["split"]["combine"] == {
        "script": ["python", "merge.py", "{parts}", "{out}"]
    }
    assert manifest.canonical_dict()["split"]["chunk"] == {"items": 50}


@pytest.mark.parametrize("word", ["concat", "merge-csv", "merge-json"])
def test_builtin_combines(word: str) -> None:
    manifest = _parse(split={"over": "a/*", "combine": word})
    assert manifest.split is not None and manifest.split.combine.builtin == word
    _roundtrip(manifest)


def test_split_spec_hash_is_order_independent() -> None:
    a = _parse(split={"over": {"list": ["x", "y"]}, "chunk": {"items": 1}})
    b = parse_manifest_data(
        {"split": {"chunk": {"items": 1}, "over": {"list": ["x", "y"]}}, **BASE}
    )
    assert a.canonical_json() == b.canonical_json()


def test_range_count_is_computed_not_built() -> None:
    spec = SplitSpec.model_validate(
        {"over": {"range": {"start": 0, "stop": 2**53 - 1, "step": 1}}}
    )
    assert spec.over.range is not None
    assert spec.over.range.count == 2**53 - 1


# --------------------------------------------------------- refusals with paths


@pytest.mark.parametrize(
    ("split", "path", "fragment"),
    [
        ({"over": {"glob": "a", "lines": "b"}}, "split.over", "exactly one"),
        ({"over": {}}, "split.over", "exactly one"),
        ({}, "split.over", "Field required"),
        ({"over": {"glob": "a", "header": True}}, "split.over", "lines only"),
        ({"over": "a/"}, "split.over.glob", "folder/**"),
        ({"over": "a//b"}, "split.over.glob", "plain path"),
        ({"over": "./a"}, "split.over.glob", "plain path"),
        ({"over": "../a"}, "split.over.glob", ".."),
        ({"over": "/abs/*"}, "split.over.glob", "relative"),
        ({"over": {"list_file": "a*"}}, "split.over.list_file", "not a glob"),
        ({"over": {"lines": "C:/x.csv"}}, "split.over.lines", "relative"),
        ({"over": {"list": []}}, "split.over.list", "at least 1"),
        ({"over": {"list": ["a", "a"]}}, "split.over.list", "repeats"),
        ({"over": {"list": [""]}}, "split.over.list", "empty"),
        ({"over": {"list": ["  "]}}, "split.over.list", "blank"),
        ({"over": {"list": ["a\x00"]}}, "split.over.list", "NUL"),
        ({"over": {"list": ["x" * 1025]}}, "split.over.list", "1024"),
        ({"over": {"list": [1]}}, "split.over.list[0]", "string"),
        ({"over": {"range": {"stop": 5, "step": 0}}}, "split.over.range.step", "0"),
        ({"over": {"range": {"stop": 2**53}}}, "split.over.range.stop", ""),
        ({"over": {"range": {"stop": "5"}}}, "split.over.range.stop", ""),
        ({"over": {"range": {"start": 1}}}, "split.over.range.stop", "required"),
        ({"over": "a", "chunk": "big"}, "split.chunk", "auto or"),
        ({"over": "a", "chunk": {"items": 0}}, "split.chunk.items", ""),
        ({"over": "a", "chunk": {"items": None}}, "split.chunk", "auto or"),
        ({"over": "a", "chunk": {"items": 100_001}}, "split.chunk.items", ""),
        ({"over": "a", "combine": "zip"}, "split.combine", "concat"),
        ({"over": "a", "combine": {"builtin": "concat"}}, "split.combine", "word"),
        ({"over": "a", "combine": {"script": []}}, "split.combine.script", ""),
        ({"over": "a", "combine": {"script": "a 'b"}}, "split.combine.script", "split"),
        ({"over": "a", "min_nodes": 1}, "split.min_nodes", ""),
        ({"over": "a", "max_nodes": 65}, "split.max_nodes", ""),
        ({"over": "a", "min_nodes": 3, "max_nodes": 2}, "split", "at least min_nodes"),
        ({"over": "a", "speculative": "yes"}, "split.speculative", ""),
        ({"over": "a", "bogus": 1}, "split.bogus", "unknown key"),
        ({"over": {"glob": "a", "extra": 1}}, "split.over.extra", "unknown key"),
    ],
)
def test_bad_split_specs_name_their_yaml_path(
    split: dict[str, Any], path: str, fragment: str
) -> None:
    problems = _problems(split=split)
    assert any(p == path and fragment in m for p, m in problems), problems


def test_list_cap() -> None:
    items = [f"i{n}" for n in range(MAX_SPLIT_LIST + 1)]
    problems = _problems(split={"over": {"list": items}})
    assert problems[0][0] == "split.over.list"
    ok = _parse(split={"over": {"list": items[:MAX_SPLIT_LIST]}})
    assert ok.split is not None and ok.split.over.list is not None


def test_split_must_be_a_mapping() -> None:
    assert _problems(split="a/*")[0][0] == "split"


# ---------------------------------------------------------------- placeholders


@pytest.mark.parametrize("token", ["{items_file}", "{chunk_index}", "{out}", "{parts}"])
def test_placeholders_are_literal_text_in_a_plain_job(token: str) -> None:
    # G1 review item 1: nothing is substituted outside a split job, so a job
    # queued before V0.10 with such text keeps validating and running.
    m = parse_manifest_data({"name": "x", "entrypoint": ["run", f"--x={token}"]})
    assert m.entrypoint == ("run", f"--x={token}")


def test_parts_is_for_the_combine_script_only() -> None:
    problems = _problems(entrypoint="run {parts}", split={"over": "a"})
    assert problems == [
        (
            "entrypoint",
            "the placeholder {parts} is only substituted in "
            "split.combine.script, not the entrypoint",
        )
    ]


@pytest.mark.parametrize("token", ["{items_file}", "{chunk_index}"])
def test_chunk_placeholders_are_refused_in_the_combine_script(token: str) -> None:
    problems = _problems(split={"over": "a", "combine": {"script": ["m", token]}})
    assert problems[0][0] == "split.combine.script"


def test_split_entrypoint_may_use_env_vars_instead_of_placeholders() -> None:
    manifest = _parse(entrypoint="python run.py", split={"over": "a"})
    assert manifest.entrypoint == ("python", "run.py")


def test_bad_split_does_not_hide_a_bad_entrypoint() -> None:
    with pytest.raises(ManifestError) as info:
        parse_manifest_data({"name": "x", "entrypoint": "", "split": {"over": {}}})
    paths = [p for p, _ in info.value.problems]
    assert "entrypoint" in paths and "split.over" in paths


def test_model_validate_accepts_the_stored_canonical_dict() -> None:
    manifest = _parse(split={"over": {"range": {"stop": 3}}, "chunk": {"items": 2}})
    stored = manifest.canonical_dict()
    assert Manifest.model_validate(stored) == manifest
