"""Privacy filters applied to the whole document before it is written (LAB.md §6).

Kept: hostname, MAC addresses, private/link-local IPv4. Removed: usernames in
paths (home directory -> ``~``, matched only at a path boundary, case-insensitively
and with either separator for Windows homes; other ``/home/<x>`` /
``C:\\Users\\<x>`` -> ``<user>``),
public IPv4 addresses, and anything :func:`meshforge.common.redaction.scrub_text`
recognises as a secret (tokens, ``password=...``, URL credentials).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

from _inventory.model import JSON

#: POSIX user names contain no whitespace; Windows profile folders may ("Aayush Kumar").
_POSIX_USER_DIR_RE: Final = re.compile(
    r"(?P<prefix>/home/|/Users/)(?P<user>[^/\s\"']+)"
)
_WIN_USER_DIR_RE: Final = re.compile(
    r"(?P<prefix>[A-Za-z]:(?:\\+|/)Users(?:\\+|/))(?P<user>[^/\\\"'\r\n]+)"
)
_IPV4_RE: Final = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_SKIP_USER_DIRS: Final = frozenset({"public", "default", "all users", "shared"})


def _load_scrubber() -> Callable[[str], str] | None:
    try:
        from meshforge.common.redaction import scrub_text
    except ImportError:
        return None
    return scrub_text


_SCRUB: Final = _load_scrubber()


def redaction_source() -> str:
    """Which secret scrubber is active (recorded in the document)."""
    return "meshforge.common.redaction" if _SCRUB is not None else "builtin-only"


def is_shareable_ipv4(text: str) -> bool:
    """True for private, loopback and link-local IPv4 (never a public address)."""
    try:
        addr = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _mask_public_ip(match: re.Match[str]) -> str:
    text = match.group(0)
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return text  # e.g. a version string like 32.0.15.6094 with a >255 part
    return text if is_shareable_ipv4(text) else "<public-ip>"


def _mask_user_dir(match: re.Match[str]) -> str:
    if match.group("user").lower() in _SKIP_USER_DIRS:
        return match.group(0)
    return match.group("prefix") + "<user>"


#: Characters that may continue a path component. The home directory is replaced
#: only when it is *not* followed by one of these (so ``/home/aayush`` never
#: matches inside ``/home/aayushkumar`` or ``/home/aayush-admin``).
_POSIX_NAME_CONT: Final = r"[^/\\\s\"'`,;:|()\[\]{}<>]"
#: Windows profile folders may contain spaces ("Aayush Kumar"), so a space does not
#: end the home directory there; a folder like "C:\Users\Ayu Kumar" is then left to
#: the generic ``C:\Users\<user>`` mask instead of becoming "~ Kumar".
_WIN_NAME_CONT: Final = r"[^/\\\"'`,;|()\[\]{}<>\r\n\t]"
#: The home directory must also start at a boundary (``/mnt/home/alice`` is not it).
_HOME_START: Final = r"(?<![\w.\-~])"
_WIN_HOME_RE: Final = re.compile(r"^[A-Za-z]:[\\/]")


def _home_pattern(home: str) -> re.Pattern[str] | None:
    """Regex for the home directory at path boundaries, either separator."""
    # A Windows home has a drive ("C:\\") or is a UNC path. A backslash alone
    # does not make it one: on Windows, str(Path("/home/alice")) is
    # "\\home\\alice" - still a POSIX home (e.g. a WSL path being sanitised).
    windows = bool(_WIN_HOME_RE.match(home)) or home.startswith("\\\\")
    parts = [p for p in re.split(r"[\\/]+", home) if p]
    if not parts:
        return None
    lead = "" if windows else r"[\\/]"
    body = r"[\\/]+".join(re.escape(p) for p in parts)
    cont = _WIN_NAME_CONT if windows else _POSIX_NAME_CONT
    flags = re.IGNORECASE if windows else 0
    return re.compile(f"{_HOME_START}{lead}{body}(?!{cont})", flags)


class Sanitizer:
    """Rewrites strings; ``home`` is the current user's home directory."""

    def __init__(self, home: Path | None) -> None:
        self._home_re: re.Pattern[str] | None = None
        if home is not None and len(str(home)) > 3:
            self._home_re = _home_pattern(str(home))

    def text(self, value: str) -> str:
        if self._home_re is not None:
            value = self._home_re.sub("~", value)
        value = _WIN_USER_DIR_RE.sub(_mask_user_dir, value)
        value = _POSIX_USER_DIR_RE.sub(_mask_user_dir, value)
        value = _IPV4_RE.sub(_mask_public_ip, value)
        if _SCRUB is not None:
            value = _SCRUB(value)
        return value

    def tree(self, value: JSON) -> JSON:
        """Sanitize every string (keys included) in a JSON-compatible tree."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.tree(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.tree(v) for v in value]
        return value
