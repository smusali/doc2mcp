from __future__ import annotations

import json

import pytest
from mcp4api.curate.models import DetailDecision
from mcp4api.curate.rules import exclusion_reason, forced_exclusions, interpret_detail
from mcp4api.model import ApiSpec, SecurityScheme
from tests.conftest import _body, _endpoint, _param, _spec


def test_category_exclusions_and_include_tag() -> None:
    spec = _spec(
        [
            _endpoint("GET", "/admin/audit", tags=["admin"]),
            _endpoint("GET", "/internal/jobs", tags=["Internal"]),
            _endpoint("GET", "/books/{book_id}/page-count", deprecated=True),
            _endpoint("POST", "/hooks/webhook-endpoints", tags=["webhooks"]),
            _endpoint("DELETE", "/books"),
            _endpoint(
                "DELETE", "/books/{book_id}", parameters=[_param("book_id", "path", required=True)]
            ),
            _endpoint("POST", "/purge-jobs", operation_id="purgeJobs"),
            _endpoint("GET", "/books"),
        ]
    )
    reasons = {item.endpoint_id: item.reason for item in forced_exclusions(spec, ())}
    assert reasons["GET /admin/audit"] == "admin"
    assert reasons["GET /internal/jobs"] == "internal"
    assert reasons["GET /books/{book_id}/page-count"] == "deprecated"
    assert reasons["POST /hooks/webhook-endpoints"] == "webhook-management"
    assert reasons["DELETE /books"] == "destructive-bulk"
    assert reasons["POST /purge-jobs"] == "destructive-bulk"
    assert "DELETE /books/{book_id}" not in reasons
    assert "GET /books" not in reasons
    waived = {item.endpoint_id for item in forced_exclusions(spec, ("admin",))}
    assert "GET /admin/audit" not in waived
    assert exclusion_reason(spec.endpoints[0], {}, ("Admin",)) is None


def test_unsupported_auth_keeps_api_key_or_oauth() -> None:
    schemes = {
        "basic": SecurityScheme(type="http", scheme="basic"),
        "oauth": SecurityScheme(type="oauth2"),
        "api": SecurityScheme(type="apiKey", name="X-API-Key", location="header"),
        "bearer": SecurityScheme(type="http", scheme="bearer"),
        "mtls": SecurityScheme(type="mutualTLS"),
        "oidc": SecurityScheme(type="openIdConnect", open_id_connect_url="https://example.test"),
    }
    spec = _spec(
        [
            _endpoint("GET", "/basic", security=[{"basic": []}]),
            _endpoint("GET", "/oauth", security=[{"oauth": []}]),
            _endpoint("GET", "/either", security=[{"api": []}, {"oauth": []}]),
            _endpoint("GET", "/open"),
            _endpoint("GET", "/bearer", security=[{"bearer": []}]),
            _endpoint("GET", "/mtls", security=[{"mtls": []}]),
            _endpoint("GET", "/oidc", security=[{"oidc": []}]),
        ],
        schemes,
    )
    reasons = {item.endpoint_id: item.reason for item in forced_exclusions(spec, ())}
    assert reasons["GET /basic"] == "unsupported"
    assert reasons["GET /oauth"] == "unsupported"
    assert reasons["GET /mtls"] == "unsupported"
    assert reasons["GET /oidc"] == "unsupported"
    assert "GET /either" not in reasons
    assert "GET /open" not in reasons
    assert "GET /bearer" not in reasons


@pytest.mark.parametrize(
    "mutate",
    [
        "_unknown",
        "_unmapped",
        "_uncovered",
        "_bad_name",
        "_bad_dependency",
        "_self_dependency",
        "_bad_example",
        "_bad_annotation",
        "_bad_field",
        "_bad_bind",
        "_duplicate",
    ],
)
def test_validation_errors(mutate: str) -> None:
    spec = _post_spec()
    detail = _MUTATIONS[mutate](_detail())
    decision = DetailDecision.model_validate(detail)
    result = interpret_detail(
        spec,
        decision,
        allowed={"POST /items"},
        namespace="",
        include_tags=(),
        accepted_names=set(),
    )
    assert result.errors


