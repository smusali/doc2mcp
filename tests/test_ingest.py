from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from doc2mcp.cli import ExitCode, main
from doc2mcp.ingest import _load_yaml, _normalize_schema, ingest
from doc2mcp.model import (
    ApiSpec,
    JsonObject,
    JsonSchema,
    JsonValue,
    Parameter,
    dump_endpoints,
    expect_json,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

CORPUS = Path(__file__).resolve().parent / "corpus" / "openapi"
SPECS = (
    "petstore-3.0.yaml",
    "petstore-3.1.json",
    "petstore-2.0.yaml",
    "recursive.yaml",
    "external-root.yaml",
    "findings.yaml",
    "stream.yaml",
    "github-api.json",
    "stripe-api.json",
)
FINDING_CODES = frozenset(
    {
        "missing-operation-id",
        "duplicate-operation-id",
        "no-success-response",
        "no-response-schema",
        "unresolvable-ref",
        "non-json-body",
        "missing-server",
        "undocumented-security",
        "invalid-against-spec",
        "unsupported-feature",
    }
)
_IGNORED_HEADERS = frozenset({"accept", "content-type", "authorization"})
_UPDATE = os.environ.get("DOC2MCP_UPDATE_GOLDENS") == "1"


def test_nullable_becomes_type_array() -> None:
    schema = _normalize_schema(
        {"type": "string", "nullable": True, "enum": ["a"], "example": "a"},
        dialect="3.0",
    )
    assert schema == {
        "type": ["string", "null"],
        "enum": ["a", None],
        "examples": ["a"],
    }
    Draft202012Validator(schema).validate("a")
    Draft202012Validator(schema).validate(None)


def test_nullable_without_type_is_dropped() -> None:
    assert _normalize_schema({"nullable": True, "description": "any"}, dialect="3.0") == {
        "description": "any"
    }


def test_nullable_combinator_allows_null() -> None:
    schema = _normalize_schema(
        {"allOf": [{"type": "string"}], "nullable": True},
        dialect="3.0",
    )
    assert schema == {"anyOf": [{"allOf": [{"type": "string"}]}, {"type": "null"}]}
    Draft202012Validator(schema).validate(None)
    Draft202012Validator(schema).validate("a")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"minimum": 1, "exclusiveMinimum": True}, {"exclusiveMinimum": 1}),
        ({"minimum": 1, "exclusiveMinimum": False}, {"minimum": 1}),
        ({"maximum": 2, "exclusiveMaximum": True}, {"exclusiveMaximum": 2}),
        ({"maximum": 2, "exclusiveMaximum": False}, {"maximum": 2}),
        ({"type": "number", "exclusiveMinimum": 1}, {"type": "number", "exclusiveMinimum": 1}),
    ],
)
def test_exclusive_bounds(raw: JsonObject, expected: JsonObject) -> None:
    assert _normalize_schema(raw, dialect="3.0") == expected


def test_schema_examples_merge() -> None:
    schema = _normalize_schema(
        {"type": "string", "examples": ["a"], "example": "b"},
        dialect="3.1",
    )
    assert schema == {"type": "string", "examples": ["a", "b"]}


def test_openapi_31_type_array_stays() -> None:
    raw: JsonObject = {"type": ["string", "null"]}
    assert _normalize_schema(raw, dialect="3.1") == raw


def test_ref_siblings_follow_dialect() -> None:
    def resolve(ref: str, pointer: str) -> JsonSchema:
        del ref, pointer
        return {"$ref": "#/defs/Pet"}

    original: JsonObject = {"$ref": "#/components/schemas/Pet", "description": "pet"}
    assert _normalize_schema(original, dialect="3.0", resolve_ref=resolve) == {"$ref": "#/defs/Pet"}
    assert _normalize_schema(original, dialect="3.1", resolve_ref=resolve) == {
        "allOf": [{"$ref": "#/defs/Pet"}, {"description": "pet"}]
    }


