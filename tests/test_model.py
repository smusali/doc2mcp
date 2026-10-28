from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator
from mcp4api.model import (
    ApiSpec,
    Endpoint,
    Parameter,
    Response,
    Server,
    Source,
    bundle_schema,
    dump_endpoints,
    published_schema_text,
)

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "endpoint-v1.json"


def _sample() -> ApiSpec:
    return ApiSpec(
        title="Demo",
        version="1",
        servers=[Server(url="https://example.test")],
        security_schemes={},
        defs={
            "Node": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "child": {"$ref": "#/defs/Node"},
                },
            }
        },
        endpoints=[
            Endpoint(
                id="GET /nodes",
                operation_id="listNodes",
                method="GET",
                path="/nodes",
                parameters=[
                    Parameter(
                        name="limit",
                        location="query",
                        required=False,
                        schema_={"$ref": "#/defs/Node"},
                        style="form",
                        explode=True,
                    )
                ],
                responses={"200": Response(schema_={"$ref": "#/defs/Node"})},
                security=[],
                provenance="/paths/~1nodes/get",
            )
        ],
        findings=[],
        source=Source(kind="openapi", uri="demo.yaml", sha256="abc"),
    )


def test_published_schema_matches() -> None:
    text = published_schema_text()
    assert text.endswith("\n")
    assert "\r" not in text
    assert _SCHEMA_PATH.read_text(encoding="utf-8") == text


def test_dump_is_idempotent() -> None:
    text = dump_endpoints(_sample())
    again = ApiSpec.model_validate_json(text)
    assert dump_endpoints(again) == text
    assert "\r" not in text


def test_bundle_schema_resolves_recursion() -> None:
    spec = _sample()
    bundled = bundle_schema(spec, {"$ref": "#/defs/Node"})
    assert isinstance(bundled, dict)
    Draft202012Validator.check_schema(bundled)
    Draft202012Validator(bundled).validate({"id": "a", "child": {"id": "b"}})
    child = bundled["$defs"]["Node"]["properties"]["child"]
    assert child == {"$ref": "#/$defs/Node"}
