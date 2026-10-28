from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"
_INLINE = re.compile(r"\]\(([^)]*)\)")
_REFERENCE = re.compile(r"(?m)^ {0,3}\[[^]]+\]: +(\S+)")


def main(argv: list[str]) -> int:
    """Check that README links and images use absolute URLs.

    Args:
        argv: Process arguments. None are accepted.

    Returns:
        0 when every link and image URL is absolute, 1 when one is not, and 2
        when the arguments are wrong.
    """
    if len(argv) != 1:
        print("usage: check_pypi_readme.py", file=sys.stderr)
        return 2
    prose = _strip_inline(_prose(_README.read_text(encoding="utf-8")))
    targets = [_target(raw) for raw in _INLINE.findall(prose)]
    targets.extend(_target(raw) for raw in _REFERENCE.findall(prose))
    failed = False
    for url in targets:
        if _absolute(url):
            continue
        print(f"README URL is not absolute: {url}", file=sys.stderr)
        failed = True
    if failed:
        return 1
    return 0


def _prose(text: str) -> str:
    lines: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return "".join(lines)


def _strip_inline(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


def _target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<"):
        end = value.find(">")
        if end != -1:
            return value[1:end].strip()
    if not value:
        return value
    return value.split()[0]


def _absolute(url: str) -> bool:
    if url.startswith("#"):
        return True
    lowered = url.lower()
    return lowered.startswith("https://") or lowered.startswith("http://")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