def test_parameter_merge_operation_wins(tmp_path: Path) -> None:
    spec = tmp_path / "merge.yaml"
    spec.write_text(
        """
openapi: 3.0.3
info: {title: Merge, version: "1"}
servers: [{url: https://example.test}]
paths:
  /pets/{id}:
    parameters:
      - name: limit
        in: query
        description: path level
        schema: {type: integer}
      - name: id
        in: path
        schema: {type: string}
    get:
      operationId: listPets
      parameters:
        - name: limit
          in: query
          description: operation level
          style: form
          explode: false
          schema:
            type: array
            items: {type: string}
            default: []
        - name: q
          in: query
          example: hat
          schema: {type: string, format: uuid, enum: [hat, cap]}
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {type: string}
""",
        encoding="utf-8",
    )
    loaded = ingest(str(spec), offline=True, allow_ref_hosts=())
    endpoint = loaded.endpoints[0]
    by_name = {parameter.name: parameter for parameter in endpoint.parameters}
    assert list(by_name) == ["limit", "id", "q"]
    assert by_name["limit"].description == "operation level"
    assert by_name["limit"].style == "form"
    assert by_name["limit"].explode is False
    assert by_name["limit"].schema_["default"] == []
    assert by_name["id"].required is True
    assert by_name["id"].style == "simple"
    assert by_name["id"].explode is False
    assert by_name["q"].schema_["format"] == "uuid"
    assert by_name["q"].schema_["enum"] == ["hat", "cap"]
    assert by_name["q"].examples == ["hat"]


def test_swagger_collection_format(tmp_path: Path) -> None:
    spec = tmp_path / "arrays.yaml"
    spec.write_text(
        """
swagger: "2.0"
info: {title: Arrays, version: "1"}
host: example.test
basePath: /v1
schemes: [https]
paths:
  /items:
    get:
      operationId: listItems
      parameters:
        - name: ids
          in: query
          type: array
          items: {type: string}
          collectionFormat: pipes
          enum: [a, b]
          default: [a]
      responses:
        "200":
          description: ok
          schema: {type: string, format: uuid}
""",
        encoding="utf-8",
    )
    loaded = ingest(str(spec), offline=True, allow_ref_hosts=())
    parameter = loaded.endpoints[0].parameters[0]
    assert parameter.style == "pipeDelimited"
    assert parameter.explode is False
    assert parameter.schema_["enum"] == ["a", "b"]
    assert parameter.schema_["default"] == ["a"]
    response = loaded.endpoints[0].responses["200"]
    assert response.schema_["format"] == "uuid"
    assert loaded.servers[0].url == "https://example.test/v1"


@pytest.mark.parametrize("name", SPECS)
def test_golden(name: str) -> None:
    spec = ingest(str(CORPUS / name), offline=True, allow_ref_hosts=())
    actual = dump_endpoints(spec).encode("utf-8")
    assert b"\r" not in actual
    golden = CORPUS / f"{name}.endpoints.json"
    if _UPDATE:
        golden.write_bytes(actual)
    assert golden.read_bytes() == actual
    assert dump_endpoints(ApiSpec.model_validate_json(actual)) == actual.decode("utf-8")


def test_each_finding_code_appears() -> None:
    seen: set[str] = set()
    for name in SPECS:
        spec = ingest(str(CORPUS / name), offline=True, allow_ref_hosts=())
        seen.update(finding.code for finding in spec.findings)
    assert seen == FINDING_CODES


def test_schema_keywords_are_2020_12() -> None:
    for name in SPECS:
        spec = ingest(str(CORPUS / name), offline=True, allow_ref_hosts=())
        seen: set[int] = set()
        for schema in _schemas(spec):
            _assert_schema(spec, schema, seen)


def test_copied_spec_is_byte_identical(tmp_path: Path) -> None:
    source = CORPUS / "petstore-3.0.yaml"
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / source.name).write_bytes(source.read_bytes())
    (second / source.name).write_bytes(source.read_bytes())
    left = ingest(str(first / source.name), offline=True, allow_ref_hosts=())
    right = ingest(str(second / source.name), offline=True, allow_ref_hosts=())
    assert dump_endpoints(left) == dump_endpoints(right)
    assert left.source.uri == source.name
    assert "/" not in left.source.uri and "\\" not in left.source.uri


