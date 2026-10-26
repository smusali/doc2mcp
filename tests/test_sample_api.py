from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from doc2mcp.ingest import _load_yaml, ingest
from doc2mcp.model import JsonObject, JsonValue
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from openapi_spec_validator import OpenAPIV31SpecValidator, validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "sample-api" / "openapi.yaml"
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_MISMATCH = "getBookStats"
_MISMATCH_FIELD = "page_count"
_UPLOADS = {
    "createUpload": "post",
    "uploadPart": "put",
    "completeUpload": "post",
}
_ADMIN = {
    "listAuditEvents",
    "suspendPatron",
    "reinstatePatron",
    "getCirculation",
}


@dataclass(frozen=True)
class _Operation:
    method: str
    path: str
    operation_id: str
    body: JsonObject


@dataclass(frozen=True)
class _Call:
    operation_id: str
    method: str
    path: str
    status: int
    body: dict[str, object] | None = None


@dataclass(frozen=True)
class _Result:
    operation_id: str
    status: int
    content: bytes
    payload: dict[str, object]


_CALLS = (
    _Call("listBooks", "GET", "/books", 200),
    _Call(
        "createBook",
        "POST",
        "/books",
        201,
        {"title": "West Index", "author": "Ned Brooks"},
    ),
    _Call("getBook", "GET", "/books/book_001", 200),
    _Call(
        "updateBook",
        "PATCH",
        "/books/book_001",
        200,
        {"title": "North Wind Revised"},
    ),
    _Call("getBookStats", "GET", "/books/book_001/stats", 200),
    _Call("getBookPageCount", "GET", "/books/book_001/page-count", 200),
    _Call("listCopies", "GET", "/books/book_001/copies", 200),
    _Call("createCopy", "POST", "/books/book_001/copies", 201, {"barcode": "C-004"}),
    _Call("getCopy", "GET", "/copies/copy_002", 200),
    _Call("updateCopy", "PATCH", "/copies/copy_002", 200, {"barcode": "C-002B"}),
    _Call("deleteCopy", "DELETE", "/copies/copy_003", 200),
    _Call("listReviews", "GET", "/books/book_001/reviews", 200),
    _Call(
        "createReview",
        "POST",
        "/books/book_001/reviews",
        201,
        {"patron_id": "patron_001", "rating": 4, "body": "Worth a second read."},
    ),
    _Call("getReview", "GET", "/reviews/review_001", 200),
    _Call("updateReview", "PATCH", "/reviews/review_001", 200, {"rating": 4}),
    _Call("deleteReview", "DELETE", "/reviews/review_002", 200),
    _Call("listPatrons", "GET", "/patrons", 200),
    _Call(
        "createPatron",
        "POST",
        "/patrons",
        201,
        {"name": "Jules Hart", "email": "jules@example.test"},
    ),
    _Call("getPatron", "GET", "/patrons/patron_001", 200),
    _Call("updatePatron", "PATCH", "/patrons/patron_001", 200, {"name": "Riley Chen"}),
    _Call("deletePatron", "DELETE", "/patrons/patron_003", 200),
    _Call("listHolds", "GET", "/patrons/patron_002/holds", 200),
    _Call(
        "createHold",
        "POST",
        "/patrons/patron_001/holds",
        201,
        {"book_id": "book_002"},
    ),
    _Call("deleteHold", "DELETE", "/holds/hold_002", 200),
    _Call("listLoans", "GET", "/loans", 200),
    _Call(
        "createLoan",
        "POST",
        "/loans",
        201,
        {"copy_id": "copy_002", "patron_id": "patron_002"},
    ),
    _Call("getLoan", "GET", "/loans/loan_001", 200),
    _Call("renewLoan", "POST", "/loans/loan_001/renew", 200),
    _Call("returnLoan", "POST", "/loans/loan_001/return", 200),
    _Call("listBookLoans", "GET", "/books/book_001/loans", 200),
    _Call("listPatronLoans", "GET", "/patrons/patron_001/loans", 200),
    _Call(
        "createUpload",
        "POST",
        "/uploads",
        201,
        {"filename": "cover.pdf", "content_type": "application/pdf"},
    ),
    _Call(
        "uploadPart",
        "PUT",
        "/uploads/upload_001/parts/1",
        200,
        {"data": "aGVsbG8="},
    ),
    _Call("completeUpload", "POST", "/uploads/upload_001/complete", 200),
    _Call(
        "attachBookFile",
        "PUT",
        "/books/book_001/file",
        200,
        {"upload_id": "upload_001"},
    ),
    _Call("listAuditEvents", "GET", "/admin/audit-events", 200),
    _Call("suspendPatron", "POST", "/admin/patrons/patron_002/suspend", 200),
    _Call("reinstatePatron", "POST", "/admin/patrons/patron_002/reinstate", 200),
    _Call("getCirculation", "GET", "/admin/circulation", 200),
    _Call("deleteBook", "DELETE", "/books/book_004", 200),
)


