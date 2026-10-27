from __future__ import annotations

import ast
import asyncio
import compileall
import hashlib
import importlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from doc2mcp.cli import ExitCode, main
from doc2mcp.curate.agent import CurationOptions, curate
from doc2mcp.curate.manifest import Manifest, load_manifest
from doc2mcp.emit import EmitError, emit
from doc2mcp.ingest import ingest
from doc2mcp.model import JsonObject, dump_endpoints
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.client import negotiate_auto
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE = _ROOT / "examples" / "sample-api"
_CORPUS = _ROOT / "tests" / "corpus" / "openapi"
_PETSTORE_3 = "e4fb447f4ee5a561f4f618d5de109953559408190705aef80dae826788b19d93"
_PETSTORE_2 = "8f1fb7ee521b71a29585ce1b70b8b89f7b0c2a5af7a3cbd4f0c15602a8d482f6"
_KEY = "SENTINEL/key+9f3a"
_TOKEN = "SENTINEL-token-9f3a"


def test_sample_emit_matches_committed_tree(tmp_path: Path) -> None:
    out = _copy_sample(tmp_path)
    emit(out)
    _assert_same(_SAMPLE / "generated", out / "generated")
    emit(out)
    _assert_same(_SAMPLE / "generated", out / "generated")
    for path in (out / "generated").rglob("*"):
        if path.is_file():
            assert b"\r" not in path.read_bytes()


def test_corpus_emit_is_stable(tmp_path: Path) -> None:
    expected = {"petstore-3.0.yaml": _PETSTORE_3, "petstore-2.0.yaml": _PETSTORE_2}
    for name, digest in expected.items():
        out = tmp_path / name
        _curate_spec(out, _CORPUS / name)
        emit(out)
        assert _tree_digest(out / "generated") == digest
        emit(out)
        assert _tree_digest(out / "generated") == digest


