# ruff: noqa: S311, PERF401, E501, RUF100
"""Random project trees + ignore files with the CLI's own ``build_tree`` verdict (V0.8 G4 tester r1).

The browser walk must produce exactly the CLI's entries, ignored count and tree id, or raise
where the CLI raises.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

from meshforge.common import tree

PATTERNS = [
    "*.log",
    "*.tmp",
    "build/",
    "/build",
    "build",
    "!keep.log",
    "!*.keep",
    "**/cache/",
    "cache/**",
    "a/**/z",
    "docs/*.md",
    "!docs/readme.md",
    "*",
    "!*/",
    "!src/",
    "src/*",
    "/src/gen",
    "?.txt",
    "[ab].txt",
    "[!a].txt",
    "x\\*y",
    "\\#hash",
    "#comment",
    "  ",
    "",
    "trail ",
    "trail\\ ",
    "ünï/",
    "日本/",
    "*.PY",
    "**",
    "**/",
    "/**",
    "a/b/",
    "a/b",
    "b/a",
    "dir/**/file.txt",
    "\\!bang",
    "[",
    "a[",
    "[]a]",
    "*.[ch]",
    "nested/*/deep",
    ".hidden",
    ".*",
    "!.gitignore",
    "!.computeignore",
    "-x",
    "~tmp",
    "!",
    "\\",
]
NAMES = [
    "a",
    "b",
    "z",
    "src",
    "build",
    "cache",
    "docs",
    "dir",
    "nested",
    "ünï",
    "日本",
    "x*y",
    "#hash",
    "!bang",
    "keep",
    ".hidden",
    "a.txt",
    "b.txt",
    "c.txt",
    "k.log",
    "keep.log",
    "t.tmp",
    "readme.md",
    "x.md",
    "file.txt",
    "m.py",
    "M.PY",
    "u.keep",
    "gen",
    "deep",
    "😀",
    "sp ace",
    "trail",
    "trail ",
    "-x",
    "~tmp",
    "[a]",
    "q?",
]


def make(rng: random.Random) -> dict[str, Any]:
    files: dict[str, str] = {}
    for _ in range(rng.randint(2, 14)):
        depth = rng.randint(1, 4)
        path = "/".join(rng.choice(NAMES) for _ in range(depth))
        files[path] = rng.choice(["x", "hello", ""])
    for _ in range(rng.randint(0, 3)):
        where = rng.choice(["", "", "src/", "a/", "docs/", "nested/x/"])
        name = rng.choice([".gitignore", ".gitignore", ".computeignore"])
        files[where + name] = "\n".join(
            rng.choice(PATTERNS) for _ in range(rng.randint(1, 6))
        ) + rng.choice(["\n", "", "\r\n"])
    # a path cannot be both file and dir
    keep: dict[str, str] = {}
    for p in sorted(files, key=len):
        parts = p.split("/")
        if any("/".join(parts[:i]) in keep for i in range(1, len(parts))):
            continue
        if any(q.startswith(p + "/") for q in keep):
            continue
        keep[p] = files[p]
    return {"files": keep}


_WINDOWS_BAD_CHARS = frozenset('<>:"\\|?*') | {chr(c) for c in range(32)}
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def windows_storable(rel: str) -> bool:
    """Can NTFS hold the POSIX relative path ``rel`` under the same name?

    No ``<>:"\\|?*`` or control characters, no component ending in a space or
    a dot, no reserved device name. Cases with other names cannot be
    materialised on Windows, so the corpus checks skip them there.
    """
    for part in rel.split("/"):
        if not part or part in (".", ".."):
            continue
        if _WINDOWS_BAD_CHARS & set(part) or part[-1] in " .":
            return False
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            return False
    return True


def case_runs_here(case: dict[str, Any]) -> bool:
    """Every file name of ``case`` can exist on this OS (see :func:`windows_storable`)."""
    if sys.platform != "win32":
        return True
    names = list(case["files"]) + list(case.get("dirs", []))
    if not all(windows_storable(n) for n in names):
        return False
    # Every path *and every parent folder* must stay distinct when case is
    # folded (NTFS): `M.PY/trail` and `m.py/z` would share one folder.
    paths = {
        "/".join(parts[:i])
        for n in names
        for parts in [n.split("/")]
        for i in range(1, len(parts) + 1)
    }
    return len({p.casefold() for p in paths}) == len(paths)


def verdict(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "p"
        root.mkdir()
        for rel, text in case["files"].items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(text.encode("utf-8"))
        try:
            built = tree.build_tree(root)
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__}
        return {
            "entries": [e.to_json() for e in built.tree.entries],
            "ignored": built.ignored,
            "tree_id": str(built.tree.tree_id),
        }


def extras() -> list[dict[str, Any]]:
    """Minimal hand-made cases for the defects the random corpus found."""
    return [
        {"files": {"src/.computeignore": "**/", "src/a.txt": "x", "src/d/b.txt": "x"}},
        {"files": {".gitignore": "**/\n", "a.txt": "x", "d/b.txt": "x"}},
        {"files": {"d/.gitignore": "**/\n", "d/a.txt": "x", "d/e/b.txt": "x"}},
        {"files": {".gitignore": "*.log\n", "trail /k.log": "x", "t/k.log": "x"}},
        {"files": {".gitignore": "*.log\n", "a b/k.log": "x", "a b/m.txt": "x"}},
        {
            "files": {
                ".gitignore": "*.log\n!keep.log\n",
                "keep.log/x/k.log": "x",
                "keep.log/m.txt": "x",
            }
        },
        {"files": {".gitignore": "*.log\n", "d /k.log": "x"}},
        {"files": {".gitignore": "m.txt\n", "d /m.txt": "x", "d /n.txt": "x"}},
        {"files": {".gitignore": "/**\n", "a": "x", "d/b": "x"}},
        {"files": {"d/.gitignore": "/**\n", "d/a": "x", "d/e/b": "x"}},
        {"files": {".gitignore": "**\n!d/\n!d/**\n", "a": "x", "d/b": "x"}},
        {"files": {".gitignore": "a/**/z\n", "a/z": "x", "a/b/z": "x", "a/b/c/z": "x"}},
        {"files": {".gitignore": "**/cache/\n", "cache/x": "y", "a/cache/x": "y"}},
        {"files": {".gitignore": "!.computeignore\n", "src/.computeignore": "**/"}},
        {
            "files": {
                ".gitignore": "*.log\n*.log\ntrail\\ \n!keep.log",
                "keep.log/build/trail /k.log": "",
            }
        },
        {
            "files": {
                ".gitignore": "cache/**\n",
                "cache/x": "y",
                "cache/d/z": "y",
                "a/cache/x": "y",
            }
        },
    ]


def build(n: int = 400, seed: int = 20261017) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        case = make(rng)
        out.append({**case, "expect": verdict(case)})
    for case in extras():
        out.append({**case, "expect": verdict(case)})
    return out


if __name__ == "__main__":
    import sys

    json.dump(build(), sys.stdout, ensure_ascii=False)
