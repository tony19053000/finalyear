# ruff: noqa: S311, PERF401, E501, RUF100
"""Corpus generator for the V0.8 G4 tester r1 YAML differential (browser parser vs the CLI loader).

``build()`` returns cases ``{"text", "expect"}``; ``expect`` is ``{"ok": value}``,
``{"error": true}`` (CLI loader refuses) or ``{"exotic": true}`` (loads to a value JSON
cannot carry - date, nan, inf, bytes, set...). The browser parser must either match
``ok`` exactly or refuse; it must never return a different value.
"""

from __future__ import annotations

import datetime
import json
import math
import random
from typing import Any

import yaml

from meshforge.common.manifest import _StrictSafeLoader  # noqa: PLC2701


def classify(text: str) -> dict[str, Any]:
    try:
        value = yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506
    except Exception:  # noqa: BLE001
        return {"error": True}
    try:
        _check(value)
    except (TypeError, UnicodeEncodeError):
        return {"exotic": True}
    return {"ok": value}


def _check(v: Any) -> None:
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                raise TypeError
            _check(k)
            _check(x)
    elif isinstance(v, list):
        for x in v:
            _check(x)
    elif isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise TypeError
    elif isinstance(v, str):
        v.encode("utf-8")  # lone surrogates -> UnicodeEncodeError
    elif v is None or isinstance(v, (bool, int)):
        if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2**53:
            raise TypeError
    elif isinstance(v, (datetime.date, bytes, set)):
        raise TypeError
    else:
        raise TypeError


SCALARS = [
    "yes",
    "No",
    "ON",
    "off",
    "y",
    "n",
    "Y",
    "N",
    "true",
    "False",
    "~",
    "null",
    "Null",
    "NULL",
    "",
    "0",
    "-0",
    "+1",
    "007",
    "0o7",
    "08",
    "09",
    "0x1F",
    "0X1f",
    "0b101",
    "1_000",
    "1__0",
    "_1",
    "1_",
    "1:30",
    "1:60",
    "1:30:15",
    "-1:30",
    "1:30.5",
    "3.10",
    "3.",
    ".5",
    "-.5",
    "1e3",
    "1E3",
    "1e+3",
    "1.0e3",
    "1.5e-3",
    ".inf",
    "-.INF",
    ".nan",
    ".NaN",
    "0.1.2",
    "1.2.3",
    "1,000",
    "12:30",
    "2024-01-02",
    "2024-1-2 3:04:05",
    "2024-01-02T03:04:05Z",
    "<<",
    "=",
    "a b",
    "a#b",
    "a #b",
    "a:b",
    "a: b",
    "a :b",
    "-x",
    "- x",
    "-",
    "--",
    "---x",
    "?x",
    "? x",
    "@x",
    "`x",
    "%x",
    "&a",
    "*a",
    "!t",
    "!!str x",
    "|",
    ">",
    "|-",
    ">+",
    "[",
    "]",
    "{",
    "}",
    "[a",
    "a]",
    "'a'",
    '"a"',
    "'a''b'",
    '"a\\"b"',
    '"\\x41"',
    '"\\u00e9"',
    '"\\U0001F600"',
    '"\\q"',
    '"\\0"',
    '"a\\\nb"',
    "'unterminated",
    '"unterminated',
    "'a' b",
    '"a" # c',
    "'a'#c",
    "a\tb",
    "é",
    "日本語",
    "😀",
    "1e3x",
    "0x",
    "0b",
    "0b2",
    "0_7",
    "0.",
    "+.5",
    "+.inf",
    "1_0.5",
    "1_0:5",
    "100:00:00",
    "9007199254740993",
    "-9007199254740993",
    "99999999999999999999",
    "0.30000000000000004",
    "1.7976931348623157e308",
    "1e400",
    "1.0e400",
    "0e0",
    "0.0e+0",
    "True",
    "tRue",
    "YES",
    "yEs",
    "nULL",
    "Off",
    "oN",
    "~x",
    "x~",
    "null ",
    " null",
    "'null'",
    "'~'",
    '"yes"',
    "{}",
    "[]",
    "{a: 1}",
    "[1, 2]",
    "[a, [b, c]]",
    "{a: [1, 2], b: {c: d}}",
    "[a,b,]",
    "[,a]",
    "[a,,b]",
    "{a}",
    "{a: }",
    "{: a}",
    "{a: 1, a: 2}",
    "{'a': 1}",
    "{a: 'x, y'}",
    "[a: b]",
    "[a, b: c]",
    "{a: b: c}",
    "[1, 2",
    "[yes, no, ~]",
    "{yes: 1}",
    "{1: a}",
    "{~: a}",
    "[\"a\", 'b', c]",
    "[a # c\n, b]",
    "[a,\n b]",
    "{a: 1,\n b: 2}",
    "http://x.y/z",
    "a:b:c",
    "C:\\x\\y",
    "a\\b",
    "a'b",
    'a"b',
    "it's",
    "50%",
    "$x",
    "$(x)",
    "a;b",
    "a|b",
    "a>b",
]

