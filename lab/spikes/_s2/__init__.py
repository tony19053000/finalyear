"""Spike S2 (roadmap 0.2.6): cross-OS reachability without WSL.

* :mod:`.protocol` - ports, message shapes, keepalive option sets, typed
  failures and the fix hint that goes with each.
* :mod:`.env` - "is this a native OS or WSL?", host facts, firewall profile.
* :mod:`.httpsrv` / :mod:`.grpcsrv` / :mod:`.server` - the two servers a peer
  dials into, and the supervisor that bounds their lifetime.
* :mod:`.client` - the measurements (HTTP GET / stream, gRPC unary /
  server-stream / long-lived bidi).
* :mod:`.run` - one client run -> result envelope + per-direction verdicts.
* :mod:`.verdict` - aggregation of several runs into the report tables.
"""