def _post_spec() -> ApiSpec:
    return _spec(
        [
            _endpoint(
                "POST",
                "/items",
                body=_body({"name": {"type": "string"}}, ["name"]),
            )
        ]
    )


def _detail() -> dict[str, object]:
    return {
        "excluded": [],
        "tools": [
            {
                "annotations": {
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                    "readOnlyHint": False,
                },
                "depends_on": [],
                "description": "Create an item.",
                "examples": [{"name": "name", "value_json": '"example"'}],
                "fixed": [],
                "inputs": [
                    {
                        "description": "name",
                        "endpoint_id": "POST /items",
                        "in": "body",
                        "name": "name",
                        "param": "name",
                        "required": True,
                    }
                ],
                "intent": "Create an item",
                "name": "create_item",
                "output_fields": ["id"],
                "output_from_step": 0,
                "rationale": "Create an item.",
                "steps": [{"binds": [], "endpoint_id": "POST /items"}],
                "title": "Create Item",
            }
        ],
    }


def _first_tool(detail: dict[str, object]) -> dict[str, object]:
    tools = detail["tools"]
    if not isinstance(tools, list) or not tools:
        raise AssertionError("detail has no tool")
    tool = tools[0]
    if not isinstance(tool, dict):
        raise AssertionError("detail tool is not an object")
    return tool


def _unknown(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["steps"] = [{"binds": [], "endpoint_id": "GET /missing"}]
    return detail


def _unmapped(detail: dict[str, object]) -> dict[str, object]:
    tool = _first_tool(detail)
    inputs = tool["inputs"]
    examples = tool["examples"]
    if not isinstance(inputs, list) or not isinstance(examples, list):
        raise AssertionError("detail inputs are missing")
    inputs.append(
        {
            "description": "extra",
            "endpoint_id": "POST /items",
            "in": "query",
            "name": "extra",
            "param": "extra",
            "required": False,
        }
    )
    examples.append({"name": "extra", "value_json": '"example"'})
    return detail


def _uncovered(detail: dict[str, object]) -> dict[str, object]:
    tool = _first_tool(detail)
    tool["inputs"] = []
    tool["examples"] = []
    return detail


def _bad_name(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["name"] = "post_item"
    return detail


def _bad_dependency(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["depends_on"] = ["missing_tool"]
    return detail


def _self_dependency(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["depends_on"] = ["create_item"]
    return detail


def _bad_example(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["examples"] = [{"name": "name", "value_json": "1"}]
    return detail


def _bad_annotation(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["annotations"] = {
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "readOnlyHint": True,
    }
    return detail


def _bad_field(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["output_fields"] = ["missing"]
    return detail


def _bad_bind(detail: dict[str, object]) -> dict[str, object]:
    _first_tool(detail)["steps"] = [
        {
            "binds": [{"in": "query", "param": "name", "ref": "$steps[1].body.id"}],
            "endpoint_id": "POST /items",
        }
    ]
    return detail


def _duplicate(detail: dict[str, object]) -> dict[str, object]:
    tools = detail["tools"]
    assert isinstance(tools, list)
    copy = json.loads(json.dumps(tools[0]))
    copy["name"] = "make_item"
    tools.append(copy)
    return detail


_MUTATIONS = {
    "_unknown": _unknown,
    "_unmapped": _unmapped,
    "_uncovered": _uncovered,
    "_bad_name": _bad_name,
    "_bad_dependency": _bad_dependency,
    "_self_dependency": _self_dependency,
    "_bad_example": _bad_example,
    "_bad_annotation": _bad_annotation,
    "_bad_field": _bad_field,
    "_bad_bind": _bad_bind,
    "_duplicate": _duplicate,
}