def test_shuffling_mappings_is_byte_identical(tmp_path: Path) -> None:
    original = _load_document(CORPUS / "petstore-3.0.yaml")
    shuffled = _reverse_maps(original)
    first = tmp_path / "original.json"
    second = tmp_path / "shuffled.json"
    _write_json(first, original)
    _write_json(second, shuffled)
    left = ingest(str(first), offline=True, allow_ref_hosts=())
    right = ingest(str(second), offline=True, allow_ref_hosts=())
    assert _without_source_identity(dump_endpoints(left)) == _without_source_identity(
        dump_endpoints(right)
    )


def test_corpus_fields_survive() -> None:
    for name in SPECS:
        path = CORPUS / name
        source = _load_document(path)
        spec = ingest(str(path), offline=True, allow_ref_hosts=())
        _assert_fields(path, source, spec)


def test_offline_url_does_not_connect(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--offline", "ingest", "https://example.com/openapi.yaml"])
    assert code == ExitCode.USAGE
    assert "https://example.com/openapi.yaml" in capsys.readouterr().err
    code = main(["ingest", "https://example.com/openapi.yaml", "--offline"])
    assert code == ExitCode.USAGE


def test_cross_origin_ref_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = tmp_path / "remote.yaml"
    spec.write_text(
        """
openapi: 3.0.3
info: {title: Remote, version: "1"}
paths: {}
components:
  schemas:
    Pet:
      $ref: https://example.com/schema.yaml
""",
        encoding="utf-8",
    )
    code = main(["ingest", str(spec), "--out", str(tmp_path)])
    assert code == ExitCode.UPSTREAM
    assert "https://example.com/schema.yaml" in capsys.readouterr().err
    code = main(
        [
            "--offline",
            "--allow-ref-host",
            "example.com",
            "ingest",
            str(spec),
            "--out",
            str(tmp_path),
        ]
    )
    assert code == ExitCode.USAGE


def test_alias_bomb_exits_quickly(tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.yaml"
    bomb.write_text(
        """
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]
f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]
g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]
h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g]
""",
        encoding="utf-8",
    )
    started = time.perf_counter()
    code = main(["ingest", str(bomb), "--out", str(tmp_path)])
    assert time.perf_counter() - started < 1
    assert code == ExitCode.UPSTREAM


def test_html_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    page = tmp_path / "docs.html"
    page.write_text("<!doctype html><html><body>docs</body></html>", encoding="utf-8")
    code = main(["ingest", str(page), "--out", str(tmp_path)])
    assert code == ExitCode.UPSTREAM
    assert "OpenAPI document itself" in capsys.readouterr().err


def test_ingest_command_writes_endpoints(tmp_path: Path) -> None:
    spec = tmp_path / "api.yaml"
    spec.write_text(_TINY, encoding="utf-8")
    out = tmp_path / "out"
    code = main(
        [
            "--allow-ref-host",
            "a.example",
            "--allow-ref-host",
            "b.example",
            "ingest",
            str(spec),
            "--out",
            str(out),
            "--offline",
        ]
    )
    assert code == ExitCode.VERIFIED
    assert (out / "endpoints.json").is_file()


def test_primary_command_stops_before_curate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = tmp_path / "api.yaml"
    spec.write_text(_TINY, encoding="utf-8")
    out = tmp_path / "out"
    code = main([str(spec), "--out", str(out)])
    assert code == ExitCode.USAGE
    assert (out / "endpoints.json").is_file()
    assert not (out / "tools.yaml").exists()
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_primary_command_heuristic_stops_at_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = tmp_path / "api.yaml"
    spec.write_text(_TINY, encoding="utf-8")
    out = tmp_path / "out"
    code = main([str(spec), "--out", str(out), "--curation", "heuristic"])
    assert code == ExitCode.USAGE
    assert (out / "endpoints.json").is_file()
    assert (out / "tools.yaml").is_file()
    assert (out / "generated").is_dir()
    assert "report" in capsys.readouterr().err


def test_largest_spec_is_fast() -> None:
    largest = max((CORPUS / name for name in SPECS), key=lambda path: path.stat().st_size)
    started = time.perf_counter()
    ingest(str(largest), offline=True, allow_ref_hosts=())
    assert time.perf_counter() - started < 10


@settings(max_examples=20, deadline=None)
@given(st.data())
def test_mapping_order_does_not_change_output(data: st.DataObject) -> None:
    document = data.draw(_openapi_documents())
    permuted = _permute(document, data)
    assert _without_source_identity(_ingest_document(document)) == _without_source_identity(
        _ingest_document(permuted)
    )


@settings(max_examples=20, deadline=None)
@given(
    kind=st.sampled_from(["string", "integer", "number", "boolean"]),
    text=st.text(max_size=12),
    number=st.integers(min_value=-20, max_value=20),
    flag=st.booleans(),
)
def test_nullable_accepts_value_and_null(kind: str, text: str, number: int, flag: bool) -> None:
    schema = _normalize_schema({"type": kind, "nullable": True}, dialect="3.0")
    if kind == "string":
        value: JsonValue = text
    elif kind == "boolean":
        value = flag
    elif kind == "integer":
        value = number
    else:
        value = float(number)
    validator = Draft202012Validator(schema)
    validator.validate(value)
    validator.validate(None)


def _schemas(spec: ApiSpec) -> list[JsonSchema]:
    found: list[JsonSchema] = []
    found.extend(spec.defs.values())
    for endpoint in spec.endpoints:
        for parameter in endpoint.parameters:
            found.append(parameter.schema_)
        if endpoint.request_body is not None:
            found.append(endpoint.request_body.schema_)
        for response in endpoint.responses.values():
            if response.schema_ is not None:
                found.append(response.schema_)
            if response.headers is not None:
                found.extend(header.schema_ for header in response.headers.values())
    return found


_SCHEMA_OBJECTS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_MAPS = frozenset({"$defs", "dependentSchemas", "patternProperties", "properties"})
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


def _assert_schema(spec: ApiSpec, schema: JsonSchema, seen: set[int]) -> None:
    if isinstance(schema, bool):
        Draft202012Validator.check_schema(schema)
        return
    identity = id(schema)
    if identity in seen:
        return
    seen.add(identity)
    Draft202012Validator.check_schema(schema)
    assert "nullable" not in schema
    assert not isinstance(schema.get("exclusiveMinimum"), bool)
    assert not isinstance(schema.get("exclusiveMaximum"), bool)
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/defs/"):
        assert ref[len("#/defs/") :] in spec.defs
    for key, value in schema.items():
        if key in _SCHEMA_OBJECTS and isinstance(value, (dict, bool)):
            _assert_schema(spec, value, seen)
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            for item in value.values():
                if isinstance(item, (dict, bool)):
                    _assert_schema(spec, item, seen)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, bool)):
                    _assert_schema(spec, item, seen)


