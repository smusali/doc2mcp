from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from mcp4api import __version__
from mcp4api.cli import main
from mcp4api.errors import ExitCode

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == ExitCode.OK
    assert capsys.readouterr().out.strip() == f"mcp4api {__version__}"


def test_help_lists_stages(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == ExitCode.OK
    output = _ANSI.sub("", capsys.readouterr().out)
    for name in ("ingest", "curate", "emit", "verify", "record"):
        assert name in output
    assert "docs URL" not in output
    assert main(["ingest", "--help"]) == ExitCode.OK
    ingest_help = _ANSI.sub("", capsys.readouterr().out)
    assert "--offline" in ingest_help
    assert "--allow-ref-host" in ingest_help
    assert main(["curate", "--help"]) == ExitCode.OK
    curate_help = _ANSI.sub("", capsys.readouterr().out)
    assert "agent" in curate_help
    assert "heuristic" in curate_help
    assert "manual" in curate_help


def test_unknown_flag_exits_usage() -> None:
    assert main(["--not-a-flag"]) == ExitCode.USAGE


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["ingest", "missing.yaml"], "missing.yaml"),
        (["curate"], "endpoints.json"),
        (["emit"], "tools.yaml"),
        (["verify"], "generated"),
        (["record"], "--sandbox"),
    ],
)
def test_missing_input(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    argv: list[str],
    text: str,
) -> None:
    code = main(["--out", str(tmp_path), *argv])
    assert code == ExitCode.USAGE
    assert text in capsys.readouterr().err


def test_json_usage_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["--json", "--out", str(tmp_path), "curate"])
    assert code == ExitCode.USAGE
    captured = capsys.readouterr()
    assert captured.err.strip() == "curate"
    payload = json.loads(captured.out)
    if not isinstance(payload, dict):
        raise TypeError("error payload must be an object")
    assert payload["ok"] is False
    assert payload["exit_code"] == ExitCode.USAGE
    error = payload["error"]
    if not isinstance(error, str):
        raise TypeError("error must be a string")
    assert "endpoints.json" in error
