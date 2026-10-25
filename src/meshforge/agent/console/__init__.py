"""The laptop owner's local console (V0.17 G3, roadmap 0.17.4 + 0.17.5; D388, D391, D392).

The dashboard lives on the controller; a laptop that has not joined yet has no
controller to open. So the agent serves its **own** small web UI, built from the
same React app (``frontend/src/agentConsole/``): the first-run wizard (join with
a code or a QR, the owner policy with safe defaults, the checks), the owner
settings, the agent's recent log and "leave this fabric".

Where it runs: inside the agent process (``meshforge-agent run``), on
``127.0.0.1:47130`` **only** (``[agent.console] bind`` refuses anything else).
With the console on, the agent no longer exits when the laptop is not joined:
:mod:`.supervisor` keeps the console up and starts the fabric part once the
laptop joins, stops it on "leave", restarts it in-process when the owner asks,
and keeps the console up (showing why) after a typed stop such as a revoked
node. Only an unexpected crash still exits (the service manager restarts it).

Who may use it (:mod:`.auth`): whoever can read the access token file - the
service user and members of ``meshforge-admin`` (Linux: ``/run/meshforge-console
/token``, ``0640``, rotated at every start). The launcher
(``meshforge-console-open``, :mod:`.launcher`) trades it for a one-time code and
opens ``http://127.0.0.1:47130/#c=<code>``; the page exchanges the code for an
``HttpOnly; SameSite=Strict`` cookie and drops the fragment. Every request must
carry ``Host: 127.0.0.1:47130`` (DNS-rebinding guard) and every write an
``Origin`` of the same (CSRF guard). The LAN cannot reach the socket at all.

Modules: :mod:`.auth` (token, launch codes, sessions, Host/Origin), :mod:`.app`
(the FastAPI routes ``/console/v1/...`` + the SPA), :mod:`.supervisor` (the
fabric part's lifecycle), :mod:`.checks` (Docker/GPU/disk/firewall/peer, D388),
:mod:`.joining` (join with plain-language errors), :mod:`.node_name`,
:mod:`.logbuffer` (the agent's own redacted log tail), :mod:`.server` (uvicorn in
the agent's event loop), :mod:`.launcher`.

FastAPI and uvicorn come with the ``agent`` extra (Q23); nothing here is
imported by ``meshforge-agent --version`` or by an agent with the console off.
"""
