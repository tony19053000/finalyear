"""REST user authentication (V0.7 G4, roadmap 0.7.6, D141).

* :mod:`.passwords` - scrypt password hashing (stdlib, off the event loop);
* :mod:`.users` - ``app_user`` / ``api_token`` storage and token authentication;
* :mod:`.login` - ``POST /v1/auth/login`` with per-source and per-user backoff;
* :mod:`.principal` - who is calling, and the one owner rule for jobs.

The HTTP side (the single auth dependency, the ``/v1/auth`` routes) lives in
:mod:`meshforge.controller.api.auth` and :mod:`meshforge.controller.api.auth_routes`.
"""
