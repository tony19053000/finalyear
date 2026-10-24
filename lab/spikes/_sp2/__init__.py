"""Spike SP2 (roadmap 0.17.0, D390/D393): managed PostgreSQL measurement code.

``bundle`` fetches/verifies/trims the pinned build; ``crash_child`` is the
"controller" process the crash scenario SIGKILLs; ``scenarios`` holds the measured
runs used by ``lab/spikes/sp2_managed_pg.py`` and the component tests.
"""
