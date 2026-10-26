"""The dashboard's built files, served by the controller (V0.8 G1, D169).

``npm run build`` in ``frontend/`` writes into ``dist/`` here (git-ignored;
included in the wheel as a build artifact). :mod:`.spa` serves it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

DIST_DIR: Final = Path(__file__).resolve().parent / "dist"
"""Where the built dashboard lives. Here, not in :mod:`.spa`, so that
``meshforge-controller doctor`` can check it without the controller extra
(:mod:`.spa` imports starlette)."""