KEYS = [
    "name",
    "yes",
    "no",
    "on",
    "off",
    "null",
    "~",
    "1",
    "0x10",
    "1.5",
    "a b",
    "a:b",
    "'q k'",
    '"d k"',
    "é",
    "true",
    "<<",
    "=",
    "-",
    "?",
    "2024-01-02",
    "'1'",
    "x#y",
    "run",
    "env",
    "inputs",
    "outputs",
    "resources",
    "gpu",
]


def hand() -> list[str]:
    out: list[str] = []
    for s in SCALARS:
        out.append(f"k: {s}\n")
        out.append(f"- {s}\n")
        out.append(f"k:\n  - {s}\n  - {s}\n")
        out.append(f"k:\n- {s}\n")
        out.append(f"a:\n  b: {s}\n")
        out.append(f"{s}\n")
    for k in KEYS:
        out.append(f"{k}: v\n")
        out.append(f"{k}: v\n{k}: w\n")
        out.append(f"{k}: 1\nother: 2\n")
        out.append(f"? {k}\n: v\n")
        out.append(f"- {k}: v\n  z: 1\n")
    out += [
        "",
        "\n\n",
        "# only comment\n",
        "---\n",
        "---\na: 1\n",
        "---\na: 1\n---\nb: 2\n",
        "a: 1\n...\n",
        "--- a\n",
        "--- |\n  x\n",
        "%YAML 1.1\n---\na: 1\n",
        "a: 1\r\nb: 2\r\n",
        "a: 1\rb: 2\r",
        "\ufeffa: 1\n",
        "a: 1\n\tb: 2\n",
        "a:\n\tb: 2\n",
        "a: 1\n b: 2\n",
        "a:\n  b: 1\n c: 2\n",
        "a:\n  b: 1\n    c: 2\n",
        "a:\n    b: 1\n  c: 2\n",
        "a:\n  - 1\n - 2\n",
        "a: &x 1\nb: *x\n",
        "a: &x [1]\n",
        "a: !!str 1\n",
        "a: !!int '1'\n",
        "a: !!python/object:os.system x\n",
        "a: |\n  text\nb: 1\n",
        "a: >\n  text\n",
        "a: |-\n",
        "a: 'multi\n  line'\n",
        'a: "multi\n  line"\n',
        "a: plain\n  continued\n",
        "a: plain\n  continued: x\n",
        "a:\n  b\n",
        "a:\n  b\n  c\n",
        "a: b: c\n",
        "a: b:c\n",
        "a: [b]: c\n",
        "- - a\n  - b\n",
        "-\n  - a\n",
        "- a\n-\n- b\n",
        "- a: 1\n  b: 2\n- c: 3\n",
        "- a: 1\n   b: 2\n",
        "- a: 1\n b: 2\n",
        "-   a: 1\n    b: 2\n",
        "- {a: 1}\n- [b]\n",
        "a: 1 # c\nb: 'x # y' # z\n",
        "a: x#y\n",
        "a: x #y\n",
        "a: 'x'#y\n",
        'a: "x" #y\n',
        "a:#b\n",
        "a:\n#c\n  b: 1\n",
        "a: \n  b: 1\n",
        "'a b': 1\n",
        '"a b": 1\n',
        "'a': 1\n'a': 2\n",
        "a: 1\n'a': 2\n",
        "1: a\n",
        "1: a\n'1': b\n",
        "yes: 1\n'yes': 2\n",
        "true: 1\nTrue: 2\n",
        "[a, b]\n",
        "{a: 1}\n",
        "'x'\n",
        '"x"\n',
        "x\n",
        "42\n",
        "null\n",
        "[a, b]: c\n",
        "{a: 1}: c\n",
        "a: [\n  1,\n  2\n]\n",
        "a: [\n  1,\n  2\n  ]\nb: 1\n",
        "a: {\n  b: 1\n}\n",
        "a: [1, 2] x\n",
        "a: {b: 1} # c\n",
        "a: [1, 2]\nb: [3]\n",
        "a: 'it''s'\n",
        'a: "tab\\there"\n',
        'a: "\\ud83d\\ude00"\n',
        'a: "\\U0010FFFF"\n',
        'a: "\\U00110000"\n',
        'a: "\\ud800"\n',
        "a: '\\n'\n",
        "a: x\x00y\n",
        "a: \x07\n",
        "a: \u2028x\n",
        "a: x\u2028y\n",
        "a: \u0085\n",
        "a: \xa0x\n",
        "a:\u3000b\n",
        "a: 1\nb:\n  - x\n  - y\nc: {d: e}\n",
        "a:\n  b:\n    c:\n      d: 1\n",
        "a:\n  - b:\n      - c\n",
        "compute:\n  name: x\n  run: python x.py\n  env:\n    A: 'yes'\n    B: yes\n    C: 010\n",
        "name: demo\nrun: [python, -c, 'print(1)']\ninputs:\n  - path: data\n    as_env: true\n",
        "__proto__: 1\n",
        "constructor: 1\nprototype: 2\n",
        "toString: x\nhasOwnProperty: y\n",
        "a:\n  __proto__:\n    x: 1\n",
        "{__proto__: 1}\n",
        "{a: 1, __proto__: {x: 1}}\n",
        "- __proto__: 1\n",
        "__proto__:\n  - 1\n",
    ]
    return out


