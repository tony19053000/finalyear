# ruff: noqa: S311, PERF401, E501, RUF005, RUF100
"""Tester r2 (V0.8 G4): fresh, more adversarial differential corpora (new seeds, new generators).

walk: small name pool + many ignore files per tree + negation-heavy pattern pool so that rule
ORDER (pathspec last-to-first), folder markers, nested .gitignore/.computeignore interplay bite.
yaml: token soup from the YAML grammar alphabet + unicode-space/control injection into valid docs.
Run:  python tests/_support/v08_g4_r2_corpus.py walk|yaml > file.json
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v08_g4_walk_corpus as w  # noqa: E402
import v08_g4_yaml_corpus as y  # noqa: E402

WALK_SEED = 4242424
YAML_SEED = 9090909

DIRS = ["a", "b", "src", "keep", "x y", "d.log", "build"]
LEAF = [
    "f.txt",
    "g.log",
    "keep.log",
    "h.py",
    ".computeignore",
    ".gitignore",
    "k",
    "z.tmp",
]
RULES = [
    "*",
    "!*",
    "*/",
    "!*/",
    "**",
    "!**",
    "**/",
    "!**/",
    "/**",
    "!/**",
    "a/",
    "!a/",
    "a",
    "!a",
    "a/**",
    "!a/**",
    "a/*",
    "!a/*",
    "**/a",
    "!**/a",
    "**/a/**",
    "*.log",
    "!*.log",
    "!keep.log",
    "keep.log",
    "src/",
    "!src/",
    "src/**",
    "!src/**",
    "src/*",
    "!src/*",
    "/src",
    "!/src",
    "b/",
    "!b/f.txt",
    "b/f.txt",
    "**/f.txt",
    "!**/f.txt",
    "!.computeignore",
    "!.gitignore",
    ".computeignore",
    ".gitignore",
    "*.tmp",
    "!*.tmp",
    "f.*",
    "!f.*",
    "?",
    "!?",
    "k",
    "!k",
    "x y/",
    "!x y/",
    "x\\ y/",
    "trail\\ ",
    "d.log/",
    "!d.log/",
    "d.log",
    "build/",
    "!build/",
    "#c",
    "\\#c",
    "\\!k",
    "[a-b]/",
    "![a-b]/",
    "*.[lt]*",
    "!*.[lt]*",
    "a/**/f.txt",
    "!a/**/f.txt",
    "**/b/**/g.log",
    "!**/b/",
    "**/keep/",
    "/keep",
    "!/keep/**",
]


def walk_case(rng: random.Random) -> dict[str, Any]:
    files: dict[str, str] = {}
    for _ in range(rng.randint(4, 16)):
        depth = rng.randint(0, 3)
        path = "/".join([rng.choice(DIRS) for _ in range(depth)] + [rng.choice(LEAF)])
        files[path] = rng.choice(["x", "yy", ""])
    dirs = sorted({str(Path(p).parent) for p in files if "/" in p}) + [""]
    for _ in range(rng.randint(1, 6)):
        where = rng.choice(dirs)
        name = rng.choice([".gitignore", ".gitignore", ".computeignore"])
        lines = [rng.choice(RULES) for _ in range(rng.randint(1, 8))]
        files[(where + "/" if where not in ("", ".") else "") + name] = rng.choice(
            ["\n", "\r\n"]
        ).join(lines) + rng.choice(["\n", ""])
    keep: dict[str, str] = {}
    for p in sorted(files, key=len):
        parts = p.split("/")
        if any("/".join(parts[:i]) in keep for i in range(1, len(parts))):
            continue
        if any(q.startswith(p + "/") for q in keep):
            continue
        keep[p] = files[p]
    return {"files": keep}


def order_cases() -> list[dict[str, Any]]:
    """Hand-picked precedence cases: last-to-first, negations, nested/both ignore files, BOM, CRLF."""
    base = {
        "a.log": "1",
        "keep.log": "2",
        "d/b.log": "3",
        "d/keep.log": "4",
        "m.py": "5",
        "build/x.txt": "6",
        "build/keep.txt": "7",
    }
    pats = [
        "*.log\n!keep.log\n",
        "!keep.log\n*.log\n",
        "*.log\n!keep.log\n*.log\n",
        "*.log\n!*.log\n*.log\n",
        "build/\n!build/keep.txt\n",
        "build/*\n!build/keep.txt\n",
        "build/**\n!build/keep.txt\n",
        "*\n!*/\n!*.py\n",
        "*\n!m.py\n",
        "/*\n!/m.py\n",
        "**\n!**/keep.*\n",
        "d/\n!d/keep.log\n",
        "d/**\n!d\n",
        "\ufeff*.log\n",
        "*.log\r\n!keep.log\r\n",
        "*.log  \n!keep.log \n",
        "*.log\\ \n",
        "# c\n\\#x\n!keep.log\n",
        "!keep.log\n",
        "",
        "\n\n   \n",
        "*.LOG\n",
        "**/d/\n",
        "**/build/**\n!**/keep.txt\n",
    ]
    out: list[dict[str, Any]] = []
    for p in pats:
        out.append({"files": {**base, ".gitignore": p}})
        out.append({"files": {**base, ".computeignore": p}})
        out.append({"files": {**base, "d/.gitignore": p}})
        out.append({"files": {**base, ".gitignore": "*.log\n", "d/.gitignore": p}})
        out.append(
            {"files": {**base, ".gitignore": p, ".computeignore": "!keep.log\n"}}
        )
        out.append(
            {
                "files": {
                    **base,
                    ".gitignore": p,
                    "d/.computeignore": "*.py\n!keep.log\n",
                    "build/.gitignore": "!*\n",
                }
            }
        )
    out.append(
        {
            "files": {
                "caf\u00e9/a.txt": "1",
                "cafe\u0301/b.txt": "2",
                ".gitignore": "caf\u00e9/\n",
            }
        }
    )
    out.append({"files": {"a.txt": "1", "a\\b.txt": "2"}})
    return out


def walk(n: int, seed: int = WALK_SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        c = walk_case(rng)
        out.append({**c, "expect": w.verdict(c)})
    for c in order_cases():
        out.append({**c, "expect": w.verdict(c)})
    return out


UNI_WS = [
    "\u00a0",
    "\u3000",
    "\u2028",
    "\u2029",
    "\u0085",
    "\u2003",
    "\u200b",
    "\ufeff",
    "\x00",
    "\x07",
    "\t",
    "\x0b",
    "\x0c",
    "\x1f",
    "\x7f",
]
ALPHA = list("abc01 :-#'\"[]{},&*!|>?%@`\\\n\t.~=<") + [
    "  ",
    "\n  ",
    "- ",
    ": ",
    "? ",
    "yes",
    "null",
    "1e3",
    "0x1f",
    "!!str ",
    "&a ",
    "*a ",
    "<<",
    "---\n",
    "...\n",
    "\u00e9",
    "\U0001f600",
    "\\u00e9",
    "\\x41",
    "\\U00110000",
    "\\ud800",
]


def yaml_texts(n: int, seed: int = YAML_SEED) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    base = y.randoms(max(200, n // 4), seed)
    for t in base:
        out.append(t)
        for _ in range(3):
            if not t:
                continue
            i = rng.randrange(len(t) + 1)
            ch = rng.choice(UNI_WS)
            out.append(t[:i] + ch + t[i:])
    while len(out) < n:
        k = rng.randint(1, 14)
        s = "".join(rng.choice(ALPHA) for _ in range(k))
        # make a plausible mapping most of the time
        out.append(
            rng.choice(["", "k: ", "- ", "k:\n  ", "{", "["])
            + s
            + rng.choice(["", "\n", "}", "]"])
        )
    return list(dict.fromkeys(out))


def yaml_cases(n: int, seed: int = YAML_SEED) -> list[dict[str, Any]]:
    return [{"text": t, "expect": y.classify(t)} for t in yaml_texts(n, seed)]


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "walk":
        json.dump(
            walk(int(sys.argv[2]) if len(sys.argv) > 2 else 1200),
            sys.stdout,
            ensure_ascii=False,
        )
    else:
        json.dump(
            yaml_cases(int(sys.argv[2]) if len(sys.argv) > 2 else 12000),
            sys.stdout,
            ensure_ascii=False,
        )
