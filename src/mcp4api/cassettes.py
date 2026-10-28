from __future__ import annotations

import os
import re
from pathlib import Path

from mcp4api.model import JsonObject, JsonValue, dump_json

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|AUTH)", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


def write_cassettes(out: Path, results: JsonObject, secrets: list[str]) -> None:
    """Write one scrubbed cassette per tool.

    Args:
        out: Artifact root. Cassettes are written under ``tests/cassettes``.
        results: Verification result object with a ``calls`` array.
        secrets: Secret values to replace before the cassette is stored.

    Returns:
        None.
    """
    grouped: dict[str, list[JsonValue]] = {}
    for call in _result_calls(results):
        if call.get("kind") == "negative":
            continue
        tool = call.get("tool")
        exchanges = call.get("exchanges")
        if not isinstance(tool, str) or not isinstance(exchanges, list):
            continue
        grouped.setdefault(tool, []).extend(item for item in exchanges if isinstance(item, dict))
    directory = out / "tests" / "cassettes"
    directory.mkdir(parents=True, exist_ok=True)
    for tool, rows in grouped.items():
        cleaned = scrub(rows, secrets)
        if not isinstance(cleaned, list):
            continue
        (directory / f"{tool}.json").write_text(
            dump_json({"exchanges": cleaned}),
            encoding="utf-8",
            newline="\n",
        )


def secret_values(names: tuple[str, ...]) -> list[str]:
    """Collect secret environment values that cassettes must not keep.

    Args:
        names: Environment variable names known to hold credentials.

    Returns:
        Secret values, longest first.
    """
    wanted = set(names)
    found: list[str] = []
    for name, value in os.environ.items():
        if (name in wanted or _SECRET_NAME.search(name)) and len(value) >= 4:
            found.append(value)
    return sorted(set(found), key=len, reverse=True)


def scrub(value: JsonValue, secrets: list[str]) -> JsonValue:
    """Replace secrets and bearer tokens in a JSON value.

    Args:
        value: JSON value to scrub.
        secrets: Secret strings to replace with ``***``.

    Returns:
        A copy of the value with those secrets removed.
    """
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, list):
        return [scrub(item, secrets) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            result[_scrub_text(key, secrets)] = scrub(item, secrets)
        return result
    return value


def _result_calls(results: JsonObject) -> list[JsonObject]:
    raw = results.get("calls")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _scrub_text(value: str, secrets: list[str]) -> str:
    text = value
    for secret in secrets:
        text = text.replace(secret, "***")
    return _BEARER.sub("Bearer ***", text)
