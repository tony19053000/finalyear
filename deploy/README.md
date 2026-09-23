# deploy/

Deployment assets: the development database, plus the **dev background service**
templates (roadmap 0.3.13). Real installers arrive with V0.17 productization.

```
deploy/
  docker-compose.dev.yml          PostgreSQL 16 for local development
  .env.example                    compose variables (copy to deploy/.env)
  systemd/meshforge-agent.service      systemd *user* unit (Linux)
  systemd/meshforge-controller.service systemd *user* unit (Linux, controller laptop)
  windows/meshforge-agent-task.xml     Task Scheduler template (Windows agent)
```

**Nothing here installs itself** (decision D17). Print the exact commands with

```bash
uv run python scripts/print_service_setup.py          # this platform
uv run python scripts/print_service_setup.py all      # linux + windows + uninstall
```

and read [`docs/OPERATIONS.md`](../docs/OPERATIONS.md) for the prose: the V0.3
trust boundary (the REST surface on port 47110 is read-only and
**unauthenticated**), `loginctl enable-linger`, where the logs go, the Windows
logoff open question, troubleshooting and uninstall.

## Development PostgreSQL (`docker-compose.dev.yml`)

PostgreSQL 16 (`postgres:16.15-alpine3.24`) for local development and manual tests.
Automated component tests do **not** use it; they start their own throwaway container
with testcontainers.

```bash
uv sync --extra controller                                  # SQLAlchemy, asyncpg, Alembic
docker compose -f deploy/docker-compose.dev.yml up -d --wait
uv run alembic upgrade head                                 # create/upgrade the schema
uv run alembic current                                      # show the applied revision
uv run alembic downgrade base                               # drop everything Alembic created
docker compose -f deploy/docker-compose.dev.yml down -v     # stop and DELETE the volume
```

`up --wait` returns once the healthcheck (`pg_isready` over TCP) passes.

### Credentials and ports (dev defaults)

| Setting  | Compose variable (`deploy/.env`) | Controller setting (env / TOML `[database]`) | Default         |
|----------|----------------------------------|-----------------------------------------------|-----------------|
| host     | (always `127.0.0.1`)             | `MESHFORGE_DATABASE__HOST`                    | `127.0.0.1`     |
| port     | `MESHFORGE_DEV_DB_PORT`          | `MESHFORGE_DATABASE__PORT`                    | `5432`          |
| database | `MESHFORGE_DEV_DB_NAME`          | `MESHFORGE_DATABASE__NAME`                    | `meshforge`     |
| user     | `MESHFORGE_DEV_DB_USER`          | `MESHFORGE_DATABASE__USER`                    | `meshforge`     |
| password | `MESHFORGE_DEV_DB_PASSWORD`      | `MESHFORGE_DATABASE__PASSWORD`                | `meshforge-dev` |

`MESHFORGE_DATABASE__URL` (for example `postgresql+asyncpg://user:pw@host:5432/db`)
overrides all the individual fields at once.

The defaults are **development-only** values for a database that listens on
`127.0.0.1` only, so it is never reachable from the LAN. Do not reuse them for any
database reachable from other machines. To change them, copy `.env.example` to
`deploy/.env` (git-ignored) **and** export the matching `MESHFORGE_DATABASE__*`
variables, because MeshForge settings do not read `.env` files.

Port 5432 already in use? Set `MESHFORGE_DEV_DB_PORT=5433` in `deploy/.env` and
`export MESHFORGE_DATABASE__PORT=5433`.

### Migrations

Alembic's config is `alembic.ini` at the repo root; the scripts live in
`src/meshforge/controller/store/migrations/` so they ship inside the package. The
database URL always comes from MeshForge settings, never from `alembic.ini`. Name new
revisions with sequential IDs:

```bash
uv run alembic revision --autogenerate --rev-id 0002 -m "add nodes table"
uv run alembic check          # fails if the models and migrations disagree
```

> **Connection URL query parameters:** the driver is asyncpg, so use asyncpg-style parameters (e.g. `?ssl=disable`). psycopg/libpq names such as `?sslmode=` are rejected at connect time and surface as a generic `DatabaseUnavailableError`.
