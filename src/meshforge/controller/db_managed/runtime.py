"""The managed cluster as the controller uses it (V0.17 G2, D390).

``[controller.db] mode = "managed"`` turns :class:`~.cluster.ManagedCluster` into
the controller's database:

* :func:`pg_bin_dir` - where the bundled PostgreSQL lives: ``[controller.db]
  bin_dir``, else ``<bundle>/pgsql/bin`` of the controller bundle this process
  runs from (``scripts/build_bundle.py --role controller`` puts it there);
* :func:`cluster_for` - the cluster of these settings (root, socket dir, port);
* :func:`tool_database_settings` - the application role's connection for the
  admin CLI (``user create``, ``fabric invite``...) while the controller service
  runs the cluster: it reads ``app.password`` (owner-only, so the CLI must run as
  the service user - :mod:`meshforge.common.system_cli` re-executes it so) and
  never starts anything.

The server (:mod:`meshforge.controller.server`) starts the cluster on its event
loop thread **before** ``check_connection``/migrations and stops it after the
engine is disposed; :class:`~.cluster.ManagedCluster.wait_exit` lets it treat a
postmaster that dies under it as fatal.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from meshforge.common.buildinfo import bundle_root
from meshforge.common.errors import ConfigError
from meshforge.controller.db_managed.binaries import PgBinaries
from meshforge.controller.db_managed.cluster import (
    APP_ROLE,
    ManagedCluster,
    StopTimeouts,
)
from meshforge.controller.db_managed.commands import LISTEN_ADDRESS
from meshforge.controller.db_managed.errors import ManagedDbError, ManagedDbReason
from meshforge.controller.db_managed.layout import ManagedLayout
from meshforge.controller.db_managed.secret import read_password_file

if TYPE_CHECKING:
    from meshforge.controller.settings import ControllerSettings
    from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "PGSQL_DIR_NAME",
    "cluster_for",
    "layout_for",
    "pg_bin_dir",
    "tool_database_settings",
]

PGSQL_DIR_NAME: Final = "pgsql"
"""``<bundle>/pgsql/{bin,lib,share}``: the trimmed PostgreSQL of the controller
bundle (D384 + D390)."""


def pg_bin_dir(settings: ControllerSettings, *, bundle: Path | None = None) -> Path:
    """The bundled PostgreSQL's ``bin`` folder; ``config_error`` if unknown.

    ``bundle`` defaults to :func:`~meshforge.common.buildinfo.bundle_root`.
    """
    configured = settings.controller.db.bin_dir
    if configured is not None:
        return configured
    root = bundle if bundle is not None else bundle_root()
    if root is not None:
        return root / PGSQL_DIR_NAME / "bin"
    raise ConfigError(
        "[controller.db] mode = 'managed' needs the bundled PostgreSQL, and this "
        "controller does not run from a MeshForge bundle: set [controller.db] "
        "bin_dir (a developer checkout: `uv run python scripts/build_bundle.py "
        "--pg-only --out .cache/pg` prints the folder), or use mode = 'external'",
        details={"setting": "[controller.db] bin_dir"},
    )


def layout_for(settings: ControllerSettings) -> ManagedLayout:
    return ManagedLayout(
        settings.db_root, socket_dir_override=settings.controller.db.socket_dir
    )


def cluster_for(
    settings: ControllerSettings,
    *,
    die_with_parent: bool = True,
    bundle: Path | None = None,
) -> ManagedCluster:
    """The managed cluster these settings describe (not started).

    The fast-shutdown budget leaves room inside systemd's ``TimeoutStopSec``
    (``packaging/linux/meshforge-controller.service``: drain + 30 s + margin).
    """
    db = settings.controller.db
    return ManagedCluster(
        PgBinaries(pg_bin_dir(settings, bundle=bundle)),
        layout_for(settings),
        port=db.port,
        start_timeout_s=db.start_timeout_s,
        stop_timeouts=StopTimeouts(smart_s=5.0, fast_s=20.0, immediate_s=5.0),
        die_with_parent=die_with_parent,
    )


def tool_database_settings(settings: ControllerSettings) -> DatabaseSettings:
    """``settings.database`` for an admin command (module docstring).

    External mode: unchanged. Managed mode: the application role over TCP
    loopback with the password from ``app.password``; a missing or non-private
    file is a typed ``config_error`` that says what to do.
    """
    from meshforge.controller.store.settings import DatabaseSettings

    if not settings.managed_db:
        return settings.database
    layout = layout_for(settings)
    path = layout.app_password_file
    if not path.exists():
        raise ManagedDbError(
            f"the managed database has not been created yet ({path} is missing): "
            "start the controller once (sudo systemctl start meshforge-controller)",
            reason=ManagedDbReason.NOT_RUNNING,
            details={"path": str(path)},
        )
    password = read_password_file(path)
    fields: dict[str, object] = {
        **settings.database.model_dump(),
        "url": None,
        "host": LISTEN_ADDRESS,
        "port": settings.controller.db.port,
        "name": "meshforge",
        "user": APP_ROLE,
        "password": password,
        "session_role": None,
    }
    return DatabaseSettings.model_validate(fields)
