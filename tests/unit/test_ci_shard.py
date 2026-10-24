"""``--shard K/N`` (tests/conftest.py): CI splits the component tier by file."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "conftest.py"


def _load_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_shard_conftest", CONFTEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFTEST = _load_conftest()


@pytest.mark.parametrize("value", ["1/1", "2/5", "5/5"])
def test_valid_shards_parse(value: str) -> None:
    k, n = (int(x) for x in value.split("/"))
    assert CONFTEST.parse_shard(value) == (k, n)


@pytest.mark.parametrize("value", ["0/3", "4/3", "3", "a/b", "1/0", "-1/2", "/", ""])
def test_invalid_shards_are_a_usage_error(value: str) -> None:
    with pytest.raises(pytest.UsageError, match="--shard expects K/N"):
        CONFTEST.parse_shard(value)


def test_shards_partition_the_files_and_keep_each_file_whole() -> None:
    files = [f"tests/component/test_{c}.py" for c in "qwertyuiopasdfg"]
    files += files[:4]  # several tests of one file: one entry each
    n = 4
    shards = [CONFTEST.shard_of_files(files, k, n) for k in range(1, n + 1)]
    assert set().union(*shards) == set(files)
    assert sum(len(s) for s in shards) == len(set(files))
    assert max(len(s) for s in shards) - min(len(s) for s in shards) <= 1
    # Deterministic: the same input always gives the same shard.
    assert CONFTEST.shard_of_files(list(reversed(files)), 2, n) == shards[1]