@settings(max_examples=20, deadline=None)
@given(
    suffix=st.text(alphabet="abcdefghij", min_size=3, max_size=6),
    kind=st.sampled_from(["string", "integer", "object", "enum", "nullable"]),
    query=st.booleans(),
)
def test_supported_schemas_import(suffix: str, kind: str, query: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        _curate_document(out, _ping_document(f"Box {suffix}", _schema(kind), query=query))
        emit(out)
        generated = out / "generated"
        assert compileall.compile_dir(generated, quiet=1)
        package = _package(generated)
        sys.path.insert(0, str(generated))
        try:
            module = importlib.import_module(f"{package}.server")
            listed = asyncio.run(module.mcp.list_tools())
        finally:
            sys.path.remove(str(generated))
            for name in list(sys.modules):
                if name == package or name.startswith(f"{package}."):
                    del sys.modules[name]
        manifest = load_manifest((out / "tools.yaml").read_text(encoding="utf-8"))
        assert [tool.name for tool in listed] == [tool.name for tool in manifest.tools]


def test_generated_modules_do_not_import_doc2mcp_or_httpx() -> None:
    for path in (_SAMPLE / "generated").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            else:
                continue
            for name in names:
                assert name.split(".", 1)[0] not in {"doc2mcp", "httpx"}


def test_hand_edit_guard(tmp_path: Path) -> None:
    out = _copy_sample(tmp_path)
    emit(out)
    extra = out / "generated" / "shelf_mcp" / "old.py"
    extra.write_text("x = 1\n", encoding="utf-8")
    _record(out / "generated", "shelf_mcp/old.py", extra.read_bytes())
    emit(out)
    assert not extra.exists()
    server = out / "generated" / "shelf_mcp" / "server.py"
    server.write_bytes(server.read_bytes() + b"\n")
    with pytest.raises(EmitError) as caught:
        emit(out)
    assert "generated/shelf_mcp/server.py" in caught.value.message
    assert "--force" in caught.value.message
    code = main(["emit", "--out", str(out)])
    assert code == ExitCode.USAGE
    code = main(["emit", "--force", "--out", str(out)])
    assert code == ExitCode.VERIFIED
    fresh = _copy_sample(tmp_path / "fresh")
    emit(fresh)
    _assert_same(fresh / "generated", out / "generated")


@pytest.mark.enable_socket
def test_stdio_lists_and_calls_search_books() -> None:
    app, key = _sample_app()
    with _serve(app) as base:
        listed, result, err = asyncio.run(
            _stdio_round_trip(
                _SAMPLE / "generated",
                "shelf_mcp",
                {"SHELF_API_KEY": key, "SHELF_BASE_URL": base},
                "search_books",
                capture=True,
            )
        )
    manifest = load_manifest((_SAMPLE / "tools.yaml").read_text(encoding="utf-8"))
    _assert_contract(manifest, listed)
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    body = result.structured_content
    assert isinstance(body, dict)
    rows = body.get("data")
    assert isinstance(rows, list)
    assert rows
    assert "GET /books" in err


@pytest.mark.enable_socket
def test_bearer_and_secret_redaction(tmp_path: Path) -> None:
    seen: list[str] = []

    async def ping(request: Request) -> Response:
        seen.append(request.headers.get("authorization", ""))
        return JSONResponse({"ok": True})

    out = tmp_path / "bearer"
    _curate_document(out, _secured_document("Bearer Box", "bearer"))
    emit(out)
    with _serve(Starlette(routes=[Route("/ping", ping)])) as base:
        _result, call, _err = asyncio.run(
            _stdio_round_trip(
                out / "generated",
                _package(out / "generated"),
                {"BEARER_BOX_TOKEN": _TOKEN, "BEARER_BOX_BASE_URL": base},
                "list_ping",
            )
        )
    assert seen == [f"Bearer {_TOKEN}"]
    assert isinstance(call, CallToolResult)
    assert call.is_error is False
    assert _TOKEN not in _tree_text(out)

    echoed: list[str] = []

    async def reject(request: Request) -> Response:
        key = request.headers.get("x-api-key", "")
        echoed.append(key)
        return JSONResponse({"error": f"rejected {key}"}, status_code=401)

    secret = tmp_path / "key"
    _curate_document(secret, _secured_document("Key Box", "apiKey"))
    emit(secret)
    with _serve(Starlette(routes=[Route("/ping", reject)])) as base:
        _listed, failed, _stderr = asyncio.run(
            _stdio_round_trip(
                secret / "generated",
                _package(secret / "generated"),
                {"KEY_BOX_API_KEY": _KEY, "KEY_BOX_BASE_URL": base},
                "list_ping",
            )
        )
    assert echoed == [_KEY]
    assert isinstance(failed, CallToolResult)
    assert failed.is_error is True
    rendered = " ".join(block.text for block in failed.content if block.type == "text")
    assert _KEY not in rendered
    assert _KEY not in _tree_text(secret)


@pytest.mark.enable_socket
def test_stdout_breaks_the_stdio_session(tmp_path: Path) -> None:
    out = tmp_path / "print"
    schema: JsonObject = {"type": "object", "additionalProperties": False}
    _curate_document(out, _ping_document("Print Box", schema))
    emit(out)
    package = _package(out / "generated")
    server = out / "generated" / package / "server.py"
    text = server.read_text(encoding="utf-8")
    marker = "async def "
    start = text.index(marker)
    doc = text.index('"""', start)
    close = text.index('"""', doc + 3) + 3
    injected = text[:close] + "\n    print('stdout-noise')\n" + text[close:]
    server.write_text(injected, encoding="utf-8")
    failed = False
    try:
        asyncio.run(_stdio_round_trip(out / "generated", package, {}, "list_ping"))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        failed = True
    assert failed


@pytest.mark.enable_socket
def test_http_transport_lists_tools(tmp_path: Path) -> None:
    port = _free_port()
    err_path = tmp_path / "stderr.txt"
    failed: Exception | None = None
    names: list[str] = []
    with err_path.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "shelf_mcp", "--transport", "http", "--port", str(port)],
            cwd=_SAMPLE / "generated",
            env={**_child_env(_SAMPLE / "generated"), "SHELF_API_KEY": "unused"},
            stdout=subprocess.DEVNULL,
            stderr=err,
        )
        try:
            names = asyncio.run(_http_names(f"http://127.0.0.1:{port}/mcp"))
        except Exception as exc:
            failed = exc
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
    if failed is not None:
        detail = err_path.read_text(encoding="utf-8")
        raise AssertionError(detail) from failed
    assert "search_books" in names


