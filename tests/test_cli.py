from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from doc2mcp import __version__
from doc2mcp.cli import ExitCode, main

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == ExitCode.VERIFIED
    assert capsys.readouterr().out.strip() == f"doc2mcp {__version__}"


def test_help_lists_stages(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == ExitCode.VERIFIED
    output = _ANSI.sub("", capsys.readouterr().out)
    for name in ("ingest", "curate", "emit", "verify"):
        assert name in output
    assert "OpenAPI file path or URL" in output
    assert "docs URL" not in output
    assert "--offline" in output
    assert "--allow-ref-host" in output


def test_unknown_flag_exits_usage() -> None:
    assert main(["--not-a-flag"]) == ExitCode.USAGE


def test_ingest_missing_spec(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    code = main(["--out", str(tmp_path), "ingest", str(missing)])
    assert code == ExitCode.USAGE
    assert str(missing) in capsys.readouterr().err


def test_curate_missing_endpoints(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["--out", str(tmp_path), "curate"])
    assert code == ExitCode.USAGE
    assert "endpoints.json" in capsys.readouterr().err


def test_emit_missing_tools(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["--out", str(tmp_path), "emit"])
    assert code == ExitCode.USAGE
    assert "tools.yaml" in capsys.readouterr().err


def test_verify_missing_generated(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["--out", str(tmp_path), "verify"])
    assert code == ExitCode.USAGE
    assert "generated" in capsys.readouterr().err


def test_json_usage_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["--json", "--out", str(tmp_path), "curate"])
    assert code == ExitCode.USAGE
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    if not isinstance(payload, dict):
        raise TypeError("error payload must be an object")
    assert payload["ok"] is False
    assert payload["exit_code"] == ExitCode.USAGE
    error = payload["error"]
    if not isinstance(error, str):
        raise TypeError("error must be a string")
    assert "endpoints.json" in error