def test_spec_is_openapi_31() -> None:
    document = _document()
    assert document.get("openapi") == "3.1.0"
    validate(document, cls=OpenAPIV31SpecValidator)
    operations = _operations(document)
    assert 35 <= len(operations) <= 45
    assert ingest(str(SPEC), offline=True, allow_ref_hosts=()).findings == []


def test_deliberate_traits() -> None:
    document = _document()
    operations = _operations(document)
    server = _server()
    lists = [item for item in operations if item.operation_id.startswith("list")]
    assert lists
    for item in lists:
        assert "cursor" in _parameter_names(document, item.path, item.body)
        schema = _response_schema(item.body, _success_status(item.body))
        assert _has_property(schema, "has_more", document), item.operation_id

    schemes = _object(_object(document["components"])["securitySchemes"])
    scheme = _object(schemes["apiKeyAuth"])
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-API-Key"
    assert scheme["name"] == server.API_KEY_HEADER
    assert document["security"] == [{"apiKeyAuth": []}]
    for item in operations:
        security = item.body.get("security")
        assert security is None or security == [{"apiKeyAuth": []}]

    uploads = {
        item.operation_id: item.method for item in operations if item.path.startswith("/uploads")
    }
    assert uploads == _UPLOADS

    admin = {
        item.operation_id
        for item in operations
        if isinstance(item.body.get("tags"), list) and "admin" in item.body["tags"]
    }
    assert admin == _ADMIN

    deprecated = [item.operation_id for item in operations if item.body.get("deprecated") is True]
    assert deprecated == ["getBookPageCount"]

    stats = next(item for item in operations if item.operation_id == _MISMATCH)
    stats_schema = _deref(_component(document, "BookStats"), document)
    field = _object(_object(stats_schema.get("properties"))[_MISMATCH_FIELD])
    assert field["type"] == "integer"
    assert field.get("examples") == [320]
    media = _media(stats.body, _success_status(stats.body))
    example = _object(media["example"])
    assert example[_MISMATCH_FIELD] == 320
    assert isinstance(example[_MISMATCH_FIELD], int)


def test_every_operation_matches_its_schema() -> None:
    document = _document()
    operations = {item.operation_id: item for item in _operations(document)}
    assert {call.operation_id for call in _CALLS} == set(operations)
    registry = _registry(document)
    server = _server()
    results = _run(server)
    by_id = {result.operation_id: result for result in results}
    books = by_id["listBooks"]
    assert books.payload["has_more"] is True
    assert books.payload["next_cursor"] == "book_002"
    page = books.payload["data"]
    assert isinstance(page, list) and len(page) == 2

    for call in _CALLS:
        result = by_id[call.operation_id]
        assert result.status == call.status, (call.operation_id, result.content)
        operation = operations[call.operation_id]
        schema = _with_base(_response_schema(operation.body, str(call.status)))
        errors = list(Draft202012Validator(schema, registry=registry).iter_errors(result.payload))
        if call.operation_id == _MISMATCH:
            assert result.payload[_MISMATCH_FIELD] == "320"
            assert len(errors) == 1
            assert list(errors[0].path) == [_MISMATCH_FIELD]
            assert errors[0].validator == "type"
            continue
        assert not errors, (call.operation_id, _error_text(errors))

    header, _key = _credentials(server)
    with TestClient(server.create_app()) as client:
        missing = client.get("/books")
        rejected = client.get("/books", headers={header: "nope"})
    assert missing.status_code == 401
    assert missing.content == b'{"error":"unauthorized"}'
    assert rejected.status_code == 401
    assert rejected.content == missing.content


def test_responses_are_deterministic() -> None:
    server = _server()
    assert _bodies(server) == _bodies(server)


def test_server_adds_no_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = {_dependency_name(item) for item in _string_list(project["project"]["dependencies"])}
    dev = {
        _dependency_name(item)
        for item in _string_list(project["project"]["optional-dependencies"]["dev"])
    }
    assert "starlette" not in runtime
    assert "uvicorn" not in runtime
    assert "starlette" in dev
    assert "uvicorn" in dev
    assert "doc2mcp" not in (ROOT / "examples" / "sample-api" / "server.py").read_text(
        encoding="utf-8"
    )


