"""CLI <-> dashboard parity table (V0.8 G7, criterion 1, D176).

Every command of the ``compute`` CLI - and of ``meshforge-controller user``,
because D176 lists "users (admin)" - is mapped to EITHER the dashboard route
that does the same thing plus the e2e test(s) that prove it, OR an explicit
"CLI-only" entry with its reason. ``tests/unit/test_v08_g7_cli_ui_parity.py``
enumerates the real command trees from typer at test time: a new command with
no entry here fails, a removed command left here fails, a route that is not in
the SPA router fails, and an e2e test id that does not exist fails.

Test ids are ``<file directly in tests/e2e>::<top-level test function>`` (no
path: a test outside tests/e2e is no UI proof). Hidden CLI commands count too.
"""

from __future__ import annotations

import dataclasses
from typing import Final


@dataclasses.dataclass(frozen=True)
class InUi:
    """The command's operation is in the dashboard."""

    route: str
    """SPA route, as in the router (``:param`` for a path parameter)."""
    where: str
    """What the user does there."""
    e2e: tuple[str, ...]
    """End-to-end tests (real controller + agents + Chromium) that prove it."""


@dataclasses.dataclass(frozen=True)
class CliOnly:
    """No dashboard equivalent, on purpose: ``reason`` says why."""

    reason: str


Entry = InUi | CliOnly

G2_SHELL = "test_v08_g2_shell.py"
G3 = "test_v08_g3_overview_nodes.py"
G4 = "test_v08_g4_jobs.py"
G4_T1 = "test_v08_g4_tester_r1.py"
G5 = "test_v08_g5_settings.py"
G5_T1 = "test_v08_g5_tester_r1_e2e.py"
G7 = "test_v08_g7_mode1_flows.py"
LIFE_CYCLE = (
    f"{G7}::test_mode1_job_life_cycle_in_the_browser_matches_the_api_and_the_cli"
)

