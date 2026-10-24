"""Move a per-user agent identity + config into the machine-wide service (V0.17 G1, D387).

``meshforge-agent migrate-identity [--from DIR] [--config-from FILE] [--dry-run]``
runs as root from the ``.deb``'s ``postinst`` (and by hand when it asks for it).
A laptop that already joined as a user-unit agent must keep its ``node_id``: a
silent re-enrolment would split its telemetry history and the ML training data,
so ADR-0013's "never silently mint" rule is applied to the move as well.

**Plan first, then act.** Everything is decided read-only (``plan``): the
target, the candidate identities, each one's validity, the config file. Any
refusal stops *before* the first write to a source:

* target identity already present -> nothing to do (``target_present``); a
  source that is the *same* node (same ``node_id``) left behind by an
  interrupted earlier run is renamed now, so a re-run converges;
* no candidate -> ``nothing_to_migrate`` (a fresh laptop: the service mints);
* exactly one valid candidate -> ``migrated``;
* two or more candidates, or a partial / corrupt / unreadable one, or a config
  file that is invalid or names a home directory -> ``blocked``: nothing is copied,
  no source is touched, and ``migration.pending`` is written into the *target*
  so the service refuses to mint while the choice is open
  (:func:`meshforge.agent.identity.load_or_create`). The message names every
  path and the fix (``--from DIR``, or rename the unwanted folders and re-run).

**Copy (one candidate).** The old user unit is stopped and disabled for its user
first (``systemctl --user -M <user>@``; its file stays - we do not delete files
in a user's home - and the report says so). Then, under both directories'
``identity.lock``: ``migration.pending`` (state ``copying``) is written into the
target, the identity files are copied (``0600``, directory ``0700``), the copy is
loaded and its ``node_id`` + ``key_fingerprint_sha256`` compared with the
source's, everything is handed to the service user, the marker is removed - the
commit point - and the source folder is renamed ``identity.migrated-<date>``.
A crash anywhere before the commit leaves the marker: the service refuses, and
the next run clears the half copy and starts again (converges). The config file
moves the same way to ``/etc/meshforge/config.toml`` (``0640 root:meshforge``),
its source renamed ``config.toml.migrated-<date>``.

**Sources are not trusted** (fix round 1): root reads them through
:mod:`~meshforge.agent.migration.safe_source` - no symlink anywhere on the path,
folders and files owned by the user, nothing writable by others, ``fstat`` on the
opened fd, renames inside the held parent fd and verified by inode. ``execute``
re-reads everything that way before its first write; a violation (in the plan or
since it) is ``blocked`` with the reason, and a failed verify removes the copied
files (the marker stays).

The peer/env/blob caches are **not** migrated (Q14: they are caches). Nothing
here logs or prints key material; paths and ``node_id`` only.
"""

from meshforge.agent.migration.apply import execute, migrate
from meshforge.agent.migration.model import (
    IDENTITY_FILES,
    SERVICE_USER,
    USER_UNIT_NAME,
    ConfigPlan,
    MigrationOutcome,
    MigrationPlan,
    MigrationReport,
    Owner,
    Paths,
    system_paths,
)
from meshforge.agent.migration.planning import check_config, find_candidates, plan
from meshforge.agent.migration.user_units import running_user_agents, stop_user_unit

__all__ = [
    "IDENTITY_FILES",
    "SERVICE_USER",
    "USER_UNIT_NAME",
    "ConfigPlan",
    "MigrationOutcome",
    "MigrationPlan",
    "MigrationReport",
    "Owner",
    "Paths",
    "check_config",
    "execute",
    "find_candidates",
    "migrate",
    "plan",
    "running_user_agents",
    "stop_user_unit",
    "system_paths",
]
