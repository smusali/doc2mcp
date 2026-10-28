from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_UNRELEASED = "## [Unreleased]"


def main(argv: list[str]) -> int:
    """Check the changelog against the package version.

    Args:
        argv: Process arguments. Pass ``v<version>`` to require a dated heading.

    Returns:
        0 when the check passes, 1 when it fails, and 2 when the arguments are wrong.
    """
    if len(argv) > 2:
        print("usage: check_changelog.py [v<version>]", file=sys.stderr)
        return 2
    version = _read_version()
    if version is None:
        print("mcp4api/__init__.py is missing __version__", file=sys.stderr)
        return 1
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if len(argv) == 1:
        if not _has_heading(changelog, _UNRELEASED):
            print("CHANGELOG.md is missing [Unreleased]", file=sys.stderr)
            return 1
        return 0
    tag = argv[1]
    if tag != f"v{version}":
        print(f"tag {tag} does not match version {version}", file=sys.stderr)
        return 1
    heading = f"## [{version}] - "
    if not any(_is_dated(line, heading) for line in changelog.splitlines()):
        print(f"CHANGELOG.md has no dated section for {version}", file=sys.stderr)
        return 1
    return 0


def _read_version() -> str | None:
    text = (_ROOT / "mcp4api" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'(?m)^__version__ = "([^"]+)"', text)
    if match is None:
        return None
    return match.group(1)


def _has_heading(text: str, heading: str) -> bool:
    return any(line.strip() == heading for line in text.splitlines())


def _is_dated(line: str, prefix: str) -> bool:
    if not line.startswith(prefix):
        return False
    date = line[len(prefix) :].strip()
    return re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is not None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
