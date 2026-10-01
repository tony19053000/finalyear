"""Cached execution environments, keyed by env hash (V0.5 G2, decisions D85-D87).

* :mod:`.hash` - the env hash (D85): what goes into it and the requirements
  normalisation; pure functions, identical on every OS.
* :mod:`.builder` - build an environment **once per hash per node** under a file
  lock, crash-safe (``<hash>.building`` -> rename), read-only to jobs (D86).
* :mod:`.errors` - :class:`EnvError`, the typed failure every step raises.

The runtimes (:mod:`meshforge.agent.execution`) call the builder in their ``env``
phase; this package imports nothing from them.
"""

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import (
    ENV_HASH_VERSION,
    EnvInputs,
    env_hash,
    normalize_requirements,
)

__all__ = [
    "ENV_HASH_VERSION",
    "EnvError",
    "EnvInputs",
    "env_hash",
    "normalize_requirements",
]
