"""Export the controller's OpenAPI document (V0.8 G1, D171) - no server needed.

    uv run python scripts/export_openapi.py                 # to stdout
    uv run python scripts/export_openapi.py --out FILE      # write FILE
    uv run python scripts/export_openapi.py --check FILE    # exit 1 if FILE differs
    uv run python scripts/export_openapi.py --console ...   # the agent console's API
                                                            # (V0.17 G3) instead

The output is deterministic (sorted keys, two-space indent, trailing newline),
so the dashboard's generated TypeScript client (``openapi-typescript``) and its
drift test can compare it byte for byte: a backend change that alters the
contract fails CI instead of the dashboard at evaluation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def render(*, console: bool = False) -> str:
    if console:
        from meshforge.agent.console.app import openapi_document as console_document

        document = console_document()
    else:
        from meshforge.controller.http import openapi_document

        document = openapi_document()
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--out", type=Path, help="write the document to this file")
    group.add_argument(
        "--check", type=Path, help="compare with this file; exit 1 when it differs"
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="the agent's local console API (frontend/console-openapi.json)",
    )
    args = parser.parse_args(argv)
    text = render(console=args.console)
    if args.check is not None:
        try:
            current = args.check.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"export_openapi: cannot read {args.check}: {exc}", file=sys.stderr)
            return 1
        if current != text:
            print(
                f"export_openapi: {args.check} is out of date - run "
                f"`uv run python scripts/export_openapi.py --out {args.check}`",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
