# ruff: noqa: RUF001, S311
"""V0.9 G4 lane B tester round 1: the condition grammar under hostile input (0.9.5).

Derived from the 0.9.5 criteria (brief §5e, D241): one canonical spelling per
condition, bounded arguments, nothing but ``ConditionError`` ever escapes, and
what parses re-parses to itself (the agent re-validates by the same function).
"""

from __future__ import annotations

import random
import string

import pytest

from meshforge.common.bench_conditions import (
    CONDITION_MAX_CHARS,
    ConditionError,
    ConditionKind,
    advertised_kinds,
    capability_tag,
    parse_condition,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "NONE",
        "None",
        "none:",
        "none:1",
        "none ",
        " none",
        "none\n",
        "cpu_load",
        "cpu_load:",
        "cpu_load:0",
        "cpu_load:257",
        "cpu_load:-1",
        "cpu_load:+2",
        "cpu_load:2.0",
        "cpu_load:2 ",
        "cpu_load: 2",
        "cpu_load:2\n",
        "cpu_load:٢",  # arabic-indic digit two
        "cpu_load:２",  # fullwidth digit two
        "cpu_load:²",  # superscript two
        "cpu_load:1_0",
        "cpu_load:0x10",
        "cpu_load:1e1",
        "cpu_load:9999999",
        "cpu_load:2:3",
        "cpu_load:2,3",
        "CPU_LOAD:2",
        "cpu-load:2",
        "gpu_load:9",
        "gpu_load:101",
        "gpu_load:0",
        "gpu_load:50 %",
        "gpu_load:50%%",
        "gpu_load:%",
        "queue:0",
        "queue:33",
        "queue:-1",
        "queue:two",
        "cache:",
        "cache:hot",
        "cache:COLD",
        "cache:cold,warm",
        "netem:",
        "netem:,",
        "netem:delay",
        "netem:delay=",
        "netem:delay=50",
        "netem:delay=50us",
        "netem:delay=-5ms",
        "netem:delay=1e2ms",
        "netem:delay=nanms",
        "netem:delay=infms",
        "netem:delay=501ms",
        "netem:delay=0.51s",
        "netem:delay=50ms,delay=60ms",
        "netem:delay=50ms,",
        "netem:,delay=50ms",
        "netem:delay=50ms,,loss=1%",
        "netem:loss=20.001%",
        "netem:loss=21%",
        "netem:loss=1",
        "netem:loss=nan%",
        "netem:loss=-1%",
        "netem:rate=999kbit",
        "netem:rate=0.5mbit",
        "netem:rate=100001mbit",
        "netem:rate=10",
        "netem:rate=10MBIT",
        "netem:rate=10mbps",
        "netem:jitter=5ms",
        "netem:delay=50ms;rm -rf /",
        "netem:delay=50ms loss=1%",
        "netem:delay=$(id)ms",
        "netem:delay=50ms\x00",
        "unavailable:",
        "unavailable:0s",
        "unavailable:301s",
        "unavailable:6m",
        "unavailable:-1s",
        "unavailable:1h",
        "unavailable:1.5s",
        "unavailable:30",  # unit is optional per the grammar? see test below
        "bogus",
        "bogus:1",
        "rm -rf /",
        "cpu_load:1; sudo tc qdisc del dev eth0 root",
        "\x00",
        "cpu_load:\x001",
        "\ud800",
        "名",
        "cpu_load:" + "9" * 100,
        "x" * (CONDITION_MAX_CHARS + 1),
        "netem:" + "delay=1ms," * 20,
    ],
)
def test_hostile_text_is_either_a_condition_error_or_canonical(text: str) -> None:
    """Never another exception type; whatever is accepted is canonical-stable."""
    try:
        condition = parse_condition(text)
    except ConditionError:
        return
    canonical = condition.canonical()
    assert parse_condition(canonical).canonical() == canonical
    assert len(canonical) <= CONDITION_MAX_CHARS


