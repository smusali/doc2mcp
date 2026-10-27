from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from doc2mcp.curate.agent import CurationOptions, curate
from doc2mcp.curate.manifest import (
    Manifest,
    Overrides,
    annotations_for,
    apply_overrides,
    build_manifest,
    canonical_flags,
    curation_hash,
    dump_manifest,
    exclusion_reason,
    forced_exclusions,
    heuristic_tools,
    load_manifest,
    name_error,
    published_manifest_schema_text,
    validation_errors,
    zero_usage,
)
from doc2mcp.ingest import ingest
from doc2mcp.model import (
    ApiSpec,
    Endpoint,
    Parameter,
    RequestBody,
    Response,
    SecurityScheme,
    Server,
    Source,
    dump_endpoints,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "manifest-v1.json"
_SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-api"
_STRING = {"type": "string"}
_OBJECT = {
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
    schema: dict[str, object] | None = None,
) -> Parameter:
    return Parameter(
        name=name,
        location=location,
        required=required,
        schema_=schema or _STRING,
        style="form",
        explode=True,
    )


def _response(schema: dict[str, object] | None = None) -> dict[str, Response]:
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


def _body(properties: dict[str, object], required: list[str]) -> RequestBody:
    return RequestBody(
        content_type="application/json",
        schema_={
            "additionalProperties": False,
            "properties": properties,
            "required": required,
            "type": "object",
        },
        required=True,
    )


def _write(out: Path, spec: ApiSpec) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "endpoints.json").write_bytes(dump_endpoints(spec).encode("utf-8"))


def _shelf() -> ApiSpec:
    return ApiSpec.model_validate_json((_SAMPLE / "endpoints.json").read_text(encoding="utf-8"))


def test_published_schema_matches() -> None:
    text = published_manifest_schema_text()
    assert text.endswith("\n")
    assert "\r" not in text
    assert _SCHEMA_PATH.read_text(encoding="utf-8") == text


def test_dump_round_trip_uses_lf_and_sorted_names() -> None:
    spec = _shelf()
    tools, excluded = heuristic_tools(
        spec,
        [endpoint.id for endpoint in spec.endpoints],
        namespace="",
        include_tags=(),
    )
    manifest = build_manifest(
        spec,
        tools,
        excluded,
        namespace="",
        mode="heuristic",
        model="claude-opus-5-5",
        digest="abc",
        usage=zero_usage(),
        fallback_tools=[],
        max_tools=20,
    )
    text = dump_manifest(manifest)
    assert text.endswith("\n")
    assert "\r" not in text
    again = dump_manifest(load_manifest(text))
    assert again == text
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    names = [item["name"] for item in loaded["tools"]]
    assert names == sorted(names)
    excluded_ids = [item["endpoint_id"] for item in loaded["excluded"]]
    assert excluded_ids == sorted(excluded_ids)


def test_hash_uses_minor_version_and_sorted_tags() -> None:
    raw = b"{}\n"
    left = curation_hash(
        raw,
        model="claude-opus-5-5",
        mode="agent",
        include_tags=("books", "admin"),
        max_tools=20,
        namespace="",
        version="0.1.0",
    )
    right = curation_hash(
        raw,
        model="claude-opus-5-5",
        mode="agent",
        include_tags=("admin", "books"),
        max_tools=20,
        namespace="",
        version="0.1.9",
    )
    assert left == right
    assert left != curation_hash(
        raw,
        model="claude-opus-5-5",
        mode="agent",
        include_tags=("admin", "books"),
        max_tools=20,
        namespace="",
        version="0.2.0",
    )
    assert "budget" not in canonical_flags(
        mode="heuristic",
        include_tags=("admin",),
        max_tools=15,
        namespace="shelf",
    )


def test_name_rules_allow_get_and_delete_as_the_action() -> None:
    assert name_error("get_book") is None
    assert name_error("delete_book") is None
    assert name_error("search_books") is None
    assert name_error("post_book") is not None
    assert name_error("book_get") is not None
    assert name_error("get_book_delete") is not None
    assert name_error("api_books") is not None
    assert name_error("list_v2") is not None
    assert name_error("shelf_get_book", namespace="shelf") is None
    assert name_error("shelf_post_book", namespace="shelf") is not None
    assert name_error("my_shelf_get_book", namespace="my_shelf") is None
    assert name_error("my_shelf_api_books", namespace="my_shelf") is not None


