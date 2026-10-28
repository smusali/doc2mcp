from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client._probe import negotiate_auto
from mcp4api.cli import ExitCode, main
from mcp4api.curate.agent import CurationOptions, curate
from mcp4api.curate.manifest import Manifest, dump_manifest, load_manifest
from mcp4api.emit import emit
from mcp4api.ingest import ingest
from mcp4api.model import JsonObject, dump_endpoints
from mcp4api.report import published_report_schema_text
from mcp4api.suite import Proxy
from mcp4api.verify import VerifyOptions, isolate_prefix, record, verify

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA = _ROOT / "docs" / "schemas" / "report-v1.json"
_SAMPLE = _ROOT / "examples" / "sample-api"


def test_report_schema_matches_the_model() -> None:
    assert published_report_schema_text() == _SCHEMA.read_text(encoding="utf-8")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    sample = json.loads((_SAMPLE / "report" / "verification.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(sample)


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_fault_cassettes_name_the_failing_check(tmp_path: Path) -> None:
    out = tmp_path / "widgets"
    _curate(out, _widget_document())
    manifest = _manifest(out)
    tool = manifest.tools[0]
    widget_id = tool.verify.example_args["widget_id"]
    if not isinstance(widget_id, str):
        raise TypeError("widget id must be a string")
    path = f"/widgets/{widget_id}"
    _write_cassette(
        out,
        tool.name,
        [
            _exchange("GET", path, {"id": widget_id, "name": 1}),
            _exchange("GET", path, {"id": widget_id, "name": 1}),
        ],
    )
    outcome = verify(out, VerifyOptions(deterministic=True, quiet=True))
    assert outcome.gaps is True
    report = _report(out)
    failure = _failure(report, tool.name)
    assert failure["assertion"] == "schema"
    assert failure["field"] == "name"

    _write_cassette(
        out,
        tool.name,
        [
            _exchange("GET", path, {"id": widget_id}),
            _exchange("GET", path, {"id": widget_id}),
        ],
    )
    verify(out, VerifyOptions(deterministic=True, quiet=True))
    assert _failure(_report(out), tool.name)["assertion"] == "required"

    _write_cassette(out, tool.name, [_exchange("GET", path, _widget(widget_id), status=418)] * 2)
    verify(out, VerifyOptions(deterministic=True, quiet=True))
    assert _failure(_report(out), tool.name)["assertion"] == "status"

    _write_cassette(out, tool.name, [_exchange("GET", path, _widget(widget_id), status=500)] * 2)
    verify(out, VerifyOptions(deterministic=True, quiet=True))
    assert _failure(_report(out), tool.name)["assertion"] == "status"

    _write_cassette(
        out,
        tool.name,
        [_exchange("GET", path, _widget(widget_id), hang=True)] * 2,
    )
    verify(out, VerifyOptions(deterministic=True, quiet=True, timeout_seconds=1))
    hung = _failure(_report(out), tool.name)
    assert hung["assertion"] == "transport"
    _write_cassette(out, tool.name, [_exchange("GET", path, _widget(widget_id), status=500)] * 2)
    code = main(["--json", "--deterministic", "--out", str(out), "verify"])
    assert code == ExitCode.GAP
    server = next((out / "generated").rglob("server.py"))
    assert f"def {tool.name}" not in server.read_text(encoding="utf-8")


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_depends_on_harvests_the_producer_id(tmp_path: Path) -> None:
    out = tmp_path / "items"
    _curate(out, _item_document())
    manifest = _manifest(out)
    producer = next(
        tool for tool in manifest.tools if any("POST" in step.endpoint_id for step in tool.steps)
    )
    consumer = next(tool for tool in manifest.tools if tool is not producer)
    updated = []
    for tool in manifest.tools:
        if tool.name != consumer.name:
            updated.append(tool)
            continue
        examples = dict(tool.verify.example_args)
        examples["item_id"] = "example"
        updated.append(
            tool.model_copy(
                update={
                    "verify": tool.verify.model_copy(
                        update={"depends_on": [producer.name], "example_args": examples}
                    )
                }
            )
        )
    _write_manifest(out, manifest.model_copy(update={"tools": updated}))
    with _serve(_ItemHandler) as base:
        record(out, VerifyOptions(sandbox=base, quiet=True))
    cassette = json.loads((out / "tests" / "cassettes" / f"{consumer.name}.json").read_text())
    paths = [row["path"] for row in cassette["exchanges"] if isinstance(row, dict)]
    assert "/items/item_9" in paths
    outcome = verify(out, VerifyOptions(deterministic=True, quiet=True))
    assert outcome.gaps is False
    arguments = _call_arguments(out, consumer.name)
    assert arguments["item_id"] == "item_9"


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_proxy_blocks_hosts_outside_the_allowlist() -> None:
    proxy = Proxy({"127.0.0.1"})
    url = proxy.start()
    try:
        port = int(url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            payload = sock.recv(1024)
        assert b"403" in payload
        assert proxy.dialed == []
        assert "example.com" in proxy.blocked
    finally:
        proxy.close()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_generated_client_refuses_a_host_before_dialing(tmp_path: Path) -> None:
    out = tmp_path / "tiny"
    _curate(out, _ping_document("https://example.test"))
    emit(out)
    proxy = Proxy({"127.0.0.1"})
    url = proxy.start()
    try:
        message = _call_ping(out / "generated", url)
    finally:
        proxy.close()
    assert "not allowed" in message
    assert proxy.dialed == []


def test_live_without_allow_host_exits_before_a_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "live"
    _curate(out, _ping_document("https://example.test"))
    emit(out)
    code = main(["--live", "--out", str(out), "verify"])
    assert code == ExitCode.USAGE
    assert "allow-host" in capsys.readouterr().err
    assert not (out / "report").exists()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_live_without_allow_writes_skips_destructive_tools(tmp_path: Path) -> None:
    out = tmp_path / "delete"
    _curate(out, _delete_document())
    outcome = verify(
        out,
        VerifyOptions(live=True, allow_hosts=("example.com",), deterministic=True, quiet=True),
    )
    assert outcome.gaps is False
    report = _report(out)
    row = report["tools"]
    if not isinstance(row, list) or not row or not isinstance(row[0], dict):
        raise TypeError("tools must be a list")
    assert row[0]["status"] == "skipped: write-guard"
    assert row[0]["calls"] == 0


def test_offline_refuses_record_sandbox_and_live(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "off"
    (out / "generated").mkdir(parents=True)
    (out / "endpoints.json").write_text("{}\n", encoding="utf-8")
    (out / "tools.yaml").write_text("tools: []\n", encoding="utf-8")
    record = main(["--offline", "--sandbox", "http://127.0.0.1:9", "--out", str(out), "record"])
    sandbox = main(["--offline", "--sandbox", "http://127.0.0.1:9", "--out", str(out), "verify"])
    live = main(["--offline", "--live", "--out", str(out), "verify"])
    assert record == sandbox == live == ExitCode.USAGE
    assert "offline" in capsys.readouterr().err


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_deterministic_reports_match(tmp_path: Path) -> None:
    out = tmp_path / "ping"
    _curate(out, _ping_document("https://example.test"))
    options = VerifyOptions(deterministic=True, quiet=True)
    verify(out, options)
    first = _report_text(out)
    verify(out, options)
    assert first == _report_text(out)
    assert "started_at" not in first


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_recorded_cassette_scrubs_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "supersecretvalue"
    monkeypatch.setenv("APP_TOKEN", secret)
    out = tmp_path / "secret"
    _curate(out, _note_document())
    with _serve(_NoteHandler) as base:
        record(out, VerifyOptions(sandbox=base, quiet=True))
    text = (
        (out / "tests" / "cassettes")
        .joinpath(f"{_manifest(out).tools[0].name}.json")
        .read_text(encoding="utf-8")
    )
    assert secret not in text
    assert "Bearer ***" in text


def test_isolate_is_linux_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform != "linux":
        out = tmp_path / "iso"
        _curate(out, _ping_document("https://example.test"))
        emit(out)
        code = main(["--isolate", "--out", str(out), "verify"])
        assert code == ExitCode.USAGE
        assert "Linux" in capsys.readouterr().err
        return
    assert isolate_prefix()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_sample_run_exits_on_the_page_count_gap(tmp_path: Path) -> None:
    dest = _copy_sample(tmp_path)
    spec = dest / "openapi.yaml"
    code = main([str(spec), "--offline", "--deterministic", "--quiet", "--out", str(dest)])
    assert code == ExitCode.GAP
    report = _report(dest)
    assert report["excluded"] == ["get_book"]
    failure = _failure(report, "get_book")
    assert failure["field"] == "page_count"
    assert failure["assertion"] == "schema"
    assert _tree(dest) == _tree(_SAMPLE)
    assert not any(path.name == "__pycache__" for path in dest.rglob("__pycache__"))


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_integer_page_count_clears_the_sample_gap(tmp_path: Path) -> None:
    dest = _copy_sample(tmp_path)
    cassette = dest / "tests" / "cassettes" / "get_book.json"
    original = cassette.read_text(encoding="utf-8")
    corrected = original.replace('"page_count": "320"', '"page_count": 320')
    assert corrected != original
    cassette.write_text(corrected, encoding="utf-8")
    code = main(["--offline", "--deterministic", "--quiet", "--out", str(dest), "verify"])
    assert code == ExitCode.VERIFIED
    assert _report(dest)["excluded"] == []


@pytest.mark.enable_socket
def test_one_tool_suite_passes_without_mcp4api(tmp_path: Path) -> None:
    out = tmp_path / "suite"
    _curate(out, _ping_document("https://example.test"))
    verify(out, VerifyOptions(deterministic=True, quiet=True))
    (out / "pytest.ini").write_text("[pytest]\naddopts =\n", encoding="utf-8")
    venv = tmp_path / "venv"
    python = _venv(venv)
    completed = subprocess.run(
        [str(python), "-c", "import mcp4api"],
        cwd=out,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    run = subprocess.run(
        [str(python), "-m", "pytest", "tests", "-q"],
        cwd=out,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    tree = ast.parse((out / "tests" / "test_tools.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".", 1)[0])
    assert "mcp4api" not in imported


def _copy_sample(tmp_path: Path) -> Path:
    dest = tmp_path / "sample"
    shutil.copytree(
        _SAMPLE,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".mcp4api"),
    )
    return dest


def _tree(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in {".mcp4api", "__pycache__"} for part in relative.parts):
            continue
        if path.suffix == ".pyc":
            continue
        found[relative.as_posix()] = path.read_bytes()
    return found


def _venv(path: Path) -> Path:
    subprocess.run(["uv", "venv", str(path)], check=True, capture_output=True, text=True)
    python = path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "pytest",
            "jsonschema",
            "mcp",
            "httpx2",
            "pydantic",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return python


def _curate(out: Path, document: JsonObject) -> None:
    out.mkdir(parents=True, exist_ok=True)
    spec = out / "api.json"
    spec.write_text(json.dumps(document), encoding="utf-8")
    loaded = ingest(str(spec), offline=True, allow_ref_hosts=())
    (out / "endpoints.json").write_text(dump_endpoints(loaded), encoding="utf-8")
    curate(out, CurationOptions(mode="heuristic", offline=True))


def _manifest(out: Path) -> Manifest:
    return load_manifest((out / "tools.yaml").read_text(encoding="utf-8"))


def _write_manifest(out: Path, manifest: Manifest) -> None:
    (out / "tools.yaml").write_text(dump_manifest(manifest), encoding="utf-8")


def _report(out: Path) -> JsonObject:
    loaded = json.loads(_report_text(out))
    if not isinstance(loaded, dict):
        raise TypeError("report must be an object")
    return loaded


def _report_text(out: Path) -> str:
    return (out / "report" / "verification.json").read_text(encoding="utf-8")


def _failure(report: JsonObject, tool: str) -> JsonObject:
    failures = report.get("failures")
    if not isinstance(failures, list):
        raise TypeError("failures must be a list")
    for item in failures:
        if isinstance(item, dict) and item.get("tool") == tool:
            return item
    raise AssertionError(tool)


def _call_arguments(out: Path, tool: str) -> JsonObject:
    path = out / "tests" / "suite.py"
    spec = importlib.util.spec_from_file_location("mcp4api_verify_suite", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("suite could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plan = json.loads((out / "tests" / "plan.json").read_text(encoding="utf-8"))
    found = module.execute(plan)
    if not isinstance(found, dict):
        raise TypeError("results must be an object")
    calls = found.get("calls")
    if not isinstance(calls, list):
        raise TypeError("calls must be a list")
    for call in calls:
        if isinstance(call, dict) and call.get("tool") == tool and call.get("kind") == "required":
            arguments = call.get("arguments")
            if isinstance(arguments, dict):
                return arguments
    raise AssertionError(tool)


def _write_cassette(out: Path, tool: str, exchanges: list[JsonObject]) -> None:
    directory = out / "tests" / "cassettes"
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"exchanges": exchanges}, indent=2, sort_keys=True) + "\n"
    (directory / f"{tool}.json").write_text(payload, encoding="utf-8")


def _exchange(
    method: str,
    path: str,
    response: JsonObject,
    *,
    status: int = 200,
    hang: bool = False,
) -> JsonObject:
    row: JsonObject = {
        "body": None,
        "method": method,
        "path": path,
        "query": {},
        "response": response,
        "status": status,
    }
    if hang:
        row["hang"] = True
    return row


def _widget(widget_id: str) -> JsonObject:
    return {"id": widget_id, "name": "Widget"}


def _widget_document() -> JsonObject:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Widgets", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:9"}],
        "paths": {
            "/widgets/{widget_id}": {
                "get": {
                    "operationId": "getWidget",
                    "parameters": [
                        {
                            "name": "widget_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "widget",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["id", "name"],
                                        "properties": {
                                            "id": {"type": "string"},
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def _item_document() -> JsonObject:
    item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id"],
        "properties": {"id": {"type": "string"}},
    }
    detailed = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "name"],
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
    }
    return {
        "openapi": "3.0.3",
        "info": {"title": "Items", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:9"}],
        "paths": {
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "responses": {
                        "200": {
                            "description": "created",
                            "content": {"application/json": {"schema": item}},
                        }
                    },
                }
            },
            "/items/{item_id}": {
                "get": {
                    "operationId": "getItem",
                    "parameters": [
                        {
                            "name": "item_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "item",
                            "content": {"application/json": {"schema": detailed}},
                        }
                    },
                }
            },
        },
    }


def _ping_document(url: str) -> JsonObject:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Tiny", "version": "1"},
        "servers": [{"url": url}],
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "responses": {"204": {"description": "pong"}},
                }
            }
        },
    }


def _delete_document() -> JsonObject:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Deletes", "version": "1"},
        "servers": [{"url": "https://example.com"}],
        "paths": {
            "/widgets/{widget_id}": {
                "delete": {
                    "operationId": "deleteWidget",
                    "parameters": [
                        {
                            "name": "widget_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"204": {"description": "gone"}},
                }
            }
        },
    }


def _note_document() -> JsonObject:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Notes", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:9"}],
        "paths": {
            "/note": {
                "get": {
                    "operationId": "getNote",
                    "responses": {
                        "200": {
                            "description": "note",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["note"],
                                        "properties": {"note": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


class _ItemHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        _send_json(self, {"id": "item_9"})

    def do_GET(self) -> None:
        _send_json(self, {"id": "item_9", "name": "North"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _NoteHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        _send_json(self, {"note": f"{os.environ['APP_TOKEN']} Bearer abcdefghijklmnop"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _send_json(handler: BaseHTTPRequestHandler, payload: JsonObject) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> _Server:
    return _Server(handler)


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        port = self._httpd.server_address[1]
        if not isinstance(port, int):
            raise RuntimeError("port is not an integer")
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *_args: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _call_ping(generated: Path, proxy: str) -> str:
    package = next(path.name for path in generated.iterdir() if (path / "client.py").is_file())
    env = dict(os.environ)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "MCP4API_ALLOWED_HOSTS": "127.0.0.1",
            "TINY_BASE_URL": "http://example.com",
        }
    )
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)

    async def run() -> str:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", package],
            cwd=generated,
            env=env,
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await negotiate_auto(session)
            listed = await session.list_tools()
            name = listed.tools[0].name
            result = await session.call_tool(name, {})
        parts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
        return " ".join(parts)

    return asyncio.run(run())