@pytest.mark.parametrize(
    "text",
    [
        "",
        "NONE",
        "none ",
        "cpu_load:0",
        "cpu_load:257",
        "cpu_load:+2",
        "cpu_load:٢",
        "cpu_load:２",
        "cpu_load:2\n",
        "gpu_load:9",
        "gpu_load:101",
        "queue:0",
        "queue:33",
        "cache:hot",
        "netem:delay=501ms",
        "netem:loss=21%",
        "netem:rate=999kbit",
        "netem:delay=50ms;rm -rf /",
        "netem:delay=50ms\x00",
        "unavailable:0s",
        "unavailable:301s",
        "unavailable:6m",
        "bogus:1",
        "cpu_load:\x001",
        "\ud800",
        "x" * (CONDITION_MAX_CHARS + 1),
    ],
)
def test_out_of_range_and_lookalike_text_is_refused(text: str) -> None:
    with pytest.raises(ConditionError):
        parse_condition(text)


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("cpu_load:256", "cpu_load:256"),
        ("cpu_load:1", "cpu_load:1"),
        ("cpu_load:007", "cpu_load:7"),
        ("gpu_load:10", "gpu_load:10"),
        ("gpu_load:100%", "gpu_load:100"),
        ("queue:32", "queue:32"),
        ("netem:delay=500ms", "netem:delay=500ms"),
        ("netem:delay=0.5s", "netem:delay=500ms"),
        ("netem:loss=20%", "netem:loss=20%"),
        ("netem:rate=1mbit", "netem:rate=1mbit"),
        ("netem:rate=1000kbit", "netem:rate=1mbit"),
        ("netem:rate=1gbit", "netem:rate=1000mbit"),
        (
            "netem:rate=10mbit,loss=1%,delay=50ms",
            "netem:delay=50ms,loss=1%,rate=10mbit",
        ),
        ("unavailable:300s", "unavailable:300s"),
        ("unavailable:5m", "unavailable:300s"),
        ("unavailable:1s", "unavailable:1s"),
    ],
)
def test_boundaries_and_canonical_spellings(text: str, canonical: str) -> None:
    assert parse_condition(text).canonical() == canonical


def test_the_only_canonical_spelling_means_distinct_texts_digest_apart() -> None:
    """Two different conditions never share a canonical string."""
    seen: dict[str, str] = {}
    for text in (
        "none",
        "cpu_load:1",
        "cpu_load:2",
        "gpu_load:50",
        "queue:1",
        "queue:2",
        "cache:cold",
        "cache:warm",
        "netem:delay=50ms",
        "netem:delay=60ms",
        "netem:loss=1%",
        "unavailable:30s",
        "unavailable:31s",
    ):
        canonical = parse_condition(text).canonical()
        assert canonical not in seen, (text, seen[canonical])
        seen[canonical] = text


def test_random_fuzz_never_raises_anything_but_condition_error() -> None:
    rng = random.Random(0x6A4B)
    alphabet = string.printable + "\x00٢２²%名\ud800é"
    names = [k.value for k in ConditionKind] + ["", "x"]
    for _ in range(4000):
        arg = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        text = rng.choice(names) + rng.choice(["", ":", ":"]) + arg
        try:
            condition = parse_condition(text)
        except ConditionError:
            continue
        canonical = condition.canonical()
        assert parse_condition(canonical).canonical() == canonical


def test_structured_fuzz_of_valid_shapes_is_idempotent() -> None:
    rng = random.Random(7)
    for _ in range(2000):
        parts = []
        if rng.random() < 0.7:
            parts.append(
                f"delay={rng.choice([0, 1, 50, 500, 0.5, 12.345])}{rng.choice(['ms', 's'])}"
            )
        if rng.random() < 0.7:
            parts.append(f"loss={rng.choice([0, 0.1, 1, 20, 20.5])}%")
        if rng.random() < 0.7:
            parts.append(
                f"rate={rng.choice([1, 10, 999, 0.5, 1000])}{rng.choice(['kbit', 'mbit', 'gbit'])}"
            )
        rng.shuffle(parts)
        text = "netem:" + ",".join(parts)
        try:
            condition = parse_condition(text)
        except ConditionError:
            continue
        canonical = condition.canonical()
        assert parse_condition(canonical).canonical() == canonical
        assert condition == parse_condition(canonical)


def test_capability_tag_hostile_inputs() -> None:
    assert capability_tag([]) is None
    assert capability_tag([ConditionKind.QUEUE, ConditionKind.UNAVAILABLE]) is None
    assert (
        capability_tag(["cpu_load", "cache", "cpu_load"])
        == "bench-conditions:cache,cpu_load"
    )
    with pytest.raises(ValueError):
        capability_tag(["bogus"])
    hostile = [
        "bench-conditions:",
        "bench-conditions:,,,",
        "bench-conditions:cpu_load,bogus,\x00,netem;rm",
        "bench-conditions:queue",  # controller kind: never an agent capability
        "bench-conditions:CPU_LOAD",
        5,  # type: ignore[list-item]
        None,  # type: ignore[list-item]
    ]
    assert advertised_kinds(hostile) == {ConditionKind.CPU_LOAD}
