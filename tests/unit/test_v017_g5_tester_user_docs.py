"""V0.17 G5 tester (brief §5e item 6, D396): the user documentation cannot rot.

Independent of the implementer's code-set test (which checks the ``Code:``
lines): here the doc is checked from the READER's side.

* every command a reader is told to type exists in the shipped CLIs (also in
  the messages the installers and packages print, which send readers to
  commands too);
* the message a troubleshooting heading quotes is really something MeshForge
  prints;
* relative links and image references resolve; no page is orphaned from the
  index; every ``Code:`` is documented once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]
USER: Final = REPO / "docs" / "user"
PAGES: Final = sorted(USER.glob("*.md"))

# where a reader is sent to a command by something other than the docs
MESSAGE_SOURCES: Final = [
    REPO / "src/meshforge/agent/winsetup.py",
    REPO / "src/meshforge/agent/doctor.py",
    REPO / "src/meshforge/common/upgrade_state.py",
    REPO / "src/meshforge/agent/console/joining.py",
    *sorted((REPO / "packaging/linux/scripts").glob("*")),
    *sorted((REPO / "packaging/linux/controller/scripts").glob("*")),
    REPO / "packaging/linux/common/upgrade.sh",
]

_CMD: Final = re.compile(
    r"\b(meshforge-agent|meshforge-controller|compute)((?: [a-z][a-z0-9-]*){1,2})"
)


def _tree(app: object) -> dict[str, object]:
    import typer.main

    command = typer.main.get_command(app)  # type: ignore[arg-type]
    return {"": command}


def _resolve(program: str, words: list[str]) -> tuple[bool, str]:
    """True when ``program words...`` reaches a real command or group (the
    first word that is not a subcommand ends the check as 'an argument')."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    pytest.importorskip("cryptography", reason="needs the agent extra")
    import typer.main

    if program == "meshforge-agent":
        from meshforge.agent.main import app
    elif program == "meshforge-controller":
        from meshforge.controller.app import app
    else:
        from meshforge.cli.app import app
    node = typer.main.get_command(app)
    seen = []
    for word in words:
        if not hasattr(node, "commands"):
            return True, " ".join(seen)  # the rest are arguments
        child = node.commands.get(word)
        if child is None:
            return False, " ".join([*seen, word])
        seen.append(word)
        node = child
    return True, " ".join(seen)


def _commands_in(text: str) -> set[tuple[str, tuple[str, ...]]]:
    found: set[tuple[str, tuple[str, ...]]] = set()
    for match in _CMD.finditer(text):
        program = match.group(1)
        words = match.group(2).split()
        # only inside code spans / shell lines, not prose such as
        # "meshforge-agent with node_id": require a backtick, `$`, `sudo`, `say`
        # or a quote right before the program name
        before = text[max(0, match.start() - 12) : match.start()]
        if not re.search(r"(`|\$ |sudo |say \"|\"|'|# )\s*$", before):
            continue
        found.add((program, tuple(words)))
    return found


# the words that follow a program name in prose / flags, not subcommands
_NOT_COMMANDS: Final = {
    ("meshforge-agent", ("with",)),
    ("meshforge-agent", ("is",)),
    ("meshforge-agent", ("-n",)),
    ("meshforge-controller", ("-n",)),
    ("meshforge-controller", ("is",)),
    ("meshforge-controller", ("may",)),
}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_command_a_page_tells_the_reader_to_type_exists(page: Path) -> None:
    bad = []
    for program, words in sorted(_commands_in(page.read_text(encoding="utf-8"))):
        if (program, words[:1]) in _NOT_COMMANDS or words[0].startswith("-"):
            continue
        ok, where = _resolve(program, list(words))
        if not ok:
            bad.append(f"{program} {' '.join(words)}  (no such command: {where})")
    assert bad == [], f"{page.name}: {bad}"


def test_defect_g5_t4_messages_name_commands_that_exist() -> None:
    text = (REPO / "src/meshforge/agent/winsetup.py").read_text(encoding="utf-8")
    ok, where = _resolve("meshforge-agent", ["console-access"])
    assert ok, f"winsetup.py names 'meshforge-agent console-access' ({where})"
    assert "meshforge-agent console-access" in text  # keeps the xfail honest


# ------------------------------------------------ quoted messages are real
def _quoted_heading_messages(text: str) -> list[tuple[str, str]]:
    out = []
    for heading in re.findall(r"^### (.+)$", text, re.MULTILINE):
        for quoted in re.findall(r'"([^"]{12,})"', heading):
            stable = re.split(r"\.\.\.|…", quoted)[0].strip()
            if len(stable) >= 12:
                out.append((heading, stable))
    return out


def _all_source_text() -> str:
    chunks = []
    for root in ("src", "packaging", "frontend/src"):
        for path in (REPO / root).rglob("*"):
            if path.is_file() and path.suffix in {
                ".py",
                ".sh",
                ".ts",
                ".tsx",
                ".iss",
                "",
            }:
                try:
                    chunks.append(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, OSError):
                    continue
    return "\n".join(chunks)


def test_every_message_quoted_in_a_troubleshooting_heading_is_really_printed() -> None:
    sources = _all_source_text()
    # headings quote the message in the reader's capitalisation; code may differ
    # in case, so compare case-insensitively on the stable first part
    lowered = sources.lower()
    os_wording = {"windows protected your pc", "the network is public"}  # Windows's
    missing = [
        (heading, stable)
        for heading, stable in _quoted_heading_messages(
            (USER / "troubleshooting.md").read_text(encoding="utf-8")
        )
        if re.split(r"\d", stable)[0].lower() not in lowered
        and stable.lower() not in os_wording
    ]
    assert missing == [], f"quoted but never printed by MeshForge: {missing}"


# ------------------------------------------------------------ links/images
_LINK: Final = re.compile(r"(?<!!)\[[^\]]*\]\(([^)#\s]+)(#[^)]*)?\)")
_IMAGE: Final = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_relative_links_resolve(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    bad = []
    for target, _anchor in _LINK.findall(text):
        if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
            continue
        if not (page.parent / target).resolve().exists():
            bad.append(target)
    assert bad == [], f"{page.name}: dead links {bad}"


def test_images_exist_unless_they_are_a_listed_lab_placeholder() -> None:
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for target in _IMAGE.findall(text):
            if (page.parent / target).exists():
                continue
            name = Path(target).name
            assert f"TODO-SCREENSHOT: {name}" in text, f"{page.name}: {target} missing"


def test_every_page_is_reachable_from_the_index() -> None:
    index = (USER / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in PAGES if p.name != "README.md" and p.name not in index]
    assert missing == [], f"not linked from docs/user/README.md: {missing}"


def test_no_draft_markers_ship() -> None:
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for marker in ("DRAFT", "FIXME", "XXX", "lorem ipsum", "TBD"):
            assert marker not in text, f"{page.name} contains {marker}"


def test_each_code_is_documented_once() -> None:
    text = (USER / "troubleshooting.md").read_text(encoding="utf-8")
    codes = []
    for line in re.findall(r"^Codes?: (.+)$", text, re.MULTILINE):
        codes.extend(re.findall(r"`([a-z_]+)`", line))
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    assert dupes == [], f"documented twice: {dupes}"
