"""Tester round 1 (V0.4 G2): manifest hostile-input cases beyond the implementer suite.

Unicode confusables masquerading as known keys, astronomically large integer
durations, tab-indented YAML, and a manifest whose YAML *parses* but whose
top-level document is a list/scalar rather than a mapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.manifest import (
    MAX_TIMEOUT_S,
    Manifest,
    ManifestError,
    parse_duration,
    parse_manifest_data,
    parse_manifest_text,
)

BASE: dict[str, Any] = {"entrypoint": ["python", "main.py"]}


def _parse(**fields: Any) -> Manifest:
    return parse_manifest_data({**BASE, **fields})


# ------------------------------------------------------- unicode confusables


def test_cyrillic_lookalike_key_is_an_unknown_key_not_a_silent_alias() -> None:
    # A key spelled with a Cyrillic "e" (U+0435) instead of Latin "e": looks
    # like "name" but is a different codepoint sequence; Pydantic's exact key
    # match must refuse it as unknown, never silently treat it as (or merge it
    # with) "name".
    spoofed = "nam" + chr(0x0435)
    data = {**BASE, spoofed: "spoofed"}
    with pytest.raises(ManifestError) as info:
        parse_manifest_data(data)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert any(spoofed in path for path, _ in info.value.problems)


def test_fullwidth_lookalike_top_level_key_is_unknown() -> None:
    # FULLWIDTH LATIN letters spelling "name" -- visually similar, not equal.
    spoofed_name = "".join(chr(0xFF00 + ord(c) - 0x20) for c in "name")
    data = {**BASE, spoofed_name: "x"}
    with pytest.raises(ManifestError):
        parse_manifest_data(data)


# ------------------------------------------------------------- huge integers


@pytest.mark.parametrize("huge", [10**30, 2**128, -(10**30)])
def test_astronomically_large_int_timeout_is_bounded_not_a_crash(huge: int) -> None:
    with pytest.raises(ValueError):
        parse_duration(huge)
    with pytest.raises(ManifestError) as info:
        _parse(limits={"timeout": huge})
    assert info.value.failure_type is FailureType.INVALID_INPUT


def test_huge_int_timeout_via_full_manifest_parse_never_hangs() -> None:
    with pytest.raises(ManifestError):
        _parse(limits={"timeout": 10**9999})


def test_timeout_bound_max_ok_and_max_plus_one_as_huge_int() -> None:
    _parse(limits={"timeout": MAX_TIMEOUT_S})
    with pytest.raises(ManifestError):
        _parse(limits={"timeout": MAX_TIMEOUT_S + 10**20})


# --------------------------------------------------------------- tab-indented


def test_tab_indented_yaml_is_a_located_parse_error() -> None:
    text = "entrypoint: python main.py\nresources:\n\tgpu: required\n"
    with pytest.raises(ManifestError) as info:
        parse_manifest_text(text)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert "line" in info.value.message


def test_tab_character_inside_a_quoted_scalar_is_accepted_as_a_literal_tab() -> None:
    # A tab INSIDE a value (not used for indentation) is legal YAML; make sure the
    # loader does not conflate "tabs are illegal anywhere" with "tabs cannot
    # indent a block". shlex.split then treats the tab as argv whitespace (POSIX
    # rules, as documented) -- the tab is consumed as a separator, not smuggled
    # into an argument, which is the "no shell metacharacter surprises" contract.
    text = 'entrypoint: "python\tmain.py"\n'
    manifest = parse_manifest_text(text)
    assert manifest.entrypoint == ("python", "main.py")


# ---------------------------------------------------- non-mapping documents


@pytest.mark.parametrize(
    "text",
    [
        "- a\n- b\n",  # a YAML list at the top level
        "just a scalar string\n",
        "42\n",
    ],
)
def test_non_mapping_top_level_document(text: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse_manifest_text(text)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert any(path == "<root>" for path, _ in info.value.problems)


@pytest.mark.parametrize("text", ["null\n", "\n"])
def test_null_or_empty_document_means_no_manifest_and_infers_cleanly(
    text: str,
) -> None:
    # `null`/an empty document is YAML for "nothing" -- treated the same as no
    # manifest at all (falls through to `data = {}`), so it fails on missing
    # entrypoint, not on "must be a mapping". Different code path, same safe
    # outcome: no confidently-wrong output either way.
    with pytest.raises(ManifestError, match="entrypoint"):
        parse_manifest_text(text)


# --------------------------------------------------------------- NUL bytes


def test_nul_byte_in_manifest_text_is_refused_cleanly() -> None:
    with pytest.raises(ManifestError):
        parse_manifest_text("entrypoint: python\x00main.py\n")