def test_packaged_server_help_runs_offline() -> None:
    project = _SAMPLE / "generated"
    completed = subprocess.run(
        [sys.executable, "-m", "shelf_mcp", "--help"],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage: shelf-mcp" in completed.stdout


def _copy_sample(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "tools.yaml").write_bytes((_SAMPLE / "tools.yaml").read_bytes())
    (out / "endpoints.json").write_bytes((_SAMPLE / "endpoints.json").read_bytes())
    return out


def _assert_same(expected: Path, actual: Path) -> None:
    assert _files(actual) == _files(expected)


def _files(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return dict(sorted(found.items()))


def _tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for rel, payload in _files(root).items():
        hasher.update(rel.encode())
        hasher.update(b"\0")
        hasher.update(payload)
        hasher.update(b"\0")
    return hasher.hexdigest()


def _tree_text(root: Path) -> str:
    return "\n".join(payload.decode("utf-8", "replace") for payload in _files(root).values())


def _record(generated: Path, rel: str, payload: bytes) -> None:
    ledger = generated / ".doc2mcp-files.json"
    loaded = json.loads(ledger.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("ledger is not an object")
    files = loaded.get("files")
    if not isinstance(files, dict):
        raise TypeError("ledger files are missing")
    files[rel] = hashlib.sha256(payload).hexdigest()
    ledger.write_text(json.dumps(loaded, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _curate_spec(out: Path, spec: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    loaded = ingest(str(spec), offline=True, allow_ref_hosts=())
    (out / "endpoints.json").write_text(dump_endpoints(loaded), encoding="utf-8")
    curate(out, CurationOptions(mode="heuristic", offline=True))


def _curate_document(out: Path, document: JsonObject) -> None:
    out.mkdir(parents=True, exist_ok=True)
    spec = out / "api.json"
    spec.write_text(json.dumps(document), encoding="utf-8")
    _curate_spec(out, spec)


def _schema(kind: str) -> JsonObject:
    if kind == "string":
        return {"type": "string", "minLength": 1, "description": "text"}
    if kind == "integer":
        return {"type": "integer", "minimum": 0, "maximum": 5}
    if kind == "enum":
        return {"type": "string", "enum": ["on", "off"]}
    if kind == "nullable":
        return {"type": "string", "nullable": True}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"name": {"type": "string", "description": "name"}},
        "required": ["name"],
    }


def _ping_document(title: str, schema: JsonObject, *, query: bool = False) -> JsonObject:
    operation: JsonObject = {
        "operationId": "ping",
        "responses": {
            "200": {
                "description": "ok",
                "content": {"application/json": {"schema": schema}},
            }
        },
    }
    if query:
        operation["parameters"] = [
            {
                "name": "q",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "description": "query"},
            }
        ]
    return {
        "openapi": "3.0.3",
        "info": {"title": title, "version": "1"},
        "paths": {"/ping": {"get": operation}},
    }


def _secured_document(title: str, kind: str) -> JsonObject:
    if kind == "bearer":
        scheme: JsonObject = {"type": "http", "scheme": "bearer"}
        name = "bearerAuth"
    else:
        scheme = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
        name = "apiKeyAuth"
    document = _ping_document(
        title,
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    )
    paths = document["paths"]
    if not isinstance(paths, dict):
        raise TypeError("paths is not an object")
    ping = paths["/ping"]
    if not isinstance(ping, dict):
        raise TypeError("path is not an object")
    operation = ping["get"]
    if not isinstance(operation, dict):
        raise TypeError("operation is not an object")
    operation["security"] = [{name: []}]
    document["components"] = {"securitySchemes": {name: scheme}}
    return document


def _package(generated: Path) -> str:
    names = [path.name for path in generated.iterdir() if path.is_dir()]
    if len(names) != 1:
        raise AssertionError(f"expected one package, found {names}")
    return names[0]


def _sample_app() -> tuple[Starlette, str]:
    path = _SAMPLE / "server.py"
    spec = importlib.util.spec_from_file_location("sample_shelf_server", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("sample server could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    key = getattr(module, "API_KEY", None)
    create = getattr(module, "create_app", None)
    if not isinstance(key, str) or not callable(create):
        raise TypeError("sample server is missing create_app")
    app = create()
    if not isinstance(app, Starlette):
        raise TypeError("create_app must return an application")
    return app, key


@contextmanager
def _serve(app: Starlette) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        bound = sock.getsockname()[1]
        if not isinstance(bound, int):
            raise TypeError("port is not an integer")
        return bound


def _child_env(generated: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    current = env.get("PYTHONPATH")
    if current:
        env["PYTHONPATH"] = os.pathsep.join((str(generated), current))
    else:
        env["PYTHONPATH"] = str(generated)
    if extra is not None:
        env.update(extra)
    return env


async def _stdio_round_trip(
    generated: Path,
    package: str,
    extra: Mapping[str, str],
    tool: str,
    *,
    capture: bool = False,
) -> tuple[list[object], object, str]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", package],
            cwd=generated,
            env=_child_env(generated, extra),
        )
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await negotiate_auto(session)
            listed = await session.list_tools()
            result = await session.call_tool(tool, {})
        errlog.seek(0)
        logged = errlog.read() if capture else ""
    return list(listed.tools), result, logged


async def _http_names(url: str) -> list[str]:
    last: Exception | None = None
    for _ in range(50):
        try:
            async with (
                streamable_http_client(url) as (read, write),
                ClientSession(read, write) as session,
            ):
                await negotiate_auto(session)
                listed = await session.list_tools()
                return [tool.name for tool in listed.tools]
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.1)
    raise RuntimeError("http server did not answer") from last


def _assert_contract(manifest: Manifest, listed: list[object]) -> None:
    names = [_text(tool, "name") for tool in listed]
    assert names == [tool.name for tool in manifest.tools]
    by_name = {_text(tool, "name"): tool for tool in listed}
    for tool in manifest.tools:
        found = by_name[tool.name]
        assert _text(found, "title") == tool.title
        assert _text(found, "description") == tool.description
        annotations = getattr(found, "annotations", None)
        assert getattr(annotations, "read_only_hint", None) is tool.annotations.readOnlyHint
        assert getattr(annotations, "destructive_hint", None) is tool.annotations.destructiveHint
        assert getattr(annotations, "idempotent_hint", None) is tool.annotations.idempotentHint
        assert getattr(annotations, "open_world_hint", None) is tool.annotations.openWorldHint
        incoming = _object(getattr(found, "input_schema", None))
        assert _names(incoming) == {item.name for item in tool.inputs}
        assert _required(incoming) == {item.name for item in tool.inputs if item.required}
        for item in tool.inputs:
            assert _property(incoming, item.name).get("description") == item.description
        if isinstance(tool.output.schema_, dict) and "properties" in tool.output.schema_:
            outgoing = _object(getattr(found, "output_schema", None))
            expected = _object(tool.output.schema_)
            assert _names(outgoing) == _names(expected)
            assert _required(outgoing) == _required(expected)


def _text(tool: object, name: str) -> str:
    value = getattr(tool, name, None)
    if not isinstance(value, str):
        raise TypeError(f"{name} is missing")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("schema is not an object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _names(schema: dict[str, object]) -> set[str]:
    raw = schema.get("properties", {})
    if not isinstance(raw, dict):
        return set()
    return {key for key in raw if isinstance(key, str)}


def _required(schema: dict[str, object]) -> set[str]:
    raw = schema.get("required", [])
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str)}


def _property(schema: dict[str, object], name: str) -> dict[str, object]:
    raw = schema.get("properties", {})
    if not isinstance(raw, dict):
        raise TypeError("properties is not an object")
    found = raw.get(name)
    if not isinstance(found, dict):
        raise TypeError("property is not an object")
    return {key: item for key, item in found.items() if isinstance(key, str)}
