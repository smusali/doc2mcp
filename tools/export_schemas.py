from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp4api.curate.models import Manifest
from mcp4api.model import ApiSpec
from mcp4api.report import Report
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[1]
_DIRECTORY = _ROOT / "docs" / "schemas"
_MODELS: dict[str, type[BaseModel]] = {
    "endpoints": ApiSpec,
    "tools": Manifest,
    "verification": Report,
}


def render() -> dict[str, str]:
    """Build the published schema documents.

    Returns:
        File name to JSON text, with sorted keys and a trailing newline.
    """
    documents: dict[str, str] = {}
    for name, model in _MODELS.items():
        payload = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        documents[f"{name}.schema.json"] = payload
    return documents


def main(argv: list[str] | None = None) -> int:
    """Write the schemas, or fail when the files differ.

    Args:
        argv: Process arguments. ``--check`` compares without writing.

    Returns:
        0 when the schemas match, 1 when they differ.
    """
    check = "--check" in (sys.argv[1:] if argv is None else argv)
    documents = render()
    _DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, payload in documents.items():
        path = _DIRECTORY / name
        if check:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != payload:
                print(f"{path} does not match the models", file=sys.stderr)
                return 1
            continue
        path.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
