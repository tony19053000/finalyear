"""The agent's content-addressed cache and tree staging (V0.5 G1, D79/D80/D84).

* :mod:`.store` - :class:`AgentCas`: the shared :class:`~meshforge.common.cas.CasStore`
  under ``<data dir>/cas`` plus per-digest locks, so two attempts never fetch the
  same blob twice at once.
* :mod:`.fetcher` - :class:`BlobFetcher`: download missing blobs from the
  listed sources (whole small files; ``Range`` chunks for large ones, each
  verified, resumable across crashes through the CAS bitmap sidecar), built on
  :mod:`.sources` (per-source identity, stats, errors, requests),
  :mod:`.manifest` (the manifest GET) and :mod:`.scheduler` (work stealing).
* :mod:`.materialise` - copy a tree from the cache into a workdir (reflink where
  the file system can, never a hard link), hashing while copying (D84).
"""

from meshforge.agent.cas.fetcher import BlobFetcher, BlobSource, FetchStats
from meshforge.agent.cas.materialise import (
    CorruptBlobError,
    MaterialiseError,
    MaterialiseStats,
    materialise,
)
from meshforge.agent.cas.store import AgentCas

__all__ = [
    "AgentCas",
    "BlobFetcher",
    "BlobSource",
    "CorruptBlobError",
    "FetchStats",
    "MaterialiseError",
    "MaterialiseStats",
    "materialise",
]
