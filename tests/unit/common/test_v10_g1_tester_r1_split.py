"""V0.10 G1 tester r1: adversarial spec + resolver tests (brief 5a, D260, D273).

Derived from the brief, not the implementation: YAML-path errors, hash
stability, bytewise ordering, path escapes, caps that refuse *before*
building, ``list_file`` / ``lines`` encoding edges, determinism of the blob.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import sys
import time
import tracemalloc
import unicodedata
from pathlib import Path
from typing import IO, Any

import pytest

from meshforge.common.manifest import (
    ManifestError,
    SplitSpec,
    parse_manifest_data,
    parse_manifest_text,
)
from meshforge.common.split_items import ResolvedItems, SplitLimits, resolve
from meshforge.common.tree import BuiltTree, Tree, build_tree

pytestmark = pytest.mark.unit

BASE: dict[str, Any] = {"name": "s", "entrypoint": "python run.py {items_file} {out}"}


def _tree(
    root: Path, files: dict[str, bytes], links: dict[str, str] | None = None
) -> BuiltTree:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name, target in (links or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    return build_tree(root)


def _one_file_tree() -> Tree:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "x").write_bytes(b"x")
        return build_tree(Path(d)).tree


def _opener(*built: BuiltTree) -> Any:
    sources: dict[str, Path] = {}
    for b in built:
        sources.update(b.sources)

    def _open(sha: str) -> IO[bytes]:
        return sources[sha].open("rb")

    return _open


def _res(
    built: BuiltTree,
    over: Any,
    *,
    inputs: dict[str, BuiltTree] | None = None,
    limits: SplitLimits | None = None,
    **extra: Any,
) -> ResolvedItems:
    inputs = inputs or {}
    trees: dict[str, Tree] = {m: b.tree for m, b in inputs.items()}
    spec = SplitSpec.model_validate({"over": over, **extra})
    return resolve(spec, built.tree, trees, _opener(built, *inputs.values()), limits)


def _refused(built: BuiltTree, over: Any, **kw: Any) -> tuple[str, str]:
    with pytest.raises(ManifestError) as info:
        _res(built, over, **kw)
    assert info.value.failure_type.value == "invalid_input"
    return info.value.problems[0]


def _problems(split: Any, **top: Any) -> list[tuple[str, str]]:
    with pytest.raises(ManifestError) as info:
        parse_manifest_data({**BASE, "split": split, **top})
    return info.value.problems


# ---------------------------------------------------------------- spec / YAML


@pytest.mark.parametrize(
    "path",
    [
        "a\\b",  # Windows separator
        "C:\\x\\y.csv",
        "C:/x.csv",
        "c:x",
        "\\\\server\\share\\f",
        "/etc/passwd",
        "../etc/passwd",
        "a/../../b",
        "a/./b",
        "a//b",
        "a\nb",
        "a\x00b",
        " lead",
        "trail ",
        "\ttab",
        "x" * 5000,
    ],
)
@pytest.mark.parametrize("kind", ["glob", "list_file", "lines"])
def test_hostile_paths_are_refused_with_their_yaml_path(kind: str, path: str) -> None:
    problems = _problems({"over": {kind: path}})
    assert problems and all(p.startswith(f"split.over.{kind}") for p, _ in problems)


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("split: []\n", "split"),
        ("split: just-a-string\n", "split"),
        ("split: 5\n", "split"),
        ("split:\n  over: [a, b]\n", "split.over"),
        ("split:\n  over: {range: {stop: 1.5}}\n", "split.over.range.stop"),
        ("split:\n  over: {range: {stop: true}}\n", "split.over.range.stop"),
        ("split:\n  over: {lines: a.csv, header: yes please}\n", "split.over.header"),
        ("split:\n  over: a\n  max_nodes: 2.0\n", "split.max_nodes"),
        ("split:\n  over: a\n  min_nodes: '2'\n", "split.min_nodes"),
        ("split:\n  over: a\n  chunk: {items: 2.5}\n", "split.chunk.items"),
        ("split:\n  over: a\n  chunk: {items: '3'}\n", "split.chunk.items"),
        ("split:\n  over: a\n  chunk: {items: -1}\n", "split.chunk.items"),
        ("split:\n  over: a\n  combine: {script: [a], builtin: x}\n", "split.combine"),
        ("split:\n  over: a\n  combine: {}\n", "split.combine"),
        ("split:\n  over: a\n  deterministic: 1\n", "split.deterministic"),
        # YAML duplicate keys are refused by the strict loader
        ("split:\n  over: a\n  over: b\n", "<root>"),
    ],
)
def test_yaml_text_errors_name_the_path(text: str, path: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse_manifest_text(f"entrypoint: python r.py {{items_file}} {{out}}\n{text}")
    assert any(p == path or p.startswith(path) for p, _ in info.value.problems), (
        info.value.problems
    )


def test_null_split_is_a_plain_job_and_hashes_like_no_split() -> None:
    a = parse_manifest_data({"name": "n", "entrypoint": "python x.py"})
    b = parse_manifest_data({"name": "n", "entrypoint": "python x.py", "split": None})
    assert a.canonical_json() == b.canonical_json()
    assert "split" not in b.canonical_dict()


def test_equivalent_spellings_hash_identically_and_real_changes_do_not() -> None:
    def h(split: Any) -> str:
        m = parse_manifest_data({**BASE, "split": split})
        return hashlib.sha256(m.canonical_json().encode()).hexdigest()

    base = h({"over": "a/*"})
    assert base == h({"over": {"glob": "a/*"}})
    assert base == h({"over": "a/*", "chunk": "auto", "combine": "concat"})
    assert base == h(
        {"combine": "concat", "over": "a/*", "min_nodes": 2, "speculative": True}
    )
    for changed in (
        {"over": "a/**"},
        {"over": "a/*", "chunk": {"items": 1}},
        {"over": "a/*", "combine": "merge-csv"},
        {"over": "a/*", "min_nodes": 3},
        {"over": "a/*", "max_nodes": 4},
        {"over": "a/*", "speculative": False},
        {"over": "a/*", "deterministic": False},
    ):
        assert h(changed) != base
    # list order is significant (it is the combine order)
    assert h({"over": {"list": ["a", "b"]}}) != h({"over": {"list": ["b", "a"]}})


def test_split_presence_changes_the_hash_of_an_otherwise_equal_manifest() -> None:
    plain = parse_manifest_data({"name": "n", "entrypoint": "python x.py"})
    split = parse_manifest_data(
        {"name": "n", "entrypoint": "python x.py", "split": {"over": "a"}}
    )
    assert plain.canonical_json() != split.canonical_json()


@pytest.mark.parametrize(
    "entry",
    [
        "python x.py {out}",
        ["python", "x.py", "--out={out}"],
        "python x.py {items_file}",
        "sh -c 'python x.py {chunk_index}'",
        "python x.py {parts}",
    ],
)
def test_split_placeholders_in_a_plain_job_are_literal(entry: Any) -> None:
    # Lead ruling after G1 review item 1: literal text, not refused.
    m = parse_manifest_data({"name": "n", "entrypoint": entry})
    assert m.split is None


def test_parts_is_refused_in_a_split_entrypoint_but_out_is_allowed_in_combine() -> None:
    assert _problems({"over": "a"}, entrypoint="python x.py {parts}")
    ok = parse_manifest_data(
        {
            **BASE,
            "split": {
                "over": "a",
                "combine": {"script": ["python", "c.py", "{parts}", "{out}"]},
            },
        }
    )
    assert ok.split is not None


# ------------------------------------------------------------ ordering (glob)


@pytest.mark.skipif(
    sys.platform in ("win32", "darwin"),
    reason="needs a case-sensitive file system (NTFS/APFS fold case)",
)
def test_glob_order_is_bytewise_not_locale_nfc_nfd_and_astral(tmp_path: Path) -> None:
    nfc = unicodedata.normalize("NFC", "é.txt")
    nfd = unicodedata.normalize("NFD", "é.txt")
    names = [
        "Z.txt",
        "a.txt",
        "B.txt",
        "_.txt",
        "~.txt",
        "10.txt",
        "9.txt",
        "\uff5e.txt",
        "\U0001f600.txt",
        "\u00e9x.txt",
        nfc,
        nfd,
        "ß.txt",
        "z.txt",
    ]
    built = _tree(tmp_path / "p", {n: n.encode() for n in names})
    got = _res(built, "*.txt").items
    assert list(got) == sorted(set(names), key=lambda n: n.encode("utf-8"))
    # explicit checks: uppercase < lowercase, digits-as-text, BMP fullwidth < astral
    assert got.index("Z.txt") < got.index("a.txt")
    assert got.index("10.txt") < got.index("9.txt")
    assert got.index("\uff5e.txt") < got.index(
        "\U0001f600.txt"
    )  # UTF-16 order would differ
    assert nfc in got and nfd in got and len(got) == len(set(names))


def test_glob_is_stable_across_trees_built_in_different_creation_orders(
    tmp_path: Path,
) -> None:
    names = [f"d{i % 3}/f{i:03d}é.dat" for i in range(60)]
    digests = set()
    for run in range(4):
        shuffled = names[:]
        random.Random(run).shuffle(shuffled)  # noqa: S311
        built = _tree(tmp_path / f"p{run}", dict.fromkeys(shuffled, b"same"))
        time.sleep(0.01)
        digests.add(_res(built, "**/*.dat").digest)
    assert len(digests) == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="file names Windows cannot store (newline, ?, *, trailing space)",
)
def test_glob_literal_metacharacters_in_filenames(tmp_path: Path) -> None:
    files = {"a[1].txt": b"1", "a1.txt": b"2", "we ird\nname.txt": b"3", "q?.txt": b"4"}
    built = _tree(tmp_path / "p", files)
    assert list(_res(built, "a[[]1].txt").items) == ["a[1].txt"]
    assert "we ird\nname.txt" in _res(built, "*.txt").items
    blob = json.loads(_res(built, "*.txt").blob())
    assert "we ird\nname.txt" in blob["items"]


def test_glob_double_star_matches_nested_and_zero_folders_but_never_dirs(
    tmp_path: Path,
) -> None:
    built = _tree(
        tmp_path / "p", {"a.x": b"1", "d/b.x": b"2", "d/e/c.x": b"3", "d/e/n.y": b"4"}
    )
    assert list(_res(built, "**/*.x").items) == ["a.x", "d/b.x", "d/e/c.x"]
    assert list(_res(built, "**").items) == ["a.x", "d/b.x", "d/e/c.x", "d/e/n.y"]
    assert list(_res(built, "d/**").items) == ["d/b.x", "d/e/c.x", "d/e/n.y"]
    assert _refused(built, "d")[0] == "split.over.glob"  # a folder is not an item
    assert _refused(built, "d/e")[0] == "split.over.glob"


def test_glob_cannot_reach_hidden_escape_via_double_dots_in_the_tree(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    built = _tree(tmp_path / "p", {"in.txt": b"ok"})
    assert list(_res(built, "**").items) == ["in.txt"]
    # a pattern that tries to climb is a spec error, never a match
    with pytest.raises(Exception):  # noqa: B017
        _res(built, "../outside.txt")
    with pytest.raises(Exception):  # noqa: B017
        _res(built, "a/../../outside.txt")


def test_build_tree_itself_refuses_escaping_symlinks(tmp_path: Path) -> None:
    from meshforge.common.tree import TreeError

    (tmp_path / "secret.txt").write_text("secret")
    root = tmp_path / "p"
    root.mkdir()
    (root / "ok.txt").write_text("ok")
    for name, target in {
        "rel.txt": "../secret.txt",
        "abs.txt": str(tmp_path / "secret.txt"),
    }.items():
        os.symlink(target, root / name)
        with pytest.raises(TreeError):
            build_tree(root)
        (root / name).unlink()


def _forged(entries: list[Any]) -> Tree:
    """A tree whose manifest was NOT checked by parse_tree: the resolver must
    still never turn an escaping/odd symlink into an item (defence in depth)."""
    from meshforge.common.hashing import Digest
    from meshforge.common.tree import TreeEntry

    real = [e for e in entries if isinstance(e, TreeEntry)]
    return Tree(
        chunk_size=1 << 20,
        entries=tuple(real),
        tree_id=Digest("0" * 64),
        manifest_bytes=0,
    )


@pytest.mark.parametrize(
    ("path", "link"),
    [
        ("rel.txt", "../secret.txt"),
        ("deep/rel.txt", "../../secret.txt"),
        ("abs.txt", "/etc/passwd"),
        ("up.txt", ".."),
        ("sneaky.txt", "a/../../x"),
    ],
)
def test_forged_escaping_symlink_is_refused_not_resolved(path: str, link: str) -> None:
    from meshforge.common.tree import TreeEntry

    sha = hashlib.sha256(b"ok").hexdigest()
    tree = _forged(
        [
            TreeEntry("a", "file", sha256=sha, size=2),
            TreeEntry(path, "symlink", link=link),
        ]
    )
    spec = SplitSpec.model_validate({"over": "**"})
    with pytest.raises(ManifestError) as info:
        resolve(spec, tree, {}, lambda _s: None)  # type: ignore[arg-type,return-value]
    assert info.value.problems[0][0] == "split.over.glob"
    # and a glob that does not match the link is unaffected
    ok = resolve(SplitSpec.model_validate({"over": "a"}), tree, {}, lambda _s: None)  # type: ignore[arg-type,return-value]
    assert list(ok.items) == ["a"]


def test_a_symlink_into_a_named_input_mount_cannot_cross_trees(tmp_path: Path) -> None:
    from meshforge.common.tree import TreeEntry

    sha = hashlib.sha256(b"in").hexdigest()
    inp = _forged([TreeEntry("f.txt", "file", sha256=sha, size=2)])
    proj = _forged([TreeEntry("l.txt", "symlink", link="in/f.txt")])
    spec = SplitSpec.model_validate({"over": "l.txt"})
    with pytest.raises(ManifestError):
        resolve(spec, proj, {"in": inp}, lambda _s: None)  # type: ignore[arg-type,return-value]


def test_dangling_symlink_is_refused_only_when_matched(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"a.txt": b"a"}, {"dead.lnk": "gone.txt"})
    assert list(_res(built, "*.txt").items) == ["a.txt"]
    field, msg = _refused(built, "*.lnk")
    assert field == "split.over.glob" and "not in the tree" in msg
    # matching both: the dangling one makes the whole spec invalid, not partial
    assert _refused(built, "*")[0] == "split.over.glob"


def test_symlink_chain_hops_and_target_digest(tmp_path: Path) -> None:
    links = {f"l{i}": f"l{i + 1}" for i in range(5)}
    links["l5"] = "real.txt"
    built = _tree(tmp_path / "p", {"real.txt": b"payload"}, links)
    got = _res(built, "l0")
    assert list(got.items) == ["l0"]
    assert got.files[0].sha256 == hashlib.sha256(b"payload").hexdigest()
    deep = {f"h{i}": f"h{i + 1}" for i in range(45)}
    deep["h45"] = "real.txt"
    built2 = _tree(tmp_path / "q", {"real.txt": b"x"}, deep)
    assert "hops" in _refused(built2, "h0")[1]


def test_glob_over_symlinked_file_counts_target_bytes(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"big.bin": b"x" * 1000}, {"alias.bin": "big.bin"})
    got = _res(built, "*.bin")
    assert list(got.items) == ["alias.bin", "big.bin"]
    assert got.item_bytes == 2000


# --------------------------------------------------------------------- caps


@pytest.mark.parametrize(("n", "ok"), [(49, True), (50, True), (51, False)])
def test_item_cap_boundary_for_every_source(tmp_path: Path, n: int, ok: bool) -> None:
    limits = SplitLimits(max_items=50)
    files = {f"f{i:03d}.txt": b"x" for i in range(n)}
    files["list.txt"] = ("\n".join(f"i{i}" for i in range(n)) + "\n").encode()
    files["rows.csv"] = ("\n".join(f"r{i}" for i in range(n)) + "\n").encode()
    built = _tree(tmp_path / f"p{n}", files)
    cases: list[Any] = [
        "f*.txt",
        {"list_file": "list.txt"},
        {"lines": "rows.csv"},
        {"range": {"stop": n}},
        {"list": [f"i{i}" for i in range(n)]},
    ]
    for over in cases:
        if ok:
            assert _res(built, over, limits=limits).count == n
        else:
            field, msg = _refused(built, over, limits=limits)
            assert field.startswith("split.over") and "more than 50" in msg


@pytest.mark.parametrize(
    "rng",
    [
        {"stop": 2**53 - 1},
        {"start": -(2**53 - 1), "stop": 2**53 - 1},
        {"start": 2**53 - 1, "stop": -(2**53 - 1), "step": -1},
        {"stop": 10**15, "step": 3},
    ],
)
def test_huge_range_is_refused_instantly_without_memory(rng: dict[str, int]) -> None:
    tree = _one_file_tree()
    spec = SplitSpec.model_validate({"over": {"range": rng}})
    tracemalloc.start()
    t0 = time.perf_counter()
    with pytest.raises(ManifestError) as info:
        resolve(
            spec, tree, {}, lambda _s: (_ for _ in ()).throw(AssertionError("read"))
        )
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert info.value.problems[0][0] == "split.over.range"
    assert elapsed < 1.0 and peak < 5_000_000


@pytest.mark.parametrize(
    ("rng", "expect"),
    [
        ({"start": 5, "stop": 5}, "empty"),
        ({"start": 5, "stop": 1}, "empty"),  # wrong direction for step 1
        ({"start": 1, "stop": 5, "step": -1}, "empty"),
    ],
)
def test_empty_ranges_are_refused(rng: dict[str, int], expect: str) -> None:
    tree = _one_file_tree()
    spec = SplitSpec.model_validate({"over": {"range": rng}})
    with pytest.raises(ManifestError) as info:
        resolve(spec, tree, {}, lambda _s: None)  # type: ignore[arg-type,return-value]
    assert expect in info.value.problems[0][1]


@pytest.mark.parametrize(
    ("rng", "want"),
    [
        ({"stop": 5}, [0, 1, 2, 3, 4]),
        ({"start": 10, "stop": 0, "step": -4}, [10, 6, 2]),
        ({"start": -3, "stop": 3, "step": 2}, [-3, -1, 1]),
        ({"start": 0, "stop": 1}, [0]),
        ({"start": 2**53 - 3, "stop": 2**53 - 1}, [2**53 - 3, 2**53 - 2]),
    ],
)
def test_range_values_and_blob_keep_integers(
    rng: dict[str, int], want: list[int]
) -> None:
    tree = _one_file_tree()
    spec = SplitSpec.model_validate({"over": {"range": rng}})
    got = resolve(spec, tree, {}, lambda _s: None)  # type: ignore[arg-type,return-value]
    assert list(got.items) == want
    assert json.loads(got.blob())["items"] == want  # ints stay ints (not "5")
    assert got.item_bytes is None


def test_fixed_chunks_bigger_than_the_item_count_and_the_chunk_cap_boundary() -> None:
    tree = _one_file_tree()
    limits = SplitLimits(max_items=100, max_chunks=5)
    ok = SplitSpec.model_validate(
        {"over": {"range": {"stop": 10}}, "chunk": {"items": 1000}}
    )
    assert resolve(ok, tree, {}, lambda _s: None, limits).count == 10  # type: ignore[arg-type,return-value]
    at = SplitSpec.model_validate(
        {"over": {"range": {"stop": 50}}, "chunk": {"items": 10}}
    )
    assert resolve(at, tree, {}, lambda _s: None, limits).count == 50  # type: ignore[arg-type,return-value]
    over = SplitSpec.model_validate(
        {"over": {"range": {"stop": 51}}, "chunk": {"items": 10}}
    )
    with pytest.raises(ManifestError) as info:
        resolve(over, tree, {}, lambda _s: None, limits)  # type: ignore[arg-type,return-value]
    assert info.value.problems[0][0] == "split.chunk.items"


def test_resolving_a_wide_tree_stays_fast_and_small(tmp_path: Path) -> None:
    built = _tree(
        tmp_path / "p", {f"d{i % 40}/f{i:05d}.txt": b"x" for i in range(3000)}
    )
    tracemalloc.start()
    t0 = time.perf_counter()
    got = _res(built, "**/*.txt", limits=SplitLimits())
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert got.count == 3000 and elapsed < 5 and peak < 100_000_000


# ------------------------------------------------------------------ list_file


@pytest.mark.parametrize(
    ("data", "want"),
    [
        (b"a\nb\nc", ["a", "b", "c"]),  # no trailing newline
        (b"\xef\xbb\xbfa\nb\n", ["a", "b"]),  # BOM stripped once
        (b"a\r\nb\r\n\r\n", ["a", "b"]),
        (b"a\n\n\n   \n\t\nb\n", ["a", "b"]),  # blank / whitespace-only skipped
        (b"  lead\ntrail  \n", ["  lead", "trail  "]),  # inner whitespace kept
        ("é\nüñí\n日本\n😀\n".encode(), ["é", "üñí", "日本", "😀"]),
        (b"a b\nc\td\n", ["a b", "c\td"]),
        (b"x" * 1024 + b"\n", ["x" * 1024]),  # exactly at the item cap
        (b"../../etc/passwd\n/abs\nC:\\x\n", ["../../etc/passwd", "/abs", "C:\\x"]),
    ],
)
def test_list_file_parsing_edges(tmp_path: Path, data: bytes, want: list[str]) -> None:
    built = _tree(tmp_path / "p", {"items.txt": data})
    assert list(_res(built, {"list_file": "items.txt"}).items) == want


def test_list_file_lone_cr_is_one_item_not_split(tmp_path: Path) -> None:
    # Documented: lines end at \n (a trailing \r is stripped); an old-Mac "\r" file is one line.
    built = _tree(tmp_path / "p", {"items.txt": b"a\rb\rc\n"})
    got = _res(built, {"list_file": "items.txt"}).items
    assert list(got) == ["a\rb\rc"]


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"", "no items"),
        (b"\n\n  \n", "no items"),
        (b"\xef\xbb\xbf\n", "no items"),
        (b"\xff\xfeh\x00i\x00\n\x00", "UTF-8"),  # UTF-16 LE with BOM
        (b"ok\n\xc3\x28\n", "UTF-8"),  # invalid sequence
        (b"ok\n\xed\xa0\x80\n", "UTF-8"),  # encoded lone surrogate
        (b"a\nb\x00c\n", "NUL"),
        (b"a\nb\na\n", "line 3 repeats"),
        (b"a\n" + b"y" * 1025 + b"\n", "line 2 is longer"),
        (
            b"a\n\xef\xbb\xbfb\n",
            None,
        ),  # BOM mid-file: kept as part of the item (or refused), never crash
    ],
)
def test_list_file_refusals_and_odd_bytes(
    tmp_path: Path, data: bytes, fragment: str | None
) -> None:
    built = _tree(tmp_path / "p", {"items.txt": data})
    if fragment is None:
        # mid-file BOM: kept as part of the item or refused typed - never a crash
        with contextlib.suppress(ManifestError):
            _res(built, {"list_file": "items.txt"})
        return
    field, msg = _refused(built, {"list_file": "items.txt"})
    assert field == "split.over.list_file" and fragment in msg


def test_list_file_size_boundary_and_read_is_bounded(tmp_path: Path) -> None:
    limits = SplitLimits(max_list_file_bytes=100)
    built = _tree(
        tmp_path / "p",
        {"exact.txt": (b"a" * 99) + b"\n", "big.txt": (b"a\n" * 51)},
    )
    assert _res(built, {"list_file": "exact.txt"}, limits=limits).count == 1
    field, msg = _refused(built, {"list_file": "big.txt"}, limits=limits)
    assert field == "split.over.list_file" and "larger than 100" in msg


def test_list_file_naming_a_folder_missing_a_symlink_or_other_tree(
    tmp_path: Path,
) -> None:
    built = _tree(
        tmp_path / "p",
        {"d/x.txt": b"a\n", "real.txt": b"a\nb\n"},
        {"alias.txt": "real.txt", "dir.lnk": "d"},
    )
    assert _refused(built, {"list_file": "d"})[0] == "split.over.list_file"
    assert _refused(built, {"list_file": "nope.txt"})[0] == "split.over.list_file"
    assert _refused(built, {"list_file": "dir.lnk"})[0] == "split.over.list_file"
    assert list(_res(built, {"list_file": "alias.txt"}).items) == ["a", "b"]
    # a file under a mount that has no such input is "no such file", not a crash
    assert _refused(built, {"list_file": "ghost/in.txt"})[0] == "split.over.list_file"


def test_list_file_in_input_resolves_and_shadow_names_do_not_confuse(
    tmp_path: Path,
) -> None:
    proj = _tree(tmp_path / "p", {"in/local.txt": b"zzz\n"})
    inp = _tree(tmp_path / "i", {"ids.txt": b"1\n2\n", "in/ids.txt": b"nested\n"})
    got = _res(proj, {"list_file": "in/ids.txt"}, inputs={"in": inp})
    assert list(got.items) == ["1", "2"]
    got2 = _res(proj, {"list_file": "in/in/ids.txt"}, inputs={"in": inp})
    assert list(got2.items) == ["nested"]


def test_list_items_survive_the_blob_exactly(tmp_path: Path) -> None:
    items = [
        'quo"te',
        "back\\slash",
        "new\nline",
        "tab\t",
        "é",
        "😀",
        "{items_file}",
        "a b",
        "0",
    ]
    built = _tree(tmp_path / "p", {"x": b""})
    got = _res(built, {"list": items})
    assert list(got.items) == items
    assert json.loads(got.blob())["items"] == items
    assert got.blob() == _res(built, {"list": items}).blob()


# --------------------------------------------------------------------- lines


def _recs(data: bytes, got: ResolvedItems) -> list[bytes]:
    return [data[o : o + n] for o, n in got.records]


@pytest.mark.parametrize(
    ("data", "header", "want"),
    [
        (b"a\nb\nc\n", False, [b"a\n", b"b\n", b"c\n"]),
        (b"a\nb\nc", False, [b"a\n", b"b\n", b"c"]),
        (b"h\na\nb\n", True, [b"a\n", b"b\n"]),
        (b"h\r\na\r\n\r\nb\r\n", True, [b"a\r\n", b"b\r\n"]),
        (b"\n\n\nx\n", False, [b"x\n"]),
        (b"h\n\n\nx", True, [b"x"]),
        (b"\xff\xfe\x00bin\n\x80\x81\n", False, [b"\xff\xfe\x00bin\n", b"\x80\x81\n"]),
        (b"a\x00b\n", False, [b"a\x00b\n"]),
        (
            b"\n\nh\nx\n",
            True,
            [b"h\n", b"x\n"],
        ),  # header is the FIRST line even if blank
        ("é,1\n日本,2\n".encode(), False, ["é,1\n".encode(), "日本,2\n".encode()]),
    ],
)
def test_lines_records_and_header_edges(
    tmp_path: Path, data: bytes, header: bool, want: list[bytes]
) -> None:
    built = _tree(tmp_path / "p", {"rows.csv": data})
    got = _res(built, {"lines": "rows.csv", "header": header})
    assert _recs(data, got) == want
    assert list(got.items) == list(range(len(want)))
    assert got.item_bytes == sum(len(w) for w in want)
    if header:
        assert got.header is not None
        o, n = got.header
        assert data[o : o + n].endswith((b"\n", b"h")) or n > 0
    else:
        assert got.header is None


@pytest.mark.parametrize(
    ("data", "header"),
    [
        (b"", False),
        (b"\n\n  \n", False),
        (b"header only\n", True),
        (b"header only", True),
        (b"", True),
    ],
)
def test_lines_without_records_is_invalid_input(
    tmp_path: Path, data: bytes, header: bool
) -> None:
    built = _tree(tmp_path / "p", {"rows.csv": data})
    field, msg = _refused(built, {"lines": "rows.csv", "header": header})
    assert field == "split.over.lines" and "no records" in msg


def test_lines_records_reassemble_the_file_property(tmp_path: Path) -> None:
    rnd = random.Random(7)  # noqa: S311
    for trial in range(25):
        parts = []
        for _ in range(rnd.randint(1, 200)):
            kind = rnd.random()
            if kind < 0.15:
                parts.append(b"\n")  # blank
            elif kind < 0.2:
                parts.append(b"   \r\n")
            else:
                parts.append(
                    bytes(
                        rnd.choice(b"abc,\xc3\xa9\x00\xff\r")
                        for _ in range(rnd.randint(1, 30))
                    )
                    + b"\n"
                )
        data = b"".join(parts)
        if rnd.random() < 0.5:
            data = data.rstrip(b"\n")
        if not data.strip():
            continue
        header = rnd.random() < 0.3
        built = _tree(tmp_path / f"t{trial}", {"f": data})
        try:
            got = _res(built, {"lines": "f", "header": header})
        except ManifestError:
            continue  # only "no records" is legal here
        # offsets are strictly increasing, non-overlapping, within the file
        last_end = 0
        for o, n in got.records:
            assert o >= last_end and n > 0 and o + n <= len(data)
            last_end = o + n
        # every non-blank non-header line appears, in file order
        lines = data.split(b"\n")
        expect = [ln + b"\n" for ln in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
        if header:
            expect = expect[1:]
        expect = [e for e in expect if e.strip()]
        assert _recs(data, got) == expect


def test_lines_across_block_boundary_and_long_line_cap(tmp_path: Path) -> None:
    # a record straddling the 1 MiB read block must be intact
    big = b"x" * (1024 * 1024 - 5) + b"\n"
    data = b"a\n" + big + b"z\n"
    built = _tree(tmp_path / "p", {"f": data})
    got = _res(built, {"lines": "f"})
    assert _recs(data, got) == [b"a\n", big, b"z\n"]
    # over the cap (multi-block) is refused, with the yaml path
    limits = SplitLimits(max_record_bytes=1000)
    huge = b"a\n" + b"y" * (3 * 1024 * 1024) + b"\n"
    built2 = _tree(tmp_path / "q", {"f": huge})
    assert _refused(built2, {"lines": "f"}, limits=limits)[0] == "split.over.lines"
    # exactly at the cap is fine, one over is refused
    at = SplitLimits(max_record_bytes=10)
    built3 = _tree(tmp_path / "r", {"f": b"123456789\n"})
    assert _res(built3, {"lines": "f"}, limits=at).count == 1
    built4 = _tree(tmp_path / "s", {"f": b"1234567890\n"})
    assert _refused(built4, {"lines": "f"}, limits=at)[0] == "split.over.lines"


def test_lines_file_size_cap_boundary(tmp_path: Path) -> None:
    limits = SplitLimits(max_lines_file_bytes=10)
    built = _tree(tmp_path / "p", {"ok": b"123456789\n", "big": b"12345678901\n"})
    assert _res(built, {"lines": "ok"}, limits=limits).count == 1
    assert "larger than 10" in _refused(built, {"lines": "big"}, limits=limits)[1]


def test_lines_blob_reassembly_matches_file_for_g4(tmp_path: Path) -> None:
    data = b"id,v\n1,a\n\n2,b\n3,c"
    built = _tree(tmp_path / "p", {"d.csv": data})
    got = _res(built, {"lines": "d.csv", "header": True})
    blob = json.loads(got.blob())
    assert blob["source"]["size"] == len(data)
    assert blob["source"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert blob["count"] == 3 and blob["items"] == [0, 1, 2]
    assert [data[o : o + n] for o, n in blob["records"]] == [b"1,a\n", b"2,b\n", b"3,c"]
    assert data[blob["header"][0] : sum(blob["header"])] == b"id,v\n"


# ----------------------------------------------------------------------- blob


def test_blob_digest_depends_only_on_content_not_on_where_or_when(
    tmp_path: Path,
) -> None:
    files = {"b/é.txt": b"2", "a/x.txt": b"1", "c.txt": b"3"}
    one = _res(_tree(tmp_path / "one", files), "**/*.txt")
    time.sleep(0.02)
    two = _res(_tree(tmp_path / "two", dict(reversed(list(files.items())))), "**/*.txt")
    assert one.blob() == two.blob() and one.digest == two.digest
    assert (
        one.blob()
        == json.dumps(
            json.loads(one.blob()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    # a changed byte in a file changes the digest; a renamed file does too
    three = _res(_tree(tmp_path / "three", {**files, "c.txt": b"X"}), "**/*.txt")
    assert three.digest != one.digest


def test_same_items_over_different_kinds_never_collide(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"a": b"", "items.txt": b"a\n"})
    by_list = _res(built, {"list": ["a"]})
    by_file = _res(built, {"list_file": "items.txt"})
    assert by_list.digest != by_file.digest  # source info differs
    assert (
        _res(built, {"list": ["0"]}).digest
        != _res(built, {"range": {"stop": 1}}).digest
    )


def test_lead_fix_d1_last_line_over_cap_in_same_block_is_refused() -> None:
    """Tester r1 D1: an over-cap final line sharing a block with a short one."""
    import io

    from meshforge.common import split_items as si

    with pytest.raises(si._LongLineError):
        list(si._records(io.BytesIO(b"a\n" + b"x" * 100), 10))
    with pytest.raises(si._LongLineError):
        list(si._records(io.BytesIO(b"a\n" + b"x" * 100 + b"\nb\n"), 10))
    assert list(si._records(io.BytesIO(b"a\n" + b"x" * 9), 10)) == [
        (0, 2, False),
        (2, 9, False),
    ]