def _load_document(path: Path) -> JsonObject:
    raw = path.read_bytes()
    if path.suffix == ".json" or raw.lstrip().startswith((b"{", b"[")):
        loaded = expect_json(json.loads(raw))
    else:
        loaded = _load_yaml(raw)
    if not isinstance(loaded, dict):
        raise TypeError(path.name)
    return loaded


def _without_source_identity(text: str) -> str:
    payload = expect_json(json.loads(text))
    if isinstance(payload, dict):
        source = payload.get("source")
        if isinstance(source, dict):
            source["sha256"] = ""
            source["uri"] = ""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _write_json(path: Path, value: JsonValue) -> None:
    path.write_bytes(json.dumps(value).encode("utf-8"))


def _reverse_maps(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _reverse_maps(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reverse_maps(item) for item in value]
    return value


def _permute(value: JsonValue, data: st.DataObject) -> JsonValue:
    if isinstance(value, dict):
        keys = list(value)
        order = data.draw(st.permutations(keys))
        return {key: _permute(value[key], data) for key in order}
    if isinstance(value, list):
        return [_permute(item, data) for item in value]
    return value


def _ingest_document(document: JsonObject) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        handle.write(json.dumps(document).encode("utf-8"))
        name = handle.name
    try:
        return dump_endpoints(ingest(name, offline=True, allow_ref_hosts=()))
    finally:
        Path(name).unlink()


def _openapi_documents() -> st.SearchStrategy[JsonObject]:
    methods = st.lists(
        st.sampled_from(["get", "post", "delete"]),
        min_size=1,
        max_size=2,
        unique=True,
    )
    return st.builds(_document, st.integers(min_value=1, max_value=3), methods, st.booleans())


def _document(count: int, methods: list[str], nullable: bool) -> JsonObject:
    paths: JsonObject = {}
    for index in range(count):
        operations: JsonObject = {}
        schema: JsonObject = {"type": "string"}
        if nullable:
            schema["nullable"] = True
        for method in methods:
            operations[method] = {
                "operationId": f"op{index}{method}",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": schema}},
                    }
                },
            }
        paths[f"/p{index}"] = operations
    return {
        "openapi": "3.0.3",
        "info": {"title": "generated", "version": "1"},
        "servers": [{"url": "https://example.test"}],
        "paths": paths,
    }


