from __future__ import annotations

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
)


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
    defs = bundled.get("$defs")
    assert isinstance(defs, dict)
    node = defs.get("Node")
    assert isinstance(node, dict)
    properties = node.get("properties")
    assert isinstance(properties, dict)
    assert properties.get("child") == {"$ref": "#/$defs/Node"}
