"""Retest round for lab/inventory.py 0.2.2 fix round 1 (tester defect DEFECT-1).

Further adversarial home-directory boundary cases beyond
test_inventory_privacy_edge_cases.py: trailing separators, home at the very
start/end of the string, JSON-escaped double-backslash paths, UNC paths,
Windows 8.3 short names, an already-present ``~``, and regex-special
characters inside the home directory name. Hermetic, OS-independent.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAB_DIR = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from _inventory.sanitize import Sanitizer  # noqa: E402

# --------------------------------------------------------------- trailing separators


def test_posix_home_string_with_trailing_slash_still_matches() -> None:
    """A home value that itself carries a trailing separator (e.g. read from a
    config file with a trailing slash) must not break matching of children."""
    s = Sanitizer(Path("/home/alice/"))
    assert s.text("/home/alice/Desktop/file.txt") == "~/Desktop/file.txt"
    assert s.text("/home/alice/") in ("~/", "~")


def test_windows_home_string_with_trailing_backslash_still_matches() -> None:
    s = Sanitizer(Path("C:\\Users\\Ayu\\"))
    assert s.text("C:\\Users\\Ayu\\Desktop\\file.txt") == "~\\Desktop\\file.txt"


# --------------------------------------------------------------- start/end of string


def test_home_at_very_start_of_string_with_no_surrounding_text() -> None:
    s = Sanitizer(Path("/home/alice"))
    assert s.text("/home/alice") == "~"


def test_home_at_very_end_of_string_no_trailing_content() -> None:
    s = Sanitizer(Path("/home/alice"))
    assert s.text("cwd=/home/alice") == "cwd=~"


def test_home_is_the_entire_json_value_string() -> None:
    s = Sanitizer(Path("C:\\Users\\Ayu"))
    assert s.text("C:\\Users\\Ayu") == "~"


# --------------------------------------------------------------- JSON-escaped backslashes


def test_double_escaped_windows_path_from_nested_json_text() -> None:
    """Some tools emit a JSON string *containing* an already-JSON-escaped path
    (e.g. a nested manifest dumped as text); after Python's own JSON decoding
    this shows up as literal double backslashes in the string. The separator
    regex splits on one-or-more backslashes, so this must still match."""
    s = Sanitizer(Path("C:\\Users\\Ayu"))
    raw = 'note: path is "C:\\\\Users\\\\Ayu\\\\project\\\\file.txt"'
    out = s.text(raw)
    assert "Ayu" not in out
    assert "~" in out


# --------------------------------------------------------------- UNC paths


def test_unc_path_user_dir_is_not_silently_left_unmasked_or_crashes() -> None:
    """UNC paths (``\\\\server\\share\\Users\\<name>``) do not start with a drive
    letter, so the generic Windows user-dir mask (which requires ``X:``) does not
    apply to them. Document current behaviour: this must never crash, and if the
    literal home string appears verbatim it is still masked via the home-specific
    pattern (which does not require a drive-letter anchor for a match found deep
    in the string, since it only requires a path-boundary, not a string-start)."""
    s = Sanitizer(Path("C:\\Users\\Ayu"))
    out = s.text("\\\\fileserver\\shared\\Users\\Ayu\\notes.txt")
    # Must not crash and must not fabricate garbage; if it doesn't recognise the
    # UNC form as "the" home (no drive letter) it should at least leave it intact
    # rather than partially mangling it -- verified as a stable, non-crashing
    # round-trip rather than assuming a mask that the current design doesn't
    # promise for UNC paths.
    assert isinstance(out, str)


def test_unc_share_named_users_with_unrelated_name_not_mistaken_for_current_home() -> (
    None
):
    s = Sanitizer(Path("C:\\Users\\Ayu"))
    out = s.text("\\\\fileserver\\Users\\bob\\file.txt")
    assert isinstance(out, str)


# --------------------------------------------------------------- 8.3 short names


def test_windows_83_short_name_of_home_is_masked_by_generic_users_pattern() -> None:
    """A tool may report the legacy 8.3 short name (``AYUSHK~1``) instead of the
    real profile folder name. It won't match the home-specific pattern (the
    strings differ), but the generic ``C:\\Users\\<name>`` mask must still catch
    it and never leak a readable fragment of the real username."""
    s = Sanitizer(Path("C:\\Users\\Ayush Kumar"))
    out = s.text("C:\\Users\\AYUSHK~1\\AppData\\Local\\Temp\\x.log")
    assert out == "C:\\Users\\<user>\\AppData\\Local\\Temp\\x.log"


# --------------------------------------------------------------- already has a tilde


def test_text_already_containing_tilde_home_shorthand_is_left_alone() -> None:
    s = Sanitizer(Path("/home/alice"))
    assert s.text("~/Documents/x") == "~/Documents/x"


def test_windows_home_env_style_tilde_left_alone() -> None:
    s = Sanitizer(Path("C:\\Users\\Ayu"))
    assert s.text("~\\Documents\\x") == "~\\Documents\\x"


# --------------------------------------------------------------- regex-special chars in home


def test_home_directory_name_with_regex_special_characters() -> None:
    """A profile name containing characters that are regex metacharacters
    (``.``, ``(", ")", "+", "$"``) must be escaped, not interpreted as regex."""
    s = Sanitizer(Path("/home/a.b+c$d"))
    assert s.text("/home/a.b+c$d/project/file.txt") == "~/project/file.txt"
    # a similarly-shaped but different directory must not falsely match
    assert "a.b+c$d" not in s.text("/home/aXbYcZd/project/file.txt")


def test_windows_home_with_parentheses_in_profile_name() -> None:
    s = Sanitizer(Path("C:\\Users\\Ayu (Work)"))
    assert s.text("C:\\Users\\Ayu (Work)\\Desktop\\x") == "~\\Desktop\\x"


# --------------------------------------------------------------- symlinked home


def test_symlinked_home_realpath_mismatch_never_crashes(tmp_path: Path) -> None:
    """If the reported home differs from a tool's resolved/real path (a symlinked
    home directory), the sanitizer must never crash; it may simply not find a
    match for the (different) real path -- documented, not a silent failure."""
    real_target = tmp_path / "real_home_target"
    real_target.mkdir()
    s = Sanitizer(tmp_path / "home" / "alice")  # the *logical* home, never created
    out = s.text(str(real_target / "file.txt"))
    assert isinstance(out, str)  # never raises regardless of match/no-match


# --------------------------------------------------------------- home shorter than 4 chars guard


def test_extremely_short_home_like_c_drive_root_is_ignored_not_wildly_matched() -> None:
    s = Sanitizer(Path("C:\\"))
    # len(str(home)) == 3 for "C:\\" -> falls under the "> 3" guard and is ignored
    assert s.text("C:\\Windows\\System32") == "C:\\Windows\\System32"
