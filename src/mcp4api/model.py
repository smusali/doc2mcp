from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, WithJsonSchema
from pydantic_core import CoreSchema, core_schema

JsonObject: TypeAlias = dict[str, "JsonValue"]
JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | JsonObject | None
JsonSchema: TypeAlias = bool | JsonObject

_DEFS_PREFIX = "#/defs/"
_BUNDLED_PREFIX = "#/$defs/"

FindingCode: TypeAlias = Literal[
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
]
FindingSeverity: TypeAlias = Literal["error", "warning", "info"]
ParameterLocation: TypeAlias = Literal["path", "query", "header", "cookie"]
SecuritySchemeType: TypeAlias = Literal["apiKey", "http", "mutualTLS", "oauth2", "openIdConnect"]
SecuritySchemeLocation: TypeAlias = Literal["query", "header", "cookie"]
SourceKind: TypeAlias = Literal["openapi", "swagger"]

_FROZEN = ConfigDict(frozen=True, populate_by_name=True)
_JSON_VALUE_SCHEMA: JsonObject = {
    "anyOf": [
        {"type": "null"},
        {"type": "boolean"},
        {"type": "number"},
        {"type": "string"},
        {"type": "array", "items": {}},
        {"type": "object"},
    ]
}
_JSON_SCHEMA_SCHEMA: JsonObject = {"anyOf": [{"type": "boolean"}, {"type": "object"}]}