def test_sample_files_are_not_ignored() -> None:
    root = ROOT / "examples" / "sample-api"
    files = [
        path.relative_to(ROOT).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    assert files
    result = subprocess.run(
        ["git", "check-ignore", "--", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == ""


def test_fixture_waits_for_verification_report() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    start = workflow.index("name: Detect sample API")
    fixture = workflow[start : workflow.index("self-run:", start)]
    assert "examples/sample-api/report/verification.json" in fixture
    assert "openapi.yaml" not in fixture


def _document() -> JsonObject:
    loaded = _load_yaml(SPEC.read_bytes())
    if not isinstance(loaded, dict):
        raise TypeError("spec must be an object")
    return loaded


def _server() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sample_api_server", ROOT / "examples" / "sample-api" / "server.py"
    )
    if spec is None or spec.loader is None or spec.name is None:
        raise RuntimeError("sample server could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _operations(document: JsonObject) -> list[_Operation]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise TypeError("paths must be an object")
    found: list[_Operation] = []
    for path, item in paths.items():
        if not isinstance(path, str) or not isinstance(item, dict):
            raise TypeError("path item must be an object")
        for method in _METHODS:
            raw = item.get(method)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise TypeError("operation must be an object")
            operation_id = raw.get("operationId")
            if not isinstance(operation_id, str):
                raise TypeError("operationId must be a string")
            found.append(_Operation(method, path, operation_id, raw))
    return found


def _parameter_names(document: JsonObject, path: str, operation: JsonObject) -> list[str]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise TypeError("paths must be an object")
    path_item = paths.get(path)
    if not isinstance(path_item, dict):
        raise TypeError("path item must be an object")
    names: list[str] = []
    for source in (path_item, operation):
        raw = source.get("parameters")
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise TypeError("parameters must be a list")
        for item in raw:
            name = _deref(item, document).get("name")
            if not isinstance(name, str):
                raise TypeError("parameter name must be a string")
            names.append(name)
    return names


def _success_status(operation: JsonObject) -> str:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise TypeError("responses must be an object")
    codes = [code for code in responses if isinstance(code, str) and code.startswith("2")]
    if len(codes) != 1:
        raise AssertionError("expected one success response")
    return codes[0]


def _response_schema(operation: JsonObject, status: str) -> JsonValue:
    return _media(operation, status)["schema"]


def _media(operation: JsonObject, status: str) -> JsonObject:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise TypeError("responses must be an object")
    response = responses.get(status)
    if not isinstance(response, dict):
        raise TypeError("response must be an object")
    content = response.get("content")
    if not isinstance(content, dict):
        raise TypeError("content must be an object")
    media = content.get("application/json")
    if not isinstance(media, dict):
        raise TypeError("media type must be an object")
    return media


def _component(document: JsonObject, name: str) -> JsonValue:
    components = document.get("components")
    if not isinstance(components, dict):
        raise TypeError("components must be an object")
    schemas = components.get("schemas")
    if not isinstance(schemas, dict) or name not in schemas:
        raise KeyError(name)
    return schemas[name]


def _has_property(schema: JsonValue, name: str, document: JsonObject) -> bool:
    if not isinstance(schema, dict):
        return False
    resolved = _deref(schema, document)
    properties = resolved.get("properties")
    if isinstance(properties, dict) and name in properties:
        return True
    combinators = resolved.get("allOf")
    if not isinstance(combinators, list):
        return False
    return any(_has_property(item, name, document) for item in combinators)


def _deref(node: JsonValue, document: JsonObject) -> JsonObject:
    if not isinstance(node, dict):
        raise TypeError("node must be an object")
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    target = _at(document, ref)
    if not isinstance(target, dict):
        raise TypeError("reference must be an object")
    if len(node) == 1:
        return _deref(target, document)
    merged: JsonObject = dict(target)
    for key, value in node.items():
        if key != "$ref":
            merged[key] = value
    return merged


def _at(document: JsonObject, ref: str) -> JsonValue:
    if not ref.startswith("#/"):
        raise ValueError(ref)
    current: JsonValue = document
    for part in ref[2:].split("/"):
        token = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise KeyError(ref)
        current = current[token]
    return current


def _registry(document: JsonObject) -> Registry:
    return Registry().with_resource(
        "urn:sample",
        Resource(contents=document, specification=DRAFT202012),
    )


def _with_base(node: JsonValue) -> JsonValue:
    if isinstance(node, list):
        return [_with_base(item) for item in node]
    if not isinstance(node, dict):
        return node
    built: JsonObject = {}
    for key, value in node.items():
        if key == "$ref" and isinstance(value, str) and value.startswith("#"):
            built[key] = f"urn:sample{value}"
            continue
        built[key] = _with_base(value)
    return built


def _credentials(server: ModuleType) -> tuple[str, str]:
    header = server.API_KEY_HEADER
    key = server.API_KEY
    if not isinstance(header, str) or not isinstance(key, str):
        raise TypeError("sample credentials must be strings")
    return header, key


def _run(server: ModuleType) -> list[_Result]:
    header, key = _credentials(server)
    create_app = server.create_app
    if not callable(create_app):
        raise TypeError("create_app must be callable")
    results: list[_Result] = []
    with TestClient(create_app()) as client:
        for call in _CALLS:
            response = client.request(
                call.method,
                call.path,
                headers={header: key},
                json=call.body,
            )
            results.append(
                _Result(
                    call.operation_id,
                    response.status_code,
                    response.content,
                    _object(response.json()),
                )
            )
    return results


def _bodies(server: ModuleType) -> list[tuple[str, int, bytes]]:
    return [(result.operation_id, result.status, result.content) for result in _run(server)]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("value must be an object")
    body: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("object keys must be strings")
        body[key] = item
    return body


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("expected a list of strings")
    return value


def _dependency_name(spec: str) -> str:
    name = spec.split(";", 1)[0].strip()
    for marker in ("[", ">", "<", "=", "~", "!"):
        name = name.split(marker, 1)[0]
    return name.strip().lower()


def _error_text(errors: list[ValidationError]) -> str:
    return "; ".join(f"{error.json_path}: {error.message}" for error in errors)
