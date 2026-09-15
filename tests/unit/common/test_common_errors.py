"""Task 0.1.6: error base classes and failure taxonomy."""

import json
import pickle

import pytest

from meshforge.common import errors
from meshforge.common.errors import (
    DEFAULT_RETRYABLE,
    ConfigError,
    DigestMismatchError,
    FailureType,
    InvalidIdError,
    MeshForgeError,
    is_retryable,
)
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit


def test_failure_types_named_in_architecture_exist() -> None:
    # ARCHITECTURE §13 names these explicitly; values are persisted and must not drift.
    for value in ("user_error", "oom", "env_error", "disk_full"):
        assert FailureType(value).value == value


_EXPECTED_RETRYABLE: dict[str, bool] = {
    "USER_ERROR": False,
    "OOM": False,
    "VRAM_OOM": True,
    "ENV_ERROR": False,
    "DISK_FULL": False,
    "NODE_LOST": True,
    "GPU_SESSION_LOST": True,
    "TRANSFER_ERROR": True,
    "INTEGRITY_ERROR": True,
    "FENCED": False,
    "CANCELLED": False,
    "TIMEOUT": True,
    "DEADLINE_EXCEEDED": False,
    "RESOURCE_BUSY": True,
    "NO_FEASIBLE_PLAN": False,
    "NETWORK_ERROR": True,
    "CONFIG_ERROR": False,
    "INVALID_INPUT": False,
    "UNAUTHORIZED": False,
    "NOT_FOUND": False,
    "CONFLICT": False,
    "INTERNAL_ERROR": False,
}


def test_failure_type_list_is_exactly_the_agreed_taxonomy() -> None:
    # Mirrored 1:1 by proto FailureType (parity test in 0.1.5); values are permanent.
    assert [m.name for m in FailureType] == list(_EXPECTED_RETRYABLE)
    for member in FailureType:
        assert member.value == member.name.lower()


@pytest.mark.parametrize(("name", "retryable"), list(_EXPECTED_RETRYABLE.items()))
def test_default_retryability_table(name: str, retryable: bool) -> None:
    assert is_retryable(FailureType[name]) is retryable
    assert MeshForgeError("x", failure_type=FailureType[name]).retryable is retryable


def test_every_failure_type_has_default_retryability() -> None:
    assert set(DEFAULT_RETRYABLE) == set(FailureType)
    assert is_retryable(FailureType.NODE_LOST) is True
    assert is_retryable(FailureType.USER_ERROR) is False


def test_base_error_defaults() -> None:
    err = MeshForgeError("boom")
    assert str(err) == "boom"
    assert err.failure_type is FailureType.INTERNAL_ERROR
    assert err.retryable is False
    assert err.details == {}
    assert err.exit_code is ExitCode.FAILURE


def test_instance_overrides_and_retryable_follows_failure_type() -> None:
    err = MeshForgeError("net", failure_type=FailureType.NETWORK_ERROR)
    assert err.retryable is True
    forced = MeshForgeError(
        "net", failure_type=FailureType.NETWORK_ERROR, retryable=False
    )
    assert forced.retryable is False


def test_subclasses() -> None:
    cfg = ConfigError("bad")
    assert isinstance(cfg, ValueError)
    assert cfg.failure_type is FailureType.CONFIG_ERROR
    assert cfg.exit_code is ExitCode.CONFIG_ERROR
    assert int(ExitCode.CONFIG_ERROR) == 4
    assert isinstance(InvalidIdError("x"), ValueError)
    assert InvalidIdError("x").failure_type is FailureType.INVALID_INPUT
    mismatch = DigestMismatchError("m")
    assert mismatch.failure_type is FailureType.INTEGRITY_ERROR
    assert mismatch.retryable is True


def test_to_dict_is_json_safe() -> None:
    err = ConfigError("bad", details={"loc": "log.level"})
    data = json.loads(json.dumps(err.to_dict()))
    assert data == {
        "error": "ConfigError",
        "failure_type": "config_error",
        "message": "bad",
        "retryable": False,
        "details": {"loc": "log.level"},
    }


def test_pickle_round_trip_preserves_everything() -> None:
    err = DigestMismatchError("m", retryable=False, details={"a": 1})
    clone = pickle.loads(pickle.dumps(err))
    assert type(clone) is DigestMismatchError
    assert (clone.message, clone.failure_type, clone.retryable, clone.details) == (
        "m",
        FailureType.INTEGRITY_ERROR,
        False,
        {"a": 1},
    )


def test_exit_code_import_paths() -> None:
    assert errors.ExitCode is ExitCode
    assert [
        ExitCode.OK,
        ExitCode.FAILURE,
        ExitCode.USAGE_ERROR,
        ExitCode.NOT_IMPLEMENTED,
    ] == [
        0,
        1,
        2,
        3,
    ]
