from __future__ import annotations

from pathlib import Path
from typing import Literal

from mcp4api.model import (
    ApiSpec,
    Endpoint,
    JsonObject,
    Parameter,
    RequestBody,
    Response,
    SecurityScheme,
    Server,
    Source,
    dump_endpoints,
    expect_json,
)

_STRING: JsonObject = {"type": "string"}
_OBJECT: JsonObject = {
    "additionalProperties": False,
    "properties": {"id": _STRING},
    "required": ["id"],
    "type": "object",
}


def _param(
    name: str,
    location: Literal["path", "query", "header", "cookie"],
    *,
    required: bool,
    schema: JsonObject | None = None,
) -> Parameter:
    return Parameter(
        name=name,
        location=location,
        required=required,
        schema_=schema or _STRING,
        style="form",
        explode=True,
    )


def _response(schema: JsonObject | None = None) -> dict[str, Response]:
    return {"200": Response(schema_=schema or _OBJECT)}


def _endpoint(
    method: str,
    path: str,
    *,
    parameters: list[Parameter] | None = None,
    body: RequestBody | None = None,
    responses: dict[str, Response] | None = None,
    tags: list[str] | None = None,
    deprecated: bool = False,
    security: list[dict[str, list[str]]] | None = None,
    operation_id: str | None = None,
    summary: str | None = None,
    description: str | None = None,
) -> Endpoint:
    return Endpoint(
        id=f"{method} {path}",
        operation_id=operation_id,
        method=method,
        path=path,
        summary=summary,
        description=description,
        tags=tags,
        deprecated=deprecated,
        parameters=parameters or [],
        request_body=body,
        responses=responses or _response(),
        security=[] if security is None else security,
        provenance=path,
    )


def _spec(
    endpoints: list[Endpoint],
    schemes: dict[str, SecurityScheme] | None = None,
) -> ApiSpec:
    return ApiSpec(
        title="Demo",
        version="1",
        servers=[Server(url="https://example.test")],
        security_schemes=schemes or {},
        defs={},
        endpoints=endpoints,
        findings=[],
        source=Source(kind="openapi", uri="demo.yaml", sha256="abc"),
    )


def _body(properties: JsonObject, required: list[str]) -> RequestBody:
    parsed = expect_json(
        {
            "additionalProperties": False,
            "properties": properties,
            "required": required,
            "type": "object",
        }
    )
    if not isinstance(parsed, dict):
        raise TypeError("schema must be an object")
    return RequestBody(
        content_type="application/json",
        schema_=parsed,
        required=True,
    )


def _write(out: Path, spec: ApiSpec) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "endpoints.json").write_bytes(dump_endpoints(spec).encode("utf-8"))