def _assert_fields(path: Path, source: JsonObject, spec: ApiSpec) -> None:
    swagger = isinstance(source.get("swagger"), str)
    documents = {path.resolve(): source}
    by_id = {endpoint.id: endpoint for endpoint in spec.endpoints}
    paths = source.get("paths")
    if not isinstance(paths, dict):
        return
    for route, path_item in paths.items():
        if not isinstance(route, str):
            continue
        item = _follow(path, source, path_item, documents, set())
        if not isinstance(item, dict):
            continue
        for method, operation in _operations(item):
            if not isinstance(operation, dict):
                continue
            normalized = route if route.startswith("/") else f"/{route}"
            endpoint = by_id[f"{method.upper()} {normalized}"]
            merged = _merged_parameters(path, source, item, operation, documents)
            model_parameters = {
                (parameter.name, parameter.location): parameter for parameter in endpoint.parameters
            }
            for parameter in merged:
                name = parameter.get("name")
                location = parameter.get("in")
                if not isinstance(name, str) or not isinstance(location, str):
                    continue
                if location in {"body", "formData", "querystring"}:
                    continue
                if location == "header" and name.lower() in _IGNORED_HEADERS:
                    assert (name, location) not in model_parameters
                    continue
                if location not in {"path", "query", "header", "cookie"}:
                    continue
                found = model_parameters[(name, location)]
                _assert_parameter(path, source, parameter, found, spec, documents, swagger)


def _operations(path_item: JsonObject) -> list[tuple[str, JsonValue]]:
    found: list[tuple[str, JsonValue]] = []
    for method in ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query"):
        if method in path_item:
            found.append((method, path_item[method]))
    extra = path_item.get("additionalOperations")
    if isinstance(extra, dict):
        for name, operation in extra.items():
            if isinstance(name, str):
                found.append((name, operation))
    return found


def _merged_parameters(
    path: Path,
    source: JsonObject,
    path_item: JsonObject,
    operation: JsonObject,
    documents: dict[Path, JsonObject],
) -> list[JsonObject]:
    merged: list[JsonObject] = []
    index: dict[tuple[str, str], int] = {}
    for raw in (path_item.get("parameters"), operation.get("parameters")):
        if not isinstance(raw, list):
            continue
        for item in raw:
            parameter = _follow(path, source, item, documents, set())
            if not isinstance(parameter, dict):
                continue
            name = parameter.get("name")
            location = parameter.get("in")
            if not isinstance(name, str) or not isinstance(location, str):
                continue
            key = (name, location)
            if key in index:
                merged[index[key]] = parameter
            else:
                index[key] = len(merged)
                merged.append(parameter)
    return merged


def _assert_parameter(
    path: Path,
    source: JsonObject,
    parameter: JsonObject,
    found: Parameter,
    spec: ApiSpec,
    documents: dict[Path, JsonObject],
    swagger: bool,
) -> None:
    location = parameter.get("in")
    assert isinstance(location, str)
    if location == "path":
        assert found.required is True
    else:
        assert found.required is (parameter.get("required") is True)
    style = _expected_style(parameter, location, swagger)
    assert found.style == style
    if isinstance(parameter.get("explode"), bool):
        assert found.explode is parameter["explode"]
    else:
        assert found.explode is (style in {"form", "cookie"})
    raw_schema = _source_schema(parameter, swagger)
    resolved = None if raw_schema is None else _follow(path, source, raw_schema, documents, set())
    model_schema = _follow_model(spec, found.schema_, set())
    if isinstance(resolved, dict) and isinstance(model_schema, dict):
        if "enum" in resolved and isinstance(resolved["enum"], list):
            expected = list(resolved["enum"])
            if resolved.get("nullable") is True and None not in expected:
                expected.append(None)
            assert model_schema.get("enum") == expected
        if "format" in resolved:
            assert model_schema.get("format") == resolved["format"]
        if "default" in resolved:
            assert _same(model_schema.get("default"), resolved["default"])
    resolved_object = resolved if isinstance(resolved, dict) else {}
    source_examples = _example_values(parameter) + _example_values(resolved_object)
    model_examples = list(found.examples or []) + _example_values(
        model_schema if isinstance(model_schema, dict) else {}
    )
    for example in source_examples:
        assert any(_same(example, item) for item in model_examples)


