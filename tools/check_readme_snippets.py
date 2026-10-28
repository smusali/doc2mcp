from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"
_SERVER = _ROOT / "examples" / "sample-api" / "generated" / "shelf_mcp" / "server.py"
_FENCE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def main(argv: list[str]) -> int:
    """Check README Python fences against the generated server.

    Args:
        argv: Process arguments. None are accepted.

    Returns:
        0 when every fence is an excerpt of the generated server, 1 when a
        fence does not match, and 2 when the arguments are wrong.
    """
    if len(argv) != 1:
        print("usage: check_readme_snippets.py", file=sys.stderr)
        return 2
    readme = _README.read_text(encoding="utf-8")
    server = _SERVER.read_text(encoding="utf-8")
    fences = _FENCE.findall(readme)
    if not fences:
        print("README has no python fence", file=sys.stderr)
        return 1
    for fence in fences:
        if fence not in server:
            print(
                "README python fence is not an excerpt of "
                "examples/sample-api/generated/shelf_mcp/server.py",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