def randoms(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    cases: list[str] = []

    def scalar() -> str:
        return rng.choice(SCALARS)

    def flow(depth: int = 0) -> str:
        r = rng.random()
        if depth > 2 or r < 0.5:
            return scalar().split("\n")[0]
        if r < 0.75:
            return (
                "[" + ", ".join(flow(depth + 1) for _ in range(rng.randint(0, 3))) + "]"
            )
        return (
            "{"
            + ", ".join(
                f"{rng.choice(KEYS)}: {flow(depth + 1)}"
                for _ in range(rng.randint(0, 3))
            )
            + "}"
        )

    def block(indent: int, depth: int) -> list[str]:
        pad = " " * indent
        lines: list[str] = []
        if rng.random() < 0.4:
            for _ in range(rng.randint(1, 3)):
                if depth < 2 and rng.random() < 0.3:
                    lines.append(f"{pad}- {rng.choice(KEYS)}: {flow()}")
                    lines.append(f"{pad}  {rng.choice(KEYS)}: {flow()}")
                else:
                    lines.append(f"{pad}- {flow()}")
        else:
            for _ in range(rng.randint(1, 4)):
                k = rng.choice(KEYS)
                if depth < 2 and rng.random() < 0.3:
                    lines.append(f"{pad}{k}:")
                    lines += block(
                        indent + rng.choice([1, 2, 4])
                        if rng.random() < 0.9
                        else indent,
                        depth + 1,
                    )
                else:
                    c = rng.choice(["", "", " # note", "  #n"])
                    lines.append(f"{pad}{k}: {flow()}{c}")
        return lines

    for _ in range(n):
        text = "\n".join(block(0, 0)) + "\n"
        if rng.random() < 0.15:
            text = text.replace("\n", "\r\n")
        cases.append(text)
    # character-level mutations of valid docs
    base = cases[: n // 4]
    for t in base:
        if not t:
            continue
        i = rng.randrange(len(t))
        op = rng.choice(["del", "dup", "ins"])
        ch = rng.choice(list(" :-#'\"[]{},&*!|>?%\t\n"))
        t2 = (
            t[:i] + t[i + 1 :]
            if op == "del"
            else t[:i] + t[i] + t[i:]
            if op == "dup"
            else t[:i] + ch + t[i:]
        )
        cases.append(t2)
    return cases


def build() -> list[dict[str, Any]]:
    texts = list(dict.fromkeys(hand() + randoms(3000, 20261016)))
    return [{"text": t, "expect": classify(t)} for t in texts]


if __name__ == "__main__":
    import sys

    json.dump(build(), sys.stdout, ensure_ascii=False)
