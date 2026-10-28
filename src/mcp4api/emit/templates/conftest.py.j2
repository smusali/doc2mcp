from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def results() -> dict[str, object]:
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    from suite import execute

    loaded: object = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    found = execute(loaded)
    if not isinstance(found, dict):
        raise TypeError("results must be an object")
    return found