def expect_json(value: object) -> JsonValue:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("non-finite number")
        return value
    if isinstance(value, list):
        return [expect_json(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = expect_json(item)
        return result
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def _as_json_schema(value: object) -> JsonSchema:
    parsed = expect_json(value)
    if isinstance(parsed, (bool, dict)):
        return parsed
    raise TypeError("schema must be a boolean or object")


def _as_json_object(value: object) -> JsonObject:
    parsed = expect_json(value)
    if isinstance(parsed, dict):
        return parsed
    raise TypeError("JSON object required")


class _JsonValidator:
    def __init__(self, validator: Callable[[object], JsonValue]) -> None:
        self._validator = validator

    def __get_pydantic_core_schema__(
        self,
        _source_type: object,
        _handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(self._validator)


JsonValueField = Annotated[
    JsonValue,
    _JsonValidator(expect_json),
    WithJsonSchema(_JSON_VALUE_SCHEMA),
]
JsonSchemaField = Annotated[
    JsonSchema,
    _JsonValidator(_as_json_schema),
    WithJsonSchema(_JSON_SCHEMA_SCHEMA),
]
JsonObjectField = Annotated[
    JsonObject,
    _JsonValidator(_as_json_object),
    WithJsonSchema({"type": "object"}),
]


class ServerVariable(BaseModel):
    model_config = _FROZEN

    default: str
    enum: list[str] | None = None
    description: str | None = None


class Server(BaseModel):
    model_config = _FROZEN

    url: str
    description: str | None = None
    variables: dict[str, ServerVariable] | None = None


class SecurityScheme(BaseModel):
    model_config = _FROZEN

    type: SecuritySchemeType
    description: str | None = None
    name: str | None = None
    location: Annotated[SecuritySchemeLocation | None, Field(alias="in")] = None
    scheme: str | None = None
    bearer_format: str | None = None
    flows: JsonObjectField | None = None
    open_id_connect_url: str | None = None


class Parameter(BaseModel):
    model_config = _FROZEN

    name: str
    location: Annotated[ParameterLocation, Field(alias="in")]
    required: bool
    schema_: Annotated[JsonSchemaField, Field(alias="schema")]
    style: str
    explode: bool
    description: str | None = None
    examples: list[JsonValueField] | None = None


class Header(BaseModel):
    model_config = _FROZEN

    required: bool
    schema_: Annotated[JsonSchemaField, Field(alias="schema")]
    description: str | None = None
    examples: list[JsonValueField] | None = None


class RequestBody(BaseModel):
    model_config = _FROZEN

    content_type: str
    schema_: Annotated[JsonSchemaField, Field(alias="schema")]
    required: bool
    examples: list[JsonValueField] | None = None


class Response(BaseModel):
    model_config = _FROZEN

    content_type: str | None = None
    schema_: Annotated[JsonSchemaField | None, Field(alias="schema")] = None
    examples: list[JsonValueField] | None = None
    headers: dict[str, Header] | None = None


class Endpoint(BaseModel):
    model_config = _FROZEN

    id: str
    operation_id: str | None = None
    method: str
    path: str
    summary: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    deprecated: bool = False
    parameters: list[Parameter]
    request_body: RequestBody | None = None
    responses: dict[str, Response]
    security: list[dict[str, list[str]]]
    provenance: str
    servers: list[Server] | None = None


class Finding(BaseModel):
    model_config = _FROZEN

    code: FindingCode
    severity: FindingSeverity
    endpoint_id: str | None = None
    pointer: str
    message: str


class Source(BaseModel):
    model_config = _FROZEN

    kind: SourceKind
    uri: str
    sha256: str


class ApiSpec(BaseModel):
    model_config = _FROZEN

    title: str
    version: str
    servers: list[Server]
    security_schemes: dict[str, SecurityScheme]
    defs: dict[str, JsonSchemaField]
    endpoints: list[Endpoint]
    findings: list[Finding]
    source: Source


def _expect_object(value: object) -> JsonObject:
    parsed = expect_json(value)
    if isinstance(parsed, dict):
        return parsed
    raise TypeError("endpoint model must dump to an object")


def dump_json(value: Mapping[str, object]) -> str:
    """Serialize one JSON object with sorted keys.

    Args:
        value: JSON object to write.

    Returns:
        UTF-8 JSON text with a trailing newline.
    """
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def dump_endpoints(spec: ApiSpec) -> str:
    return dump_json(_expect_object(spec.model_dump(mode="json", by_alias=True, exclude_none=True)))


def bundle_schema(spec: ApiSpec, schema: JsonSchema) -> JsonSchema:
    if isinstance(schema, bool):
        return schema
    order: list[str] = []
    seen: set[str] = set()

    def want(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        order.append(name)
        body = spec.defs.get(name)
        if isinstance(body, dict):
            _collect_refs(body, want)

    rewritten = _rewrite_schema(schema, want)
    if not isinstance(rewritten, dict) or not order:
        return rewritten
    defs: JsonObject = {}
    for name in order:
        body = spec.defs.get(name)
        if isinstance(body, dict):
            defs[name] = _rewrite_schema(body, want)
        elif isinstance(body, bool):
            defs[name] = body
    bundled = dict(rewritten)
    bundled["$defs"] = defs
    return bundled


def _collect_refs(schema: JsonObject, want: Callable[[str], None]) -> None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
        want(ref[len(_DEFS_PREFIX) :])
    for value in schema.values():
        _collect_value(value, want)


def _collect_value(value: JsonValue, want: Callable[[str], None]) -> None:
    if isinstance(value, dict):
        _collect_refs(value, want)
        return
    if isinstance(value, list):
        for item in value:
            _collect_value(item, want)


def _rewrite_schema(schema: JsonSchema, want: Callable[[str], None]) -> JsonSchema:
    if isinstance(schema, bool):
        return schema
    rewritten: JsonObject = {}
    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str) and value.startswith(_DEFS_PREFIX):
            name = value[len(_DEFS_PREFIX) :]
            want(name)
            rewritten[key] = f"{_BUNDLED_PREFIX}{name}"
            continue
        rewritten[key] = _rewrite_value(value, want)
    return rewritten


def _rewrite_value(value: JsonValue, want: Callable[[str], None]) -> JsonValue:
    if isinstance(value, dict):
        return _rewrite_schema(value, want)
    if isinstance(value, list):
        return [_rewrite_value(item, want) for item in value]
    return value
