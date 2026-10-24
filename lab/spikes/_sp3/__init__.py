"""Spike SP3 (V0.17 G0, roadmap 0.17.0): the D384 relocatable bundle.

* :mod:`.checks` - run bundle commands in a clean environment (no uv, no system
  Python on ``PATH``) and time them; shared with the component test.
* :mod:`.env_probe` - runs *inside* the bundled interpreter: one real env build
  through the agent's :class:`~meshforge.agent.envs.builder.EnvBuilder` with the
  bundle's own ``uv``.

CLI: ``lab/spikes/sp3_bundle.py``; results: ``lab/spikes/SP3.md``.
"""