COMPUTE: Final[dict[str, Entry]] = {
    # ------------------------------------------------------------ jobs
    "run": InUi(
        "/jobs/new",
        "pick a project folder (or zip) -> Upload and submit",
        (LIFE_CYCLE, f"{G4}::test_submit_a_folder_run_follow_logs_download_outputs"),
    ),
    "jobs": InUi(
        "/jobs",
        "jobs list (server paging, filters); a row opens /jobs/:jobId",
        (LIFE_CYCLE, f"{G4}::test_list_pages_through_120_jobs"),
    ),
    "logs": InUi(
        "/jobs/:jobId",
        "Logs tab: live terminal view, follow/pause, download",
        (
            LIFE_CYCLE,
            f"{G4_T1}::test_log_follower_reconnects_without_gaps_or_duplicates_v08_g4_tester_r1",
        ),
    ),
    "get": InUi(
        "/jobs/:jobId",
        "Outputs tab: Download outputs (the verified archive)",
        (LIFE_CYCLE,),
    ),
    "cancel": InUi(
        "/jobs/:jobId",
        "Cancel job -> confirm",
        (LIFE_CYCLE, f"{G4}::test_keyboard_only_submit_and_cancel_a_running_job"),
    ),
    "explain": InUi(
        "/jobs/:jobId",
        "Plan tab: the planner's decision table (every candidate, chosen node)",
        (LIFE_CYCLE,),
    ),
    # ------------------------------------------------------------ nodes
    "nodes": InUi(
        "/nodes",
        "nodes list; a row opens /nodes/:nodeId (capabilities, live metrics)",
        (f"{G3}::test_overview_and_nodes_show_what_the_api_returns",),
    ),
    "node revoke": InUi(
        "/nodes/:nodeId",
        "Revoke node -> type the node name -> Revoke permanently (admin)",
        (f"{G3}::test_revoke_by_keyboard_only",),
    ),
    "network": InUi(
        "/nodes/:nodeId",
        "Network tab: the measured links of this node (bandwidth, RTT, age)",
        (f"{G3}::test_overview_and_nodes_show_what_the_api_returns",),
    ),
    "node prepare": CliOnly(
        "node maintenance, not a Mode 1 job operation (D176 list): pre-pulls base "
        "images / builds environments on one node ahead of a run. No dashboard "
        "action in V0.8 - Lead to decide whether one lands later (V0.17 polish)."
    ),
    "node gc": CliOnly(
        "node maintenance, not a Mode 1 job operation (D176 list): asks one agent "
        "for a cache GC pass now (agents also run it on their own schedule). No "
        "dashboard action in V0.8 - Lead to decide whether one lands later."
    ),
    # ------------------------------------------------------------ data + secrets
    "data ls": InUi(
        "/settings/data",
        "every named input version (ref, files, size, pinned)",
        (f"{G5}::test_data_pin_and_delete_from_the_browser",),
    ),
    "data show": InUi(
        "/settings/data",
        "one input's versions are rows of the same table",
        (f"{G5_T1}::test_data_values_match_the_endpoint_and_keyboard_only_pin_delete",),
    ),
    "data pin": InUi(
        "/settings/data",
        "Pin / Unpin on the version's row",
        (f"{G5}::test_data_pin_and_delete_from_the_browser",),
    ),
    "data rm": InUi(
        "/settings/data",
        "Delete on the version's row -> confirm",
        (f"{G5}::test_data_pin_and_delete_from_the_browser",),
    ),
    "data push": CliOnly(
        "uploads a local DIRECTORY as a new named-input version; the browser has "
        "no data upload in V0.8 (G5 hand-off, D203 deviations) - the Data page "
        "shows the `compute data push` command instead."
    ),
    "secret list": InUi(
        "/settings/secrets",
        "names and times (never values)",
        (
            f"{G5}::test_a_secret_set_in_the_browser_reaches_a_job_and_never_the_browser",
        ),
    ),
    "secret set": InUi(
        "/settings/secrets",
        "Set a secret: typed (write-only) or read from a file, byte-exact",
        (
            f"{G5}::test_a_secret_set_in_the_browser_reaches_a_job_and_never_the_browser",
            f"{G5_T1}::test_secret_read_from_a_file_reaches_the_job_byte_for_byte",
        ),
    ),
    "secret delete": InUi(
        "/settings/secrets",
        "Delete on the secret's row -> confirm",
        (
            f"{G5}::test_a_secret_set_in_the_browser_reaches_a_job_and_never_the_browser",
        ),
    ),
    # ------------------------------------------------------------ fabric
    "fabric invite": InUi(
        "/settings/invites",
        "Create invite: the join code once, with copy + QR (admin)",
        (f"{G5}::test_invite_in_the_browser_lets_a_laptop_join",),
    ),
    "fabric invites": InUi(
        "/settings/invites",
        "the invite list; Revoke on a pending invite (admin)",
        (
            f"{G5}::test_invite_in_the_browser_lets_a_laptop_join",
            f"{G5_T1}::test_displayed_tokens_invites_and_controller_card_match_the_endpoints",
        ),
    ),
    "fabric join": CliOnly(
        "runs ON the joining laptop: it enrols that machine's agent "
        "(`meshforge-agent join`) and writes its identity there - a browser page "
        "cannot do that for the machine it runs on. The Invites page shows the "
        "code, its QR and the exact join commands."
    ),
    # ------------------------------------------------------------ auth
    "login": InUi(
        "/login",
        "sign in (a browser session cookie; API tokens are made on /settings/tokens)",
        (
            f"{G2_SHELL}::test_keyboard_only_login_and_logout",
            f"{G5}::test_a_browser_made_token_works_until_revoked_in_the_browser",
        ),
    ),
    "logout": InUi(
        "/",
        "Sign out in the header (the session is deleted server-side)",
        (f"{G2_SHELL}::test_login_shell_and_logout", LIFE_CYCLE),
    ),
    "whoami": InUi(
        "/settings",
        "Account card: user, role, session (and the header's current user)",
        (f"{G2_SHELL}::test_login_shell_and_logout",),
    ),
    "tokens list": InUi(
        "/settings/tokens",
        "API tokens and browser sessions; admins: Every user",
        (
            f"{G5_T1}::test_displayed_tokens_invites_and_controller_card_match_the_endpoints",
        ),
    ),
    "tokens revoke": InUi(
        "/settings/tokens",
        "Revoke on the token's row -> confirm",
        (f"{G5}::test_a_browser_made_token_works_until_revoked_in_the_browser",),
    ),
    "status": InUi(
        "/settings",
        "Controller card (/healthz: status, agent sessions, degraded loops) + the "
        "header's connection and node count",
        (
            f"{G5}::test_user_and_admin_views_and_axe_on_every_settings_page",
            f"{G2_SHELL}::test_login_shell_and_logout",
        ),
    ),
    # ------------------------------------------------------------ benchmarks (V0.9)
    "bench workloads": CliOnly(
        "V0.9 G2: lists the benchmark workload library on this laptop (no "
        "controller call). The Benchmarks page (0.9.9, G6) is where campaigns are "
        "shown; G6 decides whether it lists the families too and moves this entry."
    ),
    # V0.9 G3 campaigns (D216). The Benchmarks page (0.9.9, G6) shows and drives
    # campaigns (D221) and moves these entries to UI mappings then.
    "bench plan": CliOnly(
        "V0.9 G3: expands a campaign spec file on this laptop before anything is "
        "stored - a preview of a YAML file the operator is editing locally."
    ),
    "bench create": CliOnly(
        "V0.9 G3: stores a campaign from a local spec file (admin); the "
        "Benchmarks page (G6, D221) adds the browser form."
    ),
    "bench campaigns": CliOnly(
        "V0.9 G3: lists campaigns (admin); the Benchmarks page (G6, D221) is their UI."
    ),
    "bench runs": CliOnly(
        "V0.9 G3: a campaign's run list (admin); G6's campaign detail shows it."
    ),
    "bench labels": CliOnly(
        "V0.9 G5: a campaign's per-group labels (admin) and their Parquet "
        "download; G6's campaign detail (D221) shows them."
    ),
    "bench submit": CliOnly(
        "V0.9 G3: the only campaign driver until G5's runner (D232); G6's "
        "pause/resume controls replace it in the UI."
    ),
    "bench start": CliOnly(
        "V0.9 G5: hands a campaign to the controller's runner (admin); the "
        "Benchmarks page (G6, D221) adds the start control."
    ),
    "bench pause": CliOnly(
        "V0.9 G5: stops a campaign's submissions (admin); G6's pause control."
    ),
    "bench resume": CliOnly(
        "V0.9 G5: resumes a paused campaign (admin); G6's resume control."
    ),
    "bench cancel": CliOnly(
        "V0.9 G5: cancels a campaign's benchmark jobs (admin); G6's cancel control."
    ),
    "bench status": CliOnly(
        "V0.9 G5: a campaign's progress, ETA and infra failure rate (admin); G6's "
        "campaign list shows the same numbers (campaign.progress events)."
    ),
    "telemetry export": CliOnly(
        "V0.9 G1: downloads the run-record dataset as a Parquet file for analysis "
        "tools (admin). The Benchmarks page (0.9.9, G6) adds the browser download "
        "(D221) and moves this entry to a UI mapping then."
    ),
}

#: ``meshforge-controller user ...`` (D176 "users (admin)").
_NO_USERS_API = (
    "user management runs on the controller host against its database (V0.7 "
    "D141); there is no /v1/users REST API, so the admin Users page lists these "
    "commands and invents nothing (G5 STOP item, D203). A browser version needs "
    "a new admin API first - Lead decision."
)
CONTROLLER_USER: Final[dict[str, Entry]] = {
    "create": CliOnly(_NO_USERS_API),
    "list": CliOnly(_NO_USERS_API),
    "disable": CliOnly(_NO_USERS_API),
    "enable": CliOnly(_NO_USERS_API),
    "reset-password": CliOnly(_NO_USERS_API),
}
