"""Jobs and attempts on the controller (V0.4, brief D49/D50).

* :mod:`.transitions` - the §12 state machines as data (pure).
* :mod:`.store` - the single compare-and-set writer of the job tables.
* :mod:`.placement` - manual placement validation for ``POST /v1/jobs``.

Imports nothing at package level: the controller package must stay importable on
a base install.
"""
