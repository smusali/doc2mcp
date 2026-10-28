from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BANNED = ("uv.lock", ".github/", "demo.gif")


def main() -> int:
    """Check the built wheel and sdist.

    Returns:
        0 when the archives have the expected members, 1 otherwise.
    """
    wheels = sorted(Path("dist").glob("*.whl"))
    sdists = sorted(Path("dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        print(f"expected one wheel and one sdist, found {wheels} {sdists}")
        return 1
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    templates = sorted((_ROOT / "src" / "mcp4api" / "emit" / "templates").glob("*.j2"))
    needed = [f"mcp4api/emit/templates/{path.name}" for path in templates]
    needed.extend(
        (
            "mcp4api/curate/criteria.md",
            "mcp4api/suite.py",
            "mcp4api/py.typed",
        )
    )
    missing = [name for name in needed if name not in names]
    if missing:
        print(f"wheel is missing {missing}")
        return 1
    with tarfile.open(sdists[0]) as archive:
        packed = archive.getnames()
    banned = [token for token in _BANNED if any(token in name for name in packed)]
    if banned:
        print(f"sdist contains {banned}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
