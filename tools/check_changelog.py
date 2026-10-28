from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_UNRELEASED = "## [Unreleased]"
_ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_BULLET = re.compile(r"^- (.+)$")
_SENTENCE = re.compile(r"\. [A-Z]")
_LIMIT = 140


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
        print("src/mcp4api/__init__.py is missing __version__", file=sys.stderr)
        return 1
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if _problems(changelog):
        return 1
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


def _problems(text: str) -> bool:
    failed = False
    ranks: list[int] = []
    for line in text.splitlines():
        if line.startswith("## "):
            ranks = []
            continue
        if line.startswith("### "):
            name = line.removeprefix("### ").strip()
            if name not in _ORDER:
                print(f"unknown changelog section {name}", file=sys.stderr)
                failed = True
                continue
            rank = _ORDER.index(name)
            if ranks and rank < ranks[-1]:
                print(f"section {name} is out of order", file=sys.stderr)
                failed = True
            ranks.append(rank)
            continue
        match = _BULLET.match(line)
        if match is None:
            continue
        bullet = match.group(1)
        if len(bullet) > _LIMIT or not bullet.endswith(".") or _SENTENCE.search(bullet):
            print(
                f"bullet is not one sentence of at most {_LIMIT} characters: {bullet}",
                file=sys.stderr,
            )
            failed = True
    return failed


def _read_version() -> str | None:
    text = (_ROOT / "src" / "mcp4api" / "__init__.py").read_text(encoding="utf-8")
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
