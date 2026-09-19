"""S1 network measurement (roadmap 0.2.5): TCP throughput + UDP RTT/jitter/loss.

Pure standard library so it runs after a plain ``uv sync`` on Linux and native
Windows. No CLI coupling: :mod:`.server` and :mod:`.client` expose plain
functions/classes so the V0.3 agent network probe can adopt them later;
:mod:`.matrix` turns result envelopes into the docs/LAB.md §2 tables.
"""
