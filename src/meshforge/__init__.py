"""MeshForge: intelligent distributed compute fabric for heterogeneous personal devices."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("meshforge")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
