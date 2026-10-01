"""The one exception type of :mod:`meshforge.agent.envs`."""

from __future__ import annotations

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = ["EnvError"]


class EnvError(MeshForgeError):
    """An environment could not be resolved or built.

    ``reason`` is a stable code a runtime copies into ``RunResult.reason``:

    ==========================  ==============  ===================================
    reason                      failure_type    when
    ==========================  ==============  ===================================
    ``requirements_rejected``   env_error       file too big / too many lines / not
                                                UTF-8 / a line the hash cannot cover
    ``requirements_missing``    env_error       the manifest names a file that is not
                                                in the project (or escapes it)
    ``setup_failed``            env_error       the installer exited non-zero
    ``python_not_found``        env_error       no interpreter for ``env.python``
    ``uv_missing``              env_error       the ``uv`` binary cannot be started
    ``env_too_large``           env_error       built env over ``max_env_bytes``
    ``unpinned_base``           config_error    a base image without ``@sha256:``
    ``disk_full``               disk_full       free disk under the floor
    ``env_timeout``             timeout         lock wait + build over the budget
    ``env_lock_failed``         internal_error  the lock file cannot be opened
    ``env_io_failed``           internal_error  the envs directory failed (I/O)
    ==========================  ==============  ===================================
    """

    default_failure_type = FailureType.ENV_ERROR

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        failure_type: FailureType | None = None,
    ) -> None:
        super().__init__(message, failure_type=failure_type, details={"reason": reason})

    @property
    def reason(self) -> str:
        # kept in ``details`` so it survives MeshForgeError's pickling round trip
        return str(self.details.get("reason", ""))