def _expected_style(parameter: JsonObject, location: str, swagger: bool) -> str:
    style = parameter.get("style")
    if isinstance(style, str) and style:
        return style
    if swagger:
        collection = parameter.get("collectionFormat")
        if collection == "multi":
            return "form"
        if collection in {"csv", "tsv"}:
            return "form"
        if collection == "ssv":
            return "spaceDelimited"
        if collection == "pipes":
            return "pipeDelimited"
    if location in {"path", "header"}:
        return "simple"
    return "form"


def _source_schema(parameter: JsonObject, swagger: bool) -> JsonValue | None:
    if swagger:
        if parameter.get("type") == "file":
            return {"type": "string", "format": "binary"}
        schema: JsonObject = {}
        for key in (
            "type",
            "format",
            "items",
            "default",
            "enum",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "nullable",
        ):
            if key in parameter:
                schema[key] = parameter[key]
        return schema
    if "schema" in parameter:
        return parameter["schema"]
    content = parameter.get("content")
    if isinstance(content, dict) and content:
        from doc2mcp.ingest import _choose_media

        media = _choose_media(content)
        entry = content.get(media) if media is not None else None
        if isinstance(entry, dict):
            if "schema" in entry:
                return entry["schema"]
            if "itemSchema" in entry:
                return entry["itemSchema"]
    return None


def _example_values(node: JsonObject) -> list[JsonValue]:
    values: list[JsonValue] = []
    if "example" in node:
        values.append(node["example"])
    examples = node.get("examples")
    if isinstance(examples, list):
        values.extend(examples)
    elif isinstance(examples, dict):
        for name in sorted(examples):
            item = examples[name]
            if isinstance(item, dict) and "value" in item:
                values.append(item["value"])
    return values


def _follow(
    path: Path,
    source: JsonObject,
    value: JsonValue,
    documents: dict[Path, JsonObject],
    seen: set[str],
) -> JsonValue:
    current_path = path.resolve()
    current_doc = source
    current = value
    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        ref = current["$ref"]
        if ref in seen:
            return current
        seen.add(ref)
        if ref.startswith("#"):
            current = _pointer(current_doc, ref)
            continue
        target, _, fragment = ref.partition("#")
        current_path = (current_path.parent / target).resolve()
        current_doc = documents.get(current_path) or _load_document(current_path)
        documents[current_path] = current_doc
        current = _pointer(current_doc, f"#{fragment}" if fragment else "#")
    return current


def _follow_model(spec: ApiSpec, schema: JsonSchema, seen: set[str]) -> JsonSchema:
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/defs/"):
        return schema
    name = ref[len("#/defs/") :]
    if name in seen:
        return schema
    target = spec.defs.get(name)
    if target is None:
        return schema
    return _follow_model(spec, target, seen | {name})


def _pointer(document: JsonObject, pointer: str) -> JsonValue:
    if pointer in {"", "#"}:
        return document
    body = pointer[1:] if pointer.startswith("#") else pointer
    current: JsonValue = document
    for raw in body.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        if isinstance(current, list) and token.isdigit():
            current = current[int(token)]
            continue
        return None
    return current


def _same(left: JsonValue, right: JsonValue) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same(item, other) for item, other in zip(left, right, strict=True)
        )
    return left == right


_TINY = """
openapi: 3.0.3
info: {title: Tiny, version: "1"}
servers: [{url: https://example.test}]
paths:
  /ping:
    get:
      operationId: ping
      responses:
        "204": {description: pong}
"""