def test_annotations_follow_methods() -> None:
    read = annotations_for(["GET", "HEAD"])
    assert read.readOnlyHint is True
    assert read.destructiveHint is False
    assert read.idempotentHint is True
    assert read.openWorldHint is True
    removed = annotations_for(["DELETE"])
    assert removed.readOnlyHint is False
    assert removed.destructiveHint is True
    assert removed.idempotentHint is True
    changed = annotations_for(["POST", "PUT"])
    assert changed.readOnlyHint is False
    assert changed.destructiveHint is False
    assert changed.idempotentHint is False


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


def test_shelf_heuristic_is_total_and_hides_pagination() -> None:
    spec = _shelf()
    tools, excluded = heuristic_tools(
        spec,
        [endpoint.id for endpoint in spec.endpoints],
        namespace="",
        include_tags=(),
    )
    names = {tool.name for tool in tools}
    assert "return_loan" in names
    assert "renew_loan" in names
    assert "complete_upload" in names
    assert "list_book_loans" in names
    assert "create_upload" in names
    assert "update_part" in names
    covered = {step.endpoint_id for tool in tools for step in tool.steps}
    covered.update(item.endpoint_id for item in excluded)
    assert covered == {endpoint.id for endpoint in spec.endpoints}
    reasons = {item.endpoint_id: item.reason for item in excluded}
    assert reasons["GET /admin/audit-events"] == "admin"
    assert reasons["GET /books/{book_id}/page-count"] == "deprecated"
    books = next(tool for tool in tools if tool.name == "list_books")
    fixed = {(item.param, item.value, item.reason) for item in books.fixed}
    assert ("cursor", None, "first page") in fixed
    assert ("limit", 2, "hidden plumbing") in fixed
    assert "Returns the first page." in books.description
    manifest = build_manifest(
        spec,
        tools,
        excluded,
        namespace="",
        mode="heuristic",
        model="claude-opus-5-5",
        digest="abc",
        usage=zero_usage(),
        fallback_tools=[],
        max_tools=20,
    )
    assert validation_errors(manifest, spec, max_tools=20, include_tags=()) == []


def test_required_plumbing_stays_an_input() -> None:
    spec = _spec(
        [
            _endpoint(
                "GET",
                "/books",
                parameters=[
                    _param("cursor", "query", required=True),
                    Parameter(
                        name="Idempotency-Key",
                        location="header",
                        required=True,
                        schema_=_STRING,
                        style="simple",
                        explode=False,
                    ),
                ],
            )
        ]
    )
    tools, _excluded = heuristic_tools(
        spec,
        ["GET /books"],
        namespace="",
        include_tags=(),
    )
    tool = tools[0]
    assert any(item.name == "cursor" for item in tool.inputs)
    fixed = tool.fixed[0]
    assert fixed.param == "Idempotency-Key"
    assert fixed.value == "$generated"
    assert fixed.reason == "generated-per-call"


def test_over_target_fills_an_empty_rationale() -> None:
    spec = _spec([_endpoint("GET", "/books"), _endpoint("GET", "/patrons")])
    tools, excluded = heuristic_tools(
        spec,
        [endpoint.id for endpoint in spec.endpoints],
        namespace="",
        include_tags=(),
    )
    blank = [tool.model_copy(update={"rationale": ""}) for tool in tools]
    manifest = build_manifest(
        spec,
        blank,
        excluded,
        namespace="",
        mode="heuristic",
        model="claude-opus-5-5",
        digest="abc",
        usage=zero_usage(),
        fallback_tools=[],
        max_tools=1,
    )
    assert {tool.rationale for tool in manifest.tools} == {"Exceeds the tool target."}


