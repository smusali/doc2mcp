from __future__ import annotations

import json
from pathlib import Path

import pytest


def _ids() -> list[str]:
    path = Path(__file__).resolve().parent / "plan.json"
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return []
    calls = loaded.get("calls")
    if not isinstance(calls, list):
        return []
    found: list[str] = []
    for call in calls:
        if isinstance(call, dict):
            name = call.get("id")
            if isinstance(name, str):
                found.append(name)
    return found


def test_suite_loaded(results: dict[str, object]) -> None:
    assert isinstance(results.get("calls"), list)


@pytest.mark.parametrize("call_id", _ids() or ["none"])
def test_call(results: dict[str, object], call_id: str) -> None:
    if call_id == "none":
        return
    calls = results.get("calls")
    assert isinstance(calls, list)
    for call in calls:
        if isinstance(call, dict) and call.get("id") == call_id:
            assert call.get("ok") is True
            return
    raise AssertionError(call_id)