def test_overrides_are_idempotent_and_warn_when_stale(tmp_path: Path) -> None:
    spec = _spec(
        [
            _endpoint("GET", "/items"),
            _endpoint("GET", "/admin/reports", tags=["admin"]),
            _endpoint("GET", "/basic", security=[{"basic": []}]),
        ],
        {"basic": SecurityScheme(type="http", scheme="basic")},
    )
    _write(tmp_path, spec)
    first = curate(tmp_path, CurationOptions(mode="heuristic", offline=True))
    assert first.manifest is not None
    overrides = Overrides.model_validate(
        {
            "descriptions": {"list_items": "List the items."},
            "exclude_endpoints": [],
            "fixed": [],
            "include_endpoints": ["GET /admin/reports", "GET /basic", "GET /missing"],
            "outputs": {"list_items": ["id"]},
            "renames": {"list_items": "search_items", "missing": "other"},
        }
    )
    updated, warnings = apply_overrides(first.manifest, overrides, spec)
    again, again_warnings = apply_overrides(updated, overrides, spec)
    assert dump_manifest(updated) == dump_manifest(again)
    assert again_warnings
    assert any(warning.startswith("stale override: rename missing") for warning in warnings)
    assert any(warning.startswith("stale override: include GET /missing") for warning in warnings)
    names = {tool.name for tool in updated.tools}
    assert "search_items" in names
    assert "list_items" not in names
    assert any(
        step.endpoint_id == "GET /admin/reports" for tool in updated.tools for step in tool.steps
    )
    assert any(
        item.endpoint_id == "GET /basic" and item.reason == "unsupported"
        for item in updated.excluded
    )
    described = next(tool for tool in updated.tools if tool.name == "search_items")
    assert described.description == "List the items."
    assert described.output.fields == ["id"]


def test_recurate_reports_final_names(tmp_path: Path) -> None:
    spec = _spec([_endpoint("GET", "/items")])
    _write(tmp_path, spec)
    options = CurationOptions(mode="heuristic", offline=True)
    curate(tmp_path, options)
    (tmp_path / "overrides.yaml").write_text(
        "renames:\n  list_items: search_items\n",
        encoding="utf-8",
    )
    renamed = curate(tmp_path, options)
    assert "tools added: search_items" in renamed.warnings
    assert "tools removed: list_items" in renamed.warnings
    stable = curate(tmp_path, options)
    assert renamed.manifest is not None
    assert stable.warnings == ()
    assert (tmp_path / "tools.yaml").read_bytes() == dump_manifest(renamed.manifest).encode("utf-8")


@settings(max_examples=20, deadline=None)
@given(
    note=st.text(min_size=0, max_size=24),
    new_name=st.text(alphabet="abcde_", min_size=1, max_size=12),
    max_tools=st.integers(min_value=1, max_value=8),
)
def test_validation_and_overrides_do_not_raise(note: str, new_name: str, max_tools: int) -> None:
    spec = _spec(
        [
            _endpoint("GET", "/items"),
            _endpoint("POST", "/items", body=_body({"name": _STRING}, ["name"])),
        ]
    )
    tools, excluded = heuristic_tools(
        spec,
        [endpoint.id for endpoint in spec.endpoints],
        namespace="",
        include_tags=(),
    )
    manifest = build_manifest(
        spec,
        tools,
        excluded,
        namespace="",
        mode="heuristic",
        model="claude-opus-5-5",
        digest="abc",
        usage=zero_usage(),
        fallback_tools=[],
        max_tools=max_tools,
    )
    errors = validation_errors(manifest, spec, max_tools=max_tools, include_tags=())
    assert isinstance(errors, list)
    overrides = Overrides(renames={"list_items": new_name}, descriptions={"list_items": note})
    first, _warnings = apply_overrides(manifest, overrides, spec)
    second, _again = apply_overrides(first, overrides, spec)
    assert isinstance(first, Manifest)
    assert dump_manifest(first) == dump_manifest(second)


def test_sample_endpoints_match_ingest() -> None:
    fresh = ingest(str(_SAMPLE / "openapi.yaml"), offline=True, allow_ref_hosts=())
    assert (_SAMPLE / "endpoints.json").read_bytes() == dump_endpoints(fresh).encode("utf-8")
    loaded = json.loads((_SAMPLE / "endpoints.json").read_text(encoding="utf-8"))
    assert len(loaded["endpoints"]) == 40
