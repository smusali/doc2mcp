from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal, TypeAlias

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field

from doc2mcp.model import (
    ApiSpec,
    Endpoint,
    JsonObject,
    JsonObjectField,
    JsonSchema,
    JsonSchemaField,
    JsonValue,
    JsonValueField,
    Parameter,
    SecurityScheme,
    bundle_schema,
    expect_json,
)

CRITERIA_VERSION: Literal["criteria-v1"] = "criteria-v1"
MANIFEST_VERSION: Literal["manifest-v1"] = "manifest-v1"

ExclusionReason: TypeAlias = Literal[
    "admin",
    "internal",
    "deprecated",
    "duplicate",
    "webhook-management",
    "destructive-bulk",
    "low-value",
    "unsupported",
    "invalid-curation",
]
CurationMode: TypeAlias = Literal["agent", "heuristic", "manual"]
ParameterIn: TypeAlias = Literal["path", "query", "header", "cookie", "body"]

_FROZEN = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")
_DECISION = ConfigDict(extra="ignore")
_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_NAMESPACE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_VERSION_TOKEN = re.compile(r"^v\d+$", re.IGNORECASE)
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INPUT_REF = re.compile(r"^\$input\.([A-Za-z_][A-Za-z0-9_]*)$")
_STEP_REF = re.compile(r"^\$steps\[(\d+)\]\.body((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\])*)$")
_BULK = re.compile(r"bulk|batch|purge", re.IGNORECASE)
_HTTP = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
_IDEMPOTENT = _SAFE | {"PUT", "DELETE"}
_PAGINATION = frozenset(
    {
        "cursor",
        "page",
        "offset",
        "limit",
        "per_page",
        "page_size",
        "page_token",
        "starting_after",
        "ending_before",
        "max_results",
    }
)
_HEADER_PLUMBING = frozenset({"accept", "content-type", "idempotency-key"})
_DEFS = "#/$defs/"
_NOTES: dict[ExclusionReason, str] = {
    "admin": "Admin operations are excluded unless the matching tag is included.",
    "internal": "Internal operations are excluded unless the matching tag is included.",
    "deprecated": "Deprecated operations are excluded unless the matching tag is included.",
    "webhook-management": "Webhook management is excluded unless the matching tag is included.",
    "destructive-bulk": (
        "Bulk destructive operations are excluded unless the matching tag is included."
    ),
    "unsupported": "The endpoint requires an authentication scheme the server cannot send.",
    "invalid-curation": "The tool failed validation and was not emitted.",
    "low-value": "Omitted because it does not map to a primary user intent.",
    "duplicate": "Omitted because another tool already covers it.",
}


class Annotations(BaseModel):
    model_config = _FROZEN

    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


class MapsTo(BaseModel):
    model_config = _FROZEN

    endpoint_id: str
    param: str
    in_: Annotated[ParameterIn, Field(alias="in")]


class ToolInput(BaseModel):
    model_config = _FROZEN

    name: str
    schema_: Annotated[JsonSchemaField, Field(alias="schema")]
    description: str
    required: bool
    maps_to: MapsTo


class FixedParam(BaseModel):
    model_config = _FROZEN

    endpoint_id: str
    param: str
    in_: Annotated[ParameterIn, Field(alias="in")]
    value: JsonValueField
    reason: str


class Bind(BaseModel):
    model_config = _FROZEN

    param: str
    in_: Annotated[ParameterIn, Field(alias="in")]
    ref: str


class Step(BaseModel):
    model_config = _FROZEN

    endpoint_id: str
    bind: list[Bind]


class ToolOutput(BaseModel):
    model_config = _FROZEN

    from_step: int
    fields: list[str]
    schema_: Annotated[JsonSchemaField, Field(alias="schema")]


class Verify(BaseModel):
    model_config = _FROZEN

    example_args: JsonObjectField
    depends_on: list[str]


class Tool(BaseModel):
    model_config = _FROZEN

    name: str
    title: str
    description: str
    intent: str
    annotations: Annotations
    inputs: list[ToolInput]
    fixed: list[FixedParam]
    steps: list[Step]
    output: ToolOutput
    verify: Verify
    rationale: str


class Excluded(BaseModel):
    model_config = _FROZEN

    endpoint_id: str
    reason: ExclusionReason
    note: str


class ApiInfo(BaseModel):
    model_config = _FROZEN

    title: str
    base_url: str
    namespace: str


class Usage(BaseModel):
    model_config = _FROZEN

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    estimated_usd: float = 0


class CurationInfo(BaseModel):
    model_config = _FROZEN

    mode: CurationMode
    model: str
    criteria_version: Literal["criteria-v1"] = CRITERIA_VERSION
    input_hash: str
    usage: Usage
    fallback_tools: list[str]


class Manifest(BaseModel):
    model_config = _FROZEN

    manifest_version: Literal["manifest-v1"] = MANIFEST_VERSION
    api: ApiInfo
    tools: list[Tool]
    excluded: list[Excluded]
    curation: CurationInfo


class AnnotationDraft(BaseModel):
    model_config = _DECISION

    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


class InputDraft(BaseModel):
    model_config = _DECISION

    name: str
    description: str
    required: bool
    endpoint_id: str
    param: str
    in_: Annotated[str, Field(alias="in")]


class FixedDraft(BaseModel):
    model_config = _DECISION

    endpoint_id: str
    param: str
    in_: Annotated[str, Field(alias="in")]
    value_json: str
    reason: str


class BindDraft(BaseModel):
    model_config = _DECISION

    param: str
    in_: Annotated[str, Field(alias="in")]
    ref: str


class StepDraft(BaseModel):
    model_config = _DECISION

    endpoint_id: str
    binds: list[BindDraft]


class ExampleDraft(BaseModel):
    model_config = _DECISION

    name: str
    value_json: str


class ToolDraft(BaseModel):
    model_config = _DECISION

    name: str
    title: str
    description: str
    intent: str
    annotations: AnnotationDraft
    inputs: list[InputDraft]
    fixed: list[FixedDraft]
    steps: list[StepDraft]
    output_from_step: int
    output_fields: list[str]
    examples: list[ExampleDraft]
    depends_on: list[str]
    rationale: str


class PlanExclusion(BaseModel):
    model_config = _DECISION

    endpoint_id: str
    reason: str
    note: str


class PlanTool(BaseModel):
    model_config = _DECISION

    name: str
    endpoint_ids: list[str]
    rationale: str


class PlanDecision(BaseModel):
    model_config = _DECISION

    tools: list[PlanTool]
    excluded: list[PlanExclusion]


class DetailDecision(BaseModel):
    model_config = _DECISION

    tools: list[ToolDraft]
    excluded: list[PlanExclusion]


class ExcludeOverride(BaseModel):
    model_config = _DECISION

    endpoint_id: str
    reason: str
    note: str = ""


class FixedOverride(BaseModel):
    model_config = _DECISION

    tool: str
    endpoint_id: str
    param: str
    in_: Annotated[ParameterIn, Field(alias="in")]
    value: JsonValueField
    reason: str


class Overrides(BaseModel):
    model_config = _DECISION

    renames: dict[str, str] = Field(default_factory=dict)
    descriptions: dict[str, str] = Field(default_factory=dict)
    exclude_endpoints: list[ExcludeOverride] = Field(default_factory=list)
    include_endpoints: list[str] = Field(default_factory=list)
    fixed: list[FixedOverride] = Field(default_factory=list)
    outputs: dict[str, list[str]] = Field(default_factory=dict)


@dataclass(frozen=True)
class PlanResult:
    tools: tuple[PlanTool, ...]
    excluded: tuple[Excluded, ...]
    uncovered: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class DetailResult:
    tools: tuple[Tool, ...]
    excluded: tuple[Excluded, ...]
    errors: tuple[str, ...]


def zero_usage() -> Usage:
    return Usage()


def base_url(spec: ApiSpec) -> str:
    if not spec.servers:
        return ""
    server = spec.servers[0]
    url = server.url
    for name, variable in (server.variables or {}).items():
        url = url.replace("{" + name + "}", variable.default)
    return url


def minor_version(version: str) -> str:
    numbers: list[str] = []
    for part in version.split(".")[:2]:
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        numbers.append(digits or "0")
    if len(numbers) < 2:
        numbers.append("0")
    return ".".join(numbers)


def canonical_flags(
    *,
    mode: str,
    include_tags: tuple[str, ...],
    max_tools: int,
    namespace: str,
) -> str:
    tags = ",".join(sorted(include_tags))
    return f"curation={mode};include_tag={tags};max_tools={max_tools};namespace={namespace}"


def curation_hash(
    endpoints: bytes,
    *,
    model: str,
    mode: str,
    include_tags: tuple[str, ...],
    max_tools: int,
    namespace: str,
    version: str,
) -> str:
    payload = b"\0".join(
        (
            endpoints,
            CRITERIA_VERSION.encode("utf-8"),
            model.encode("utf-8"),
            minor_version(version).encode("utf-8"),
            canonical_flags(
                mode=mode,
                include_tags=include_tags,
                max_tools=max_tools,
                namespace=namespace,
            ).encode("utf-8"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def namespace_error(namespace: str) -> str | None:
    if namespace and _NAMESPACE.fullmatch(namespace) is None:
        return f"namespace {namespace} is not allowed"
    return None


def name_error(name: str, *, namespace: str = "") -> str | None:
    if _NAME.fullmatch(name) is None:
        return f"tool name {name} is not allowed"
    short = name
    verb_at = 0
    if namespace and name.startswith(f"{namespace}_"):
        short = name[len(namespace) + 1 :]
        verb_at = len([token for token in re.split(r"[_-]", namespace) if token])
    if short.startswith("api_"):
        return f"tool name {name} uses an api_ prefix"
    tokens = [token for token in re.split(r"[_-]", name) if token]
    for index, token in enumerate(tokens):
        if _VERSION_TOKEN.fullmatch(token):
            return f"tool name {name} contains a version segment"
        lowered = token.lower()
        if lowered in _HTTP and not (index == verb_at and lowered in {"get", "delete"}):
            return f"tool name {name} contains an HTTP verb"
    return None


def apply_namespace(name: str, namespace: str) -> str:
    if not namespace:
        return name
    prefix = f"{namespace}_"
    short = name[len(prefix) :] if name.startswith(prefix) else name
    return f"{prefix}{short}"


def annotations_for(methods: list[str]) -> Annotations:
    upper = [method.upper() for method in methods] or ["GET"]
    return Annotations(
        readOnlyHint=all(method in _SAFE for method in upper),
        destructiveHint=any(method == "DELETE" for method in upper),
        idempotentHint=all(method in _IDEMPOTENT for method in upper),
        openWorldHint=True,
    )


def parse_reason(value: str) -> ExclusionReason | None:
    if value == "admin":
        return "admin"
    if value == "internal":
        return "internal"
    if value == "deprecated":
        return "deprecated"
    if value == "duplicate":
        return "duplicate"
    if value == "webhook-management":
        return "webhook-management"
    if value == "destructive-bulk":
        return "destructive-bulk"
    if value == "low-value":
        return "low-value"
    if value == "unsupported":
        return "unsupported"
    if value == "invalid-curation":
        return "invalid-curation"
    return None


def parse_location(value: str) -> ParameterIn | None:
    if value == "path":
        return "path"
    if value == "query":
        return "query"
    if value == "header":
        return "header"
    if value == "cookie":
        return "cookie"
    if value == "body":
        return "body"
    return None


def exclusion_reason(
    endpoint: Endpoint,
    schemes: dict[str, SecurityScheme],
    include_tags: tuple[str, ...],
) -> ExclusionReason | None:
    included = {tag.lower() for tag in include_tags}
    if _is_admin(endpoint) and not _waived("admin", endpoint, included):
        return "admin"
    if _is_internal(endpoint) and not _waived("internal", endpoint, included):
        return "internal"
    if endpoint.deprecated and "deprecated" not in included:
        return "deprecated"
    if _is_webhook(endpoint) and not _waived("webhook-management", endpoint, included):
        return "webhook-management"
    if _is_bulk(endpoint) and not _waived("destructive-bulk", endpoint, included):
        return "destructive-bulk"
    if not _auth_supported(endpoint, schemes):
        return "unsupported"
    return None


def bundle_parameter(spec: ApiSpec, parameter: Parameter) -> JsonSchema:
    return bundle_schema(spec, parameter.schema_)


def endpoint_index_document(spec: ApiSpec, include_tags: tuple[str, ...]) -> str:
    forced = {item.endpoint_id: item.reason for item in forced_exclusions(spec, include_tags)}
    rows: list[JsonObject] = []
    for endpoint in sorted(spec.endpoints, key=lambda item: item.id):
        rows.append(
            {
                "description": _shield_doc(endpoint.description or ""),
                "eligible": endpoint.id not in forced,
                "exclusion": forced.get(endpoint.id, ""),
                "id": endpoint.id,
                "method": endpoint.method,
                "parameters": [
                    {"in": parameter.location, "name": parameter.name}
                    for parameter in endpoint.parameters
                ],
                "summary": _shield_doc(endpoint.summary or ""),
                "tags": [_shield_doc(tag) for tag in endpoint.tags or []],
            }
        )
    return _shield_doc(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))


def forced_exclusions(spec: ApiSpec, include_tags: tuple[str, ...]) -> list[Excluded]:
    found: list[Excluded] = []
    for endpoint in spec.endpoints:
        reason = exclusion_reason(endpoint, spec.security_schemes, include_tags)
        if reason is None:
            continue
        found.append(Excluded(endpoint_id=endpoint.id, reason=reason, note=_NOTES[reason]))
    return found


def dump_manifest(manifest: Manifest) -> str:
    ordered = manifest.model_copy(
        update={
            "tools": sorted(manifest.tools, key=lambda tool: tool.name),
            "excluded": sorted(manifest.excluded, key=lambda item: item.endpoint_id),
        }
    )
    payload = _object(ordered.model_dump(mode="json", by_alias=True))
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=4096)
    if not text.endswith("\n"):
        text += "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_manifest(text: str) -> Manifest:
    return Manifest.model_validate(_yaml_object(text))


def load_overrides(text: str) -> Overrides:
    if not text.strip():
        return Overrides()
    return Overrides.model_validate(_yaml_object(text))


def published_manifest_schema_text() -> str:
    schema = Manifest.model_json_schema(by_alias=True)
    return json.dumps(_object(schema), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_manifest(
    spec: ApiSpec,
    tools: list[Tool],
    excluded: list[Excluded],
    *,
    namespace: str,
    mode: CurationMode,
    model: str,
    digest: str,
    usage: Usage,
    fallback_tools: list[str],
    max_tools: int,
) -> Manifest:
    ready: list[Tool] = []
    over_target = len(tools) > max_tools
    for tool in tools:
        rationale = tool.rationale.strip()
        if over_target and not rationale:
            rationale = "Exceeds the tool target."
        elif not rationale:
            rationale = tool.rationale
        ready.append(tool.model_copy(update={"rationale": rationale or "Curated tool."}))
    claimed: set[str] = set()
    unique_excluded: list[Excluded] = []
    for item in excluded:
        if item.endpoint_id in claimed:
            continue
        claimed.add(item.endpoint_id)
        unique_excluded.append(item)
    return Manifest(
        api=ApiInfo(title=spec.title, base_url=base_url(spec), namespace=namespace),
        tools=ready,
        excluded=unique_excluded,
        curation=CurationInfo(
            mode=mode,
            model=model,
            input_hash=digest,
            usage=usage,
            fallback_tools=sorted(set(fallback_tools)),
        ),
    )


def interpret_plan(
    spec: ApiSpec,
    decision: PlanDecision,
    include_tags: tuple[str, ...],
) -> PlanResult:
    forced = {item.endpoint_id for item in forced_exclusions(spec, include_tags)}
    known = {endpoint.id for endpoint in spec.endpoints}
    errors: list[str] = []
    placed: dict[str, str] = {}
    duplicates: set[str] = set()
    excluded: list[Excluded] = []
    for item in decision.excluded:
        if item.endpoint_id not in known:
            errors.append(f"unknown endpoint {item.endpoint_id}")
            continue
        if item.endpoint_id in forced or item.endpoint_id in placed:
            continue
        reason = parse_reason(item.reason)
        if reason is None:
            errors.append(f"unknown exclusion reason {item.reason}")
            continue
        placed[item.endpoint_id] = ""
        note = item.note.strip() or _NOTES[reason]
        excluded.append(Excluded(endpoint_id=item.endpoint_id, reason=reason, note=note))
    tools: list[PlanTool] = []
    for tool in decision.tools:
        kept: list[str] = []
        for endpoint_id in tool.endpoint_ids:
            if endpoint_id not in known:
                errors.append(f"unknown endpoint {endpoint_id}")
                continue
            if endpoint_id in forced:
                continue
            if endpoint_id in placed:
                errors.append(f"duplicate endpoint {endpoint_id}")
                duplicates.add(endpoint_id)
                continue
            placed[endpoint_id] = tool.name
            kept.append(endpoint_id)
        if kept:
            tools.append(PlanTool(name=tool.name, endpoint_ids=kept, rationale=tool.rationale))
    if duplicates:
        tools = [
            PlanTool(
                name=tool.name,
                endpoint_ids=[item for item in tool.endpoint_ids if item not in duplicates],
                rationale=tool.rationale,
            )
            for tool in tools
            if any(item not in duplicates for item in tool.endpoint_ids)
        ]
        for endpoint_id in duplicates:
            placed.pop(endpoint_id, None)
    uncovered: list[str] = []
    for endpoint in spec.endpoints:
        if endpoint.id in forced or endpoint.id in placed:
            continue
        uncovered.append(endpoint.id)
        errors.append(f"missing endpoint {endpoint.id}")
    return PlanResult(
        tools=tuple(tool for tool in tools if tool.endpoint_ids),
        excluded=tuple(excluded),
        uncovered=tuple(uncovered),
        errors=tuple(errors),
    )


def interpret_detail(
    spec: ApiSpec,
    decision: DetailDecision,
    *,
    allowed: set[str],
    namespace: str,
    include_tags: tuple[str, ...],
    accepted_names: set[str],
) -> DetailResult:
    forced = {item.endpoint_id: item for item in forced_exclusions(spec, include_tags)}
    errors: list[str] = []
    tools: list[Tool] = []
    excluded: list[Excluded] = []
    names = {apply_namespace(draft.name, namespace) for draft in decision.tools}
    names.update(accepted_names)
    seen_names: set[str] = set()
    for draft in decision.tools:
        tool, draft_errors = _assemble_draft(
            spec,
            draft,
            allowed=allowed,
            namespace=namespace,
            forced=set(forced),
            known_names=names,
        )
        errors.extend(draft_errors)
        if tool is None:
            continue
        if tool.name in seen_names or tool.name in accepted_names:
            errors.append(f"duplicate tool name {tool.name}")
            continue
        seen_names.add(tool.name)
        tools.append(tool)
    for item in decision.excluded:
        if item.endpoint_id not in allowed or item.endpoint_id in forced:
            continue
        reason = parse_reason(item.reason)
        if reason is None:
            errors.append(f"unknown exclusion reason {item.reason}")
            continue
        note = item.note.strip() or _NOTES[reason]
        excluded.append(Excluded(endpoint_id=item.endpoint_id, reason=reason, note=note))
    seen_endpoints: set[str] = set()
    for tool in tools:
        for step in tool.steps:
            if step.endpoint_id in seen_endpoints:
                errors.append(f"duplicate endpoint {step.endpoint_id}")
            seen_endpoints.add(step.endpoint_id)
    if _has_cycle(tools):
        errors.append("tool dependencies contain a cycle")
    if errors:
        return DetailResult(tools=(), excluded=tuple(excluded), errors=tuple(errors))
    return DetailResult(tools=tuple(tools), excluded=tuple(excluded), errors=())


def validation_errors(
    manifest: Manifest,
    spec: ApiSpec,
    *,
    max_tools: int,
    include_tags: tuple[str, ...],
) -> list[str]:
    try:
        return _validation_errors(manifest, spec, max_tools=max_tools, include_tags=include_tags)
    except (ValueError, KeyError, TypeError, RecursionError, SchemaError, ValidationError) as exc:
        return [exc.__class__.__name__]


def heuristic_tools(
    spec: ApiSpec,
    endpoint_ids: list[str],
    *,
    namespace: str,
    include_tags: tuple[str, ...],
    fallback: bool = False,
    reserved_names: set[str] | None = None,
) -> tuple[list[Tool], list[Excluded]]:
    forced = {item.endpoint_id: item for item in forced_exclusions(spec, include_tags)}
    by_id = {endpoint.id: endpoint for endpoint in spec.endpoints}
    excluded = [forced[endpoint_id] for endpoint_id in endpoint_ids if endpoint_id in forced]
    chosen = [
        by_id[endpoint_id]
        for endpoint_id in endpoint_ids
        if endpoint_id in by_id and endpoint_id not in forced
    ]
    chosen.sort(key=lambda endpoint: endpoint.id)
    drafts = [_heuristic_name(endpoint) for endpoint in chosen]
    counts = Counter(item[0] for item in drafts)
    used = set(reserved_names or ())
    tools: list[Tool] = []
    for endpoint, (base, action) in zip(chosen, drafts, strict=True):
        name = base
        if counts[base] > 1:
            parent = _parent_segment(endpoint.path)
            if parent:
                name = _with_parent(base, parent)
        if name in used:
            stem = name
            number = 2
            while f"{stem}_{number}" in used:
                number += 1
            name = f"{stem}_{number}"
        used.add(name)
        tools.append(
            _heuristic_tool(
                spec,
                endpoint,
                name=apply_namespace(name, namespace),
                action=action,
                fallback=fallback,
            )
        )
    return tools, excluded


def apply_overrides(
    manifest: Manifest,
    overrides: Overrides,
    spec: ApiSpec,
) -> tuple[Manifest, list[str]]:
    warnings: list[str] = []
    by_id = {endpoint.id: endpoint for endpoint in spec.endpoints}
    tools = list(manifest.tools)
    excluded = list(manifest.excluded)
    tools, excluded = _apply_exclusions(tools, excluded, overrides, by_id, warnings)
    tools, excluded = _apply_inclusions(
        tools,
        excluded,
        overrides,
        spec,
        manifest.api.namespace,
        warnings,
    )
    tools = _apply_descriptions(tools, overrides, warnings)
    tools = _apply_fixed(tools, overrides, warnings)
    tools = _apply_outputs(tools, overrides, spec, warnings)
    tools = _apply_renames(tools, overrides, manifest.api.namespace, warnings)
    updated = manifest.model_copy(update={"tools": tools, "excluded": excluded})
    return updated, warnings


def invalid_exclusions(endpoint_ids: list[str], note: str) -> list[Excluded]:
    text = note.strip() or _NOTES["invalid-curation"]
    return [
        Excluded(endpoint_id=endpoint_id, reason="invalid-curation", note=text)
        for endpoint_id in endpoint_ids
    ]


def _validation_errors(
    manifest: Manifest,
    spec: ApiSpec,
    *,
    max_tools: int,
    include_tags: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    by_id = {endpoint.id: endpoint for endpoint in spec.endpoints}
    forced = {item.endpoint_id: item.reason for item in forced_exclusions(spec, include_tags)}
    seen_names: set[str] = set()
    coverage: dict[str, str] = {}
    for tool in manifest.tools:
        problem = name_error(tool.name, namespace=manifest.api.namespace)
        if problem:
            errors.append(problem)
        if tool.name in seen_names:
            errors.append(f"duplicate tool name {tool.name}")
        seen_names.add(tool.name)
        if not tool.steps:
            errors.append(f"{tool.name} has no steps")
        methods: list[str] = []
        for step in tool.steps:
            endpoint = by_id.get(step.endpoint_id)
            if endpoint is None:
                errors.append(f"unknown endpoint {step.endpoint_id}")
                continue
            if step.endpoint_id in coverage:
                errors.append(f"duplicate endpoint {step.endpoint_id}")
            coverage[step.endpoint_id] = tool.name
            reason = forced.get(step.endpoint_id)
            if reason is not None:
                errors.append(f"{step.endpoint_id} must stay excluded ({reason})")
            methods.append(endpoint.method)
            errors.extend(_bind_errors(spec, tool, step, endpoint))
            errors.extend(_coverage_errors(spec, tool, step, endpoint))
        if methods and tool.annotations != annotations_for(methods):
            errors.append(f"{tool.name} annotations do not match its methods")
        errors.extend(_input_errors(spec, tool))
        errors.extend(_output_errors(spec, tool))
        errors.extend(_example_errors(tool))
        for dep in tool.verify.depends_on:
            if dep == tool.name or dep not in {item.name for item in manifest.tools}:
                errors.append(f"{tool.name} depends on unknown tool {dep}")
    if _has_cycle(manifest.tools):
        errors.append("tool dependencies contain a cycle")
    for item in manifest.excluded:
        if item.endpoint_id not in by_id:
            errors.append(f"unknown endpoint {item.endpoint_id}")
            continue
        if item.endpoint_id in coverage:
            errors.append(f"duplicate endpoint {item.endpoint_id}")
        coverage[item.endpoint_id] = ""
        expected = forced.get(item.endpoint_id)
        if expected is not None and item.reason != expected:
            errors.append(f"{item.endpoint_id} exclusion reason must be {expected}")
    for endpoint in spec.endpoints:
        if endpoint.id not in coverage:
            errors.append(f"missing endpoint {endpoint.id}")
    if len(manifest.tools) > max_tools and any(
        not tool.rationale.strip() for tool in manifest.tools
    ):
        errors.append("exceeding the tool target requires a rationale")
    return errors


def _assemble_draft(
    spec: ApiSpec,
    draft: ToolDraft,
    *,
    allowed: set[str],
    namespace: str,
    forced: set[str],
    known_names: set[str],
) -> tuple[Tool | None, list[str]]:
    errors: list[str] = []
    name = apply_namespace(draft.name, namespace)
    problem = name_error(name, namespace=namespace)
    if problem:
        errors.append(problem)
    steps: list[Step] = []
    for step_draft in draft.steps:
        if step_draft.endpoint_id in forced:
            continue
        if step_draft.endpoint_id not in allowed:
            errors.append(f"{step_draft.endpoint_id} is outside this tool group")
            continue
        binds: list[Bind] = []
        for bind in step_draft.binds:
            location = parse_location(bind.in_)
            if location is None:
                errors.append(f"unknown parameter location {bind.in_}")
                continue
            binds.append(Bind(param=bind.param, in_=location, ref=bind.ref))
        steps.append(Step(endpoint_id=step_draft.endpoint_id, bind=binds))
    if not steps:
        if errors:
            return None, errors
        return None, []
    inputs: list[ToolInput] = []
    for input_draft in draft.inputs:
        location = parse_location(input_draft.in_)
        if location is None:
            errors.append(f"unknown parameter location {input_draft.in_}")
            continue
        schema = _input_schema(spec, input_draft, steps)
        if schema is None:
            errors.append(f"input {input_draft.name} does not map to a parameter")
            continue
        inputs.append(
            ToolInput(
                name=input_draft.name,
                schema_=schema,
                description=input_draft.description,
                required=input_draft.required,
                maps_to=MapsTo(
                    endpoint_id=input_draft.endpoint_id,
                    param=input_draft.param,
                    in_=location,
                ),
            )
        )
    fixed: list[FixedParam] = []
    for fixed_draft in draft.fixed:
        location = parse_location(fixed_draft.in_)
        if location is None:
            errors.append(f"unknown parameter location {fixed_draft.in_}")
            continue
        value, value_error = parse_scalar(fixed_draft.value_json)
        if value_error:
            errors.append(value_error)
            continue
        fixed.append(
            FixedParam(
                endpoint_id=fixed_draft.endpoint_id,
                param=fixed_draft.param,
                in_=location,
                value=value,
                reason=fixed_draft.reason,
            )
        )
    output_schema, output_error = _draft_output(
        spec, steps, draft.output_from_step, draft.output_fields
    )
    if output_error:
        errors.append(output_error)
    examples, example_error = _draft_examples(draft.examples)
    if example_error:
        errors.append(example_error)
    for dep in draft.depends_on:
        if dep not in known_names or dep == name:
            errors.append(f"{name} depends on unknown tool {dep}")
    if errors or output_schema is None:
        return None, errors
    inputs = sorted(inputs, key=lambda item: (not item.required, item.name))
    tool = Tool(
        name=name,
        title=draft.title,
        description=draft.description,
        intent=draft.intent,
        annotations=Annotations(
            readOnlyHint=draft.annotations.readOnlyHint,
            destructiveHint=draft.annotations.destructiveHint,
            idempotentHint=draft.annotations.idempotentHint,
            openWorldHint=draft.annotations.openWorldHint,
        ),
        inputs=inputs,
        fixed=fixed,
        steps=steps,
        output=ToolOutput(
            from_step=draft.output_from_step,
            fields=list(draft.output_fields),
            schema_=output_schema,
        ),
        verify=Verify(example_args=examples, depends_on=list(draft.depends_on)),
        rationale=draft.rationale,
    )
    errors.extend(_tool_errors(spec, tool))
    if errors:
        return None, errors
    return tool, []


def _tool_errors(spec: ApiSpec, tool: Tool) -> list[str]:
    errors: list[str] = []
    by_id = {endpoint.id: endpoint for endpoint in spec.endpoints}
    methods: list[str] = []
    for step in tool.steps:
        endpoint = by_id.get(step.endpoint_id)
        if endpoint is None:
            errors.append(f"unknown endpoint {step.endpoint_id}")
            continue
        methods.append(endpoint.method)
        errors.extend(_bind_errors(spec, tool, step, endpoint))
        errors.extend(_coverage_errors(spec, tool, step, endpoint))
    if methods and tool.annotations != annotations_for(methods):
        errors.append(f"{tool.name} annotations do not match its methods")
    errors.extend(_input_errors(spec, tool))
    errors.extend(_output_errors(spec, tool))
    errors.extend(_example_errors(tool))
    return errors


def _input_errors(spec: ApiSpec, tool: Tool) -> list[str]:
    errors: list[str] = []
    referenced = _referenced_inputs(tool)
    seen: set[str] = set()
    for item in tool.inputs:
        if item.name in seen:
            errors.append(f"duplicate input {item.name}")
        seen.add(item.name)
        mapped = _target_exists(
            spec,
            item.maps_to.endpoint_id,
            item.maps_to.param,
            item.maps_to.in_,
        )
        if not mapped and item.name not in referenced:
            errors.append(f"input {item.name} does not map to a parameter")
            continue
        expected = _input_schema_for(
            spec, item.maps_to.endpoint_id, item.maps_to.param, item.maps_to.in_, tool
        )
        if expected is not None and not _same_schema(item.schema_, expected):
            errors.append(f"input {item.name} schema was not copied from the endpoint")
    return errors


def _output_errors(spec: ApiSpec, tool: Tool) -> list[str]:
    if tool.output.from_step < 0 or tool.output.from_step >= len(tool.steps):
        return [f"{tool.name} output step is outside the tool"]
    endpoint_id = tool.steps[tool.output.from_step].endpoint_id
    endpoint = _endpoint(spec, endpoint_id)
    if endpoint is None:
        return []
    try:
        expected = projected_schema(success_schema(spec, endpoint), tool.output.fields)
    except ValueError as exc:
        return [str(exc)]
    if not _same_schema(tool.output.schema_, expected):
        return [f"{tool.name} output schema was not copied from the endpoint"]
    return []


def _example_errors(tool: Tool) -> list[str]:
    errors: list[str] = []
    names = {item.name for item in tool.inputs}
    for name in tool.verify.example_args:
        if name not in names:
            errors.append(f"example {name} is not an input")
    for item in tool.inputs:
        if item.name not in tool.verify.example_args:
            errors.append(f"example {item.name} is missing")
            continue
        if not _accepts(item.schema_, tool.verify.example_args[item.name]):
            errors.append(f"example {item.name} does not match its schema")
    return errors


def _bind_errors(spec: ApiSpec, tool: Tool, step: Step, endpoint: Endpoint) -> list[str]:
    errors: list[str] = []
    index = tool.steps.index(step)
    inputs = {item.name for item in tool.inputs}
    for bind in step.bind:
        input_match = _INPUT_REF.fullmatch(bind.ref)
        step_match = _STEP_REF.fullmatch(bind.ref)
        if input_match:
            if input_match.group(1) not in inputs:
                errors.append(f"bind {bind.ref} does not match an input")
            continue
        if step_match is None:
            errors.append(f"bind {bind.ref} is not allowed")
            continue
        source = int(step_match.group(1))
        if source >= index:
            errors.append(f"bind {bind.ref} does not point at an earlier step")
            continue
        source_endpoint = _endpoint(spec, tool.steps[source].endpoint_id)
        if source_endpoint is None:
            errors.append(f"bind {bind.ref} does not point at an earlier step")
            continue
        if not _response_path_exists(spec, source_endpoint, step_match.group(2)):
            errors.append(f"bind {bind.ref} does not match a response field")
        if not _target_exists(spec, endpoint.id, bind.param, bind.in_):
            errors.append(f"bind {bind.param} is not a parameter of {endpoint.id}")
    return errors


def _coverage_errors(spec: ApiSpec, tool: Tool, step: Step, endpoint: Endpoint) -> list[str]:
    covered = _covered(tool, step)
    errors: list[str] = []
    for parameter in endpoint.parameters:
        if parameter.required and (parameter.location, parameter.name) not in covered:
            errors.append(f"required parameter {parameter.name} is not covered")
    body = endpoint.request_body
    if body is None or not body.required:
        return errors
    properties = _body_properties(spec, endpoint)
    required = [
        name for name in _required_names(body_schema(spec, endpoint) or {}) if name in properties
    ]
    if not required:
        if not any(location == "body" for location, _name in covered):
            errors.append(f"required body of {endpoint.id} is not covered")
        return errors
    if ("body", "$body") in covered:
        return errors
    for name in required:
        if ("body", name) not in covered:
            errors.append(f"required body property {name} is not covered")
    return errors


def _covered(tool: Tool, step: Step) -> set[tuple[str, str]]:
    covered: set[tuple[str, str]] = set()
    for item in tool.inputs:
        if item.maps_to.endpoint_id == step.endpoint_id:
            covered.add((item.maps_to.in_, item.maps_to.param))
    for fixed in tool.fixed:
        if fixed.endpoint_id == step.endpoint_id:
            covered.add((fixed.in_, fixed.param))
    for bind in step.bind:
        covered.add((bind.in_, bind.param))
    return covered


def _referenced_inputs(tool: Tool) -> set[str]:
    found: set[str] = set()
    for step in tool.steps:
        for bind in step.bind:
            match = _INPUT_REF.fullmatch(bind.ref)
            if match:
                found.add(match.group(1))
    return found


def _input_schema(spec: ApiSpec, raw: InputDraft, steps: list[Step]) -> JsonSchema | None:
    if raw.endpoint_id and _target_exists(
        spec, raw.endpoint_id, raw.param, parse_location(raw.in_) or "query"
    ):
        location = parse_location(raw.in_)
        if location is None:
            return None
        return _copied_target(spec, raw.endpoint_id, raw.param, location)
    tool_inputs = {raw.name}
    for step in steps:
        for bind in step.bind:
            match = _INPUT_REF.fullmatch(bind.ref)
            if match and match.group(1) in tool_inputs:
                return _copied_target(spec, step.endpoint_id, bind.param, bind.in_)
    return None


def _input_schema_for(
    spec: ApiSpec,
    endpoint_id: str,
    param: str,
    location: ParameterIn,
    tool: Tool,
) -> JsonSchema | None:
    if _target_exists(spec, endpoint_id, param, location):
        return _copied_target(spec, endpoint_id, param, location)
    for step in tool.steps:
        for bind in step.bind:
            match = _INPUT_REF.fullmatch(bind.ref)
            if match is None:
                continue
            return _copied_target(spec, step.endpoint_id, bind.param, bind.in_)
    return None


def _copied_target(
    spec: ApiSpec, endpoint_id: str, param: str, location: ParameterIn
) -> JsonSchema | None:
    endpoint = _endpoint(spec, endpoint_id)
    if endpoint is None:
        return None
    if location == "body":
        schema = body_schema(spec, endpoint)
        if schema is None:
            return None
        if param == "$body":
            return schema
        properties = _body_properties(spec, endpoint)
        child = properties.get(param)
        if child is None:
            return None
        return _attach_defs(_detach(child), _defs_of(schema))
    parameter = _parameter(endpoint, param, location)
    if parameter is None:
        return None
    return bundle_schema(spec, parameter.schema_)


def _target_exists(spec: ApiSpec, endpoint_id: str, param: str, location: ParameterIn) -> bool:
    endpoint = _endpoint(spec, endpoint_id)
    if endpoint is None:
        return False
    if location == "body":
        if endpoint.request_body is None:
            return False
        if param == "$body":
            return True
        return param in _body_properties(spec, endpoint)
    return _parameter(endpoint, param, location) is not None


def _draft_output(
    spec: ApiSpec,
    steps: list[Step],
    index: int,
    fields: list[str],
) -> tuple[JsonSchema | None, str | None]:
    if index < 0 or index >= len(steps):
        return None, "output step is outside the tool"
    endpoint = _endpoint(spec, steps[index].endpoint_id)
    if endpoint is None:
        return None, f"unknown endpoint {steps[index].endpoint_id}"
    try:
        return projected_schema(success_schema(spec, endpoint), fields), None
    except ValueError as exc:
        return None, str(exc)


def _draft_examples(examples: list[ExampleDraft]) -> tuple[JsonObject, str | None]:
    payload: JsonObject = {}
    for item in examples:
        value, error = parse_scalar(item.value_json)
        if error:
            return {}, error
        payload[item.name] = value
    return payload, None


def parse_scalar(text: str) -> tuple[JsonValue, str | None]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None, "fixed value is not JSON"
    try:
        parsed = expect_json(loaded)
    except TypeError:
        return None, "fixed value must be a scalar"
    if isinstance(parsed, float) and not math.isfinite(parsed):
        return None, "fixed value is not a finite number"
    if parsed is None or isinstance(parsed, (str, bool, int)):
        return parsed, None
    if isinstance(parsed, float):
        return parsed, None
    return None, "fixed value must be a scalar"


def success_schema(spec: ApiSpec, endpoint: Endpoint) -> JsonSchema:
    chosen: tuple[int, JsonSchema] | None = None
    for code, response in endpoint.responses.items():
        if response.schema_ is None or not code.isdigit():
            continue
        status = int(code)
        if status < 200 or status > 299:
            continue
        if chosen is None or status < chosen[0]:
            chosen = (status, response.schema_)
    if chosen is None:
        return {"type": "object", "additionalProperties": False}
    return bundle_schema(spec, chosen[1])


def body_schema(spec: ApiSpec, endpoint: Endpoint) -> JsonSchema | None:
    if endpoint.request_body is None:
        return None
    return bundle_schema(spec, endpoint.request_body.schema_)


def projected_schema(schema: JsonSchema, fields: list[str]) -> JsonSchema:
    if isinstance(schema, bool) or not fields:
        return schema
    parsed = [_parse_field(field) for field in fields]
    defs = _defs_of(schema)
    projected = _project_object(schema, parsed, defs)
    return _attach_defs(projected, defs)


def _project_object(
    schema: JsonSchema,
    paths: list[list[tuple[str, bool]]],
    defs: JsonObject,
) -> JsonSchema:
    properties = _properties(schema, defs)
    required = set(_required_names(schema, defs))
    grouped: dict[tuple[str, bool], list[list[tuple[str, bool]]]] = {}
    order: list[tuple[str, bool]] = []
    for path in paths:
        if not path:
            raise ValueError("bad output field")
        head = path[0]
        if head not in grouped:
            order.append(head)
            grouped[head] = []
        if path[1:]:
            grouped[head].append(path[1:])
    projected: JsonObject = {}
    new_required: list[str] = []
    for head in order:
        name, is_array = head
        if name not in properties:
            raise ValueError(f"missing output field {name}")
        child = properties[name]
        rests = grouped[head]
        if is_array:
            items = _items(child, defs)
            if items is None:
                raise ValueError(f"missing output field {name}")
            projected_items = _project_object(items, rests, defs) if rests else _detach(items)
            value: JsonSchema = {"type": "array", "items": projected_items}
        elif rests:
            value = _project_object(child, rests, defs)
        else:
            value = _detach(child)
        projected[name] = value
        if name in required:
            new_required.append(name)
    result: JsonObject = {
        "additionalProperties": False,
        "properties": projected,
        "type": "object",
    }
    if new_required:
        required_values: list[JsonValue] = []
        required_values.extend(new_required)
        result["required"] = required_values
    return result


def _parse_field(path: str) -> list[tuple[str, bool]]:
    if not path or ".." in path or path.startswith(".") or path.endswith("."):
        raise ValueError(f"bad output field {path}")
    segments: list[tuple[str, bool]] = []
    for piece in path.split("."):
        array = piece.endswith("[]")
        name = piece[:-2] if array else piece
        if _FIELD_NAME.fullmatch(name) is None:
            raise ValueError(f"bad output field {path}")
        segments.append((name, array))
    return segments


def _response_path_exists(spec: ApiSpec, endpoint: Endpoint, suffix: str) -> bool:
    schema = success_schema(spec, endpoint)
    defs = _defs_of(schema)
    node: JsonSchema = schema
    index = 0
    while index < len(suffix):
        if suffix.startswith("[]", index):
            items = _items(node, defs)
            if items is None:
                return False
            node = items
            index += 2
            continue
        if suffix[index] != ".":
            return False
        index += 1
        match = _FIELD_NAME.match(suffix, index)
        if match is None:
            return False
        properties = _properties(node, defs)
        name = match.group(0)
        if name not in properties:
            return False
        node = properties[name]
        index = match.end()
    return True


def _properties(schema: JsonSchema, defs: JsonObject) -> dict[str, JsonSchema]:
    resolved = _deref(schema, defs, set())
    if not isinstance(resolved, dict):
        return {}
    merged: dict[str, JsonSchema] = {}
    raw = resolved.get("properties")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (dict, bool)):
                merged[key] = value
    branches = resolved.get("allOf")
    if isinstance(branches, list):
        for branch in branches:
            if isinstance(branch, (dict, bool)):
                merged.update(_properties(branch, defs))
    return merged


def _required_names(schema: JsonSchema, defs: JsonObject | None = None) -> list[str]:
    store = defs if defs is not None else _defs_of(schema)
    resolved = _deref(schema, store, set())
    if not isinstance(resolved, dict):
        return []
    found: list[str] = []
    raw = resolved.get("required")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item not in found:
                found.append(item)
    branches = resolved.get("allOf")
    if isinstance(branches, list):
        for branch in branches:
            if isinstance(branch, (dict, bool)):
                for item in _required_names(branch, store):
                    if item not in found:
                        found.append(item)
    return found


def _items(schema: JsonSchema, defs: JsonObject) -> JsonSchema | None:
    resolved = _deref(schema, defs, set())
    if not isinstance(resolved, dict):
        return None
    items = resolved.get("items")
    if isinstance(items, (dict, bool)):
        return items
    return None


def _deref(schema: JsonSchema, defs: JsonObject, seen: set[str]) -> JsonSchema:
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith(_DEFS):
        return schema
    name = ref[len(_DEFS) :]
    if name in seen:
        return schema
    target = defs.get(name)
    if not isinstance(target, (dict, bool)):
        return schema
    return _deref(target, defs, seen | {name})


def _defs_of(schema: JsonSchema) -> JsonObject:
    if isinstance(schema, dict):
        raw = schema.get("$defs")
        if isinstance(raw, dict):
            return raw
    return {}


def _detach(schema: JsonSchema) -> JsonSchema:
    copied = _copy_schema(schema)
    if isinstance(copied, dict):
        return {key: value for key, value in copied.items() if key != "$defs"}
    return copied


def _attach_defs(schema: JsonSchema, defs: JsonObject) -> JsonSchema:
    if not isinstance(schema, dict):
        return schema
    used: list[str] = []
    seen: set[str] = set()

    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(_DEFS):
                name = ref[len(_DEFS) :]
                if name not in seen:
                    seen.add(name)
                    used.append(name)
                    target = defs.get(name)
                    if isinstance(target, (dict, bool)):
                        walk(target)
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    if not used:
        return schema
    attached = dict(schema)
    bundled: JsonObject = {}
    for name in used:
        target = defs.get(name)
        if isinstance(target, (dict, bool)):
            bundled[name] = target
    attached["$defs"] = bundled
    return attached


def _copy_schema(schema: JsonSchema) -> JsonSchema:
    parsed = expect_json(json.loads(json.dumps(schema)))
    if isinstance(parsed, (dict, bool)):
        return parsed
    raise TypeError("schema copy failed")


def _same_schema(left: JsonSchema, right: JsonSchema) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _accepts(schema: JsonSchema, value: JsonValue) -> bool:
    if schema is True:
        return True
    if schema is False:
        return False
    try:
        Draft202012Validator(schema).validate(value)
    except (ValidationError, SchemaError):
        return False
    return True


def _heuristic_tool(
    spec: ApiSpec,
    endpoint: Endpoint,
    *,
    name: str,
    action: str,
    fallback: bool,
) -> Tool:
    inputs: list[ToolInput] = []
    fixed: list[FixedParam] = []
    for parameter in endpoint.parameters:
        hidden = _plumbing_fixed(endpoint, parameter)
        if hidden is not None:
            fixed.append(hidden)
            continue
        inputs.append(
            ToolInput(
                name=parameter.name,
                schema_=bundle_schema(spec, parameter.schema_),
                description=parameter.description or parameter.name,
                required=parameter.required,
                maps_to=MapsTo(
                    endpoint_id=endpoint.id, param=parameter.name, in_=parameter.location
                ),
            )
        )
    inputs.extend(_body_inputs(spec, endpoint))
    inputs = sorted(inputs, key=lambda item: (not item.required, item.name))
    schema = success_schema(spec, endpoint)
    fields = list(_properties(schema, _defs_of(schema)))
    output = projected_schema(schema, fields)
    summary = endpoint.summary or endpoint.id
    description = (
        f"{summary}. Use this when that is what the user asked for. "
        "Do not use it for a different resource. "
        f"It returns the {summary} result."
    )
    if action == "list":
        description += " Returns the first page."
    rationale = f"Heuristic {action} for {endpoint.id}."
    if fallback:
        rationale = f"Heuristic fallback: {rationale}"
    title = " ".join(part.capitalize() for part in name.split("_"))
    return Tool(
        name=name,
        title=title,
        description=description,
        intent=summary,
        annotations=annotations_for([endpoint.method]),
        inputs=inputs,
        fixed=fixed,
        steps=[Step(endpoint_id=endpoint.id, bind=[])],
        output=ToolOutput(from_step=0, fields=fields, schema_=output),
        verify=Verify(example_args=_examples(inputs), depends_on=[]),
        rationale=rationale,
    )


def _body_inputs(spec: ApiSpec, endpoint: Endpoint) -> list[ToolInput]:
    schema = body_schema(spec, endpoint)
    if schema is None or endpoint.request_body is None:
        return []
    properties = _body_properties(spec, endpoint)
    if not properties:
        return [
            ToolInput(
                name="body",
                schema_=schema,
                description=endpoint.summary or "Request body",
                required=endpoint.request_body.required,
                maps_to=MapsTo(endpoint_id=endpoint.id, param="$body", in_="body"),
            )
        ]
    required = set(_required_names(schema))
    if endpoint.request_body.required and not required and properties:
        required.add(next(iter(properties)))
    inputs: list[ToolInput] = []
    for name, child in properties.items():
        inputs.append(
            ToolInput(
                name=name,
                schema_=_attach_defs(_detach(child), _defs_of(schema)),
                description=name,
                required=name in required,
                maps_to=MapsTo(endpoint_id=endpoint.id, param=name, in_="body"),
            )
        )
    return inputs


def _examples(inputs: list[ToolInput]) -> JsonObject:
    payload: JsonObject = {}
    for item in inputs:
        payload[item.name] = _example_for(item.schema_)
    return payload


def _example_for(schema: JsonSchema) -> JsonValue:
    defs = _defs_of(schema)
    resolved = _deref(schema, defs, set())
    if not isinstance(resolved, dict):
        return "example"
    if "const" in resolved:
        constant = resolved["const"]
        if isinstance(constant, (str, bool, int, float)) or constant is None:
            return constant
    enum = resolved.get("enum")
    if isinstance(enum, list):
        for item in enum:
            if isinstance(item, (str, bool, int, float)) or item is None:
                return item
    if "default" in resolved:
        default = resolved["default"]
        if isinstance(default, (str, bool, int, float)) or default is None:
            return default
    kind = resolved.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "integer":
        minimum = resolved.get("minimum")
        if isinstance(minimum, int) and not isinstance(minimum, bool):
            return minimum
        return 1
    if kind == "number":
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object" or "properties" in resolved:
        return {}
    return "example"


def _plumbing_fixed(endpoint: Endpoint, parameter: Parameter) -> FixedParam | None:
    if not _is_plumbing(parameter):
        return None
    if parameter.name.lower() == "idempotency-key":
        return FixedParam(
            endpoint_id=endpoint.id,
            param=parameter.name,
            in_=parameter.location,
            value="$generated",
            reason="generated-per-call",
        )
    default = _schema_default(parameter.schema_)
    if default is not None or _has_default(parameter.schema_):
        return FixedParam(
            endpoint_id=endpoint.id,
            param=parameter.name,
            in_=parameter.location,
            value=default,
            reason="hidden plumbing",
        )
    if not parameter.required:
        reason = "first page" if parameter.name.lower() in _PAGINATION else "hidden plumbing"
        return FixedParam(
            endpoint_id=endpoint.id,
            param=parameter.name,
            in_=parameter.location,
            value=None,
            reason=reason,
        )
    return None


def _is_plumbing(parameter: Parameter) -> bool:
    name = parameter.name.lower()
    if name in _PAGINATION or name == "idempotency-key":
        return True
    if parameter.location != "header":
        return False
    return name in _HEADER_PLUMBING or "api-version" in name


def _has_default(schema: JsonSchema) -> bool:
    return isinstance(schema, dict) and "default" in schema


def _schema_default(schema: JsonSchema) -> JsonValue | None:
    if not isinstance(schema, dict) or "default" not in schema:
        return None
    value = schema["default"]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return None


def _heuristic_name(endpoint: Endpoint) -> tuple[str, str]:
    parts = _static_parts(endpoint.path)
    resource = parts[-1] if parts else "resource"
    ends_with_param = endpoint.path.rstrip("/").split("/")[-1].startswith("{")
    method = endpoint.method.upper()
    if method == "GET" and not ends_with_param:
        return f"list_{resource}", "list"
    if method == "GET" and ends_with_param:
        return f"get_{_singular(resource)}", "get"
    if method in {"PUT", "PATCH"} and ends_with_param:
        return f"update_{_singular(resource)}", "update"
    if method == "DELETE" and ends_with_param:
        return f"delete_{_singular(resource)}", "delete"
    if method == "POST" and not ends_with_param and _is_plural(resource):
        return f"create_{_singular(resource)}", "create"
    if len(parts) >= 2 and not ends_with_param:
        action = parts[-1].replace("-", "_")
        return f"{action}_{_singular(parts[-2])}", action
    if method == "POST":
        return f"create_{_singular(resource)}", "create"
    return f"{method.lower()}_{_singular(resource)}", method.lower()


def _with_parent(name: str, parent: str) -> str:
    head, separator, tail = name.partition("_")
    if not separator:
        return f"{parent}_{name}"
    return f"{head}_{parent}_{tail}"


def _parent_segment(path: str) -> str | None:
    parts = _static_parts(path)
    if len(parts) < 2:
        return None
    return _singular(parts[-2])


def _static_parts(path: str) -> list[str]:
    return [part for part in path.strip("/").split("/") if part and not part.startswith("{")]


def _is_plural(name: str) -> bool:
    word = name.replace("-", "_")
    if word.endswith("ies") and len(word) > 3:
        return True
    long_enough = len(word) > 1
    plural_s = word.endswith("s") and not word.endswith("ss") and not word.endswith("us")
    return plural_s and long_enough


def _singular(name: str) -> str:
    word = name.replace("-", "_")
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith(("ss", "us")) and len(word) > 1:
        return word[:-1]
    return word


def _apply_exclusions(
    tools: list[Tool],
    excluded: list[Excluded],
    overrides: Overrides,
    by_id: dict[str, Endpoint],
    warnings: list[str],
) -> tuple[list[Tool], list[Excluded]]:
    removed: dict[str, Excluded] = {}
    for item in overrides.exclude_endpoints:
        if item.endpoint_id not in by_id:
            warnings.append(f"stale override: exclude {item.endpoint_id}")
            continue
        reason = parse_reason(item.reason)
        if reason is None:
            warnings.append(f"stale override: exclude {item.endpoint_id}")
            continue
        note = item.note.strip() or _NOTES[reason]
        removed[item.endpoint_id] = Excluded(endpoint_id=item.endpoint_id, reason=reason, note=note)
    if not removed:
        return tools, excluded
    kept_tools: list[Tool] = []
    for tool in tools:
        steps = [step for step in tool.steps if step.endpoint_id not in removed]
        if steps and len(steps) != len(tool.steps):
            kept_tools.append(tool.model_copy(update={"steps": steps}))
        elif steps:
            kept_tools.append(tool)
    kept_excluded = [item for item in excluded if item.endpoint_id not in removed]
    kept_excluded.extend(removed[endpoint_id] for endpoint_id in sorted(removed))
    return kept_tools, kept_excluded


def _apply_inclusions(
    tools: list[Tool],
    excluded: list[Excluded],
    overrides: Overrides,
    spec: ApiSpec,
    namespace: str,
    warnings: list[str],
) -> tuple[list[Tool], list[Excluded]]:
    present = {step.endpoint_id for tool in tools for step in tool.steps}
    known = {endpoint.id for endpoint in spec.endpoints}
    wanted: list[str] = []
    for endpoint_id in overrides.include_endpoints:
        if endpoint_id not in known:
            warnings.append(f"stale override: include {endpoint_id}")
            continue
        if endpoint_id in present or endpoint_id in wanted:
            continue
        wanted.append(endpoint_id)
    if not wanted:
        return tools, excluded
    reserved = {tool.name for tool in tools}
    added, still_out = heuristic_tools(
        spec,
        wanted,
        namespace=namespace,
        include_tags=("admin", "internal", "deprecated", "webhook-management", "destructive-bulk"),
        reserved_names=reserved,
    )
    added_ids = {step.endpoint_id for tool in added for step in tool.steps}
    still_ids = {item.endpoint_id for item in still_out}
    kept_excluded = [
        item
        for item in excluded
        if item.endpoint_id not in added_ids and item.endpoint_id not in still_ids
    ]
    kept_excluded.extend(still_out)
    return [*tools, *added], kept_excluded


def _apply_renames(
    tools: list[Tool],
    overrides: Overrides,
    namespace: str,
    warnings: list[str],
) -> list[Tool]:
    by_name = {tool.name: tool for tool in tools}
    planned: dict[str, str] = {}
    claimed = set(by_name)
    for old, new in overrides.renames.items():
        if old not in by_name:
            warnings.append(f"stale override: rename {old}")
            continue
        problem = name_error(new, namespace=namespace)
        if problem or new in claimed:
            warnings.append(f"stale override: rename {old}")
            continue
        claimed.remove(old)
        claimed.add(new)
        planned[old] = new
    if not planned:
        return tools
    renamed: list[Tool] = []
    for tool in tools:
        name = planned.get(tool.name, tool.name)
        depends = [planned.get(item, item) for item in tool.verify.depends_on]
        renamed.append(
            tool.model_copy(
                update={
                    "name": name,
                    "verify": tool.verify.model_copy(update={"depends_on": depends}),
                }
            )
        )
    return renamed


def _apply_descriptions(tools: list[Tool], overrides: Overrides, warnings: list[str]) -> list[Tool]:
    by_name = {tool.name: tool for tool in tools}
    for name in overrides.descriptions:
        if name not in by_name:
            warnings.append(f"stale override: description {name}")
    return [
        tool.model_copy(
            update={"description": overrides.descriptions.get(tool.name, tool.description)}
        )
        for tool in tools
    ]


def _apply_fixed(tools: list[Tool], overrides: Overrides, warnings: list[str]) -> list[Tool]:
    grouped: dict[str, list[FixedOverride]] = {}
    by_name = {tool.name: tool for tool in tools}
    for item in overrides.fixed:
        if item.tool not in by_name:
            warnings.append(f"stale override: fixed {item.tool}")
            continue
        grouped.setdefault(item.tool, []).append(item)
    if not grouped:
        return tools
    updated: list[Tool] = []
    for tool in tools:
        items = grouped.get(tool.name)
        if not items:
            updated.append(tool)
            continue
        fixed = list(tool.fixed)
        inputs = list(tool.inputs)
        examples = dict(tool.verify.example_args)
        for item in items:
            replacement = FixedParam(
                endpoint_id=item.endpoint_id,
                param=item.param,
                in_=item.in_,
                value=item.value,
                reason=item.reason,
            )
            fixed = [
                current
                for current in fixed
                if not (
                    current.endpoint_id == item.endpoint_id
                    and current.param == item.param
                    and current.in_ == item.in_
                )
            ]
            fixed.append(replacement)
            removed_inputs = [
                current.name
                for current in inputs
                if current.maps_to.endpoint_id == item.endpoint_id
                and current.maps_to.param == item.param
                and current.maps_to.in_ == item.in_
            ]
            inputs = [current for current in inputs if current.name not in set(removed_inputs)]
            for name in removed_inputs:
                examples.pop(name, None)
        updated.append(
            tool.model_copy(
                update={
                    "fixed": fixed,
                    "inputs": inputs,
                    "verify": tool.verify.model_copy(update={"example_args": examples}),
                }
            )
        )
    return updated


def _apply_outputs(
    tools: list[Tool],
    overrides: Overrides,
    spec: ApiSpec,
    warnings: list[str],
) -> list[Tool]:
    by_name = {tool.name: tool for tool in tools}
    for name in overrides.outputs:
        if name not in by_name:
            warnings.append(f"stale override: output {name}")
    updated: list[Tool] = []
    for tool in tools:
        fields = overrides.outputs.get(tool.name)
        if fields is None:
            updated.append(tool)
            continue
        if tool.output.from_step >= len(tool.steps):
            warnings.append(f"stale override: output {tool.name}")
            updated.append(tool)
            continue
        endpoint = _endpoint(spec, tool.steps[tool.output.from_step].endpoint_id)
        if endpoint is None:
            warnings.append(f"stale override: output {tool.name}")
            updated.append(tool)
            continue
        try:
            schema = projected_schema(success_schema(spec, endpoint), fields)
        except ValueError:
            warnings.append(f"stale override: output {tool.name}")
            updated.append(tool)
            continue
        updated.append(
            tool.model_copy(
                update={
                    "output": ToolOutput(
                        from_step=tool.output.from_step, fields=list(fields), schema_=schema
                    )
                }
            )
        )
    return updated


def _has_cycle(tools: list[Tool]) -> bool:
    graph = {tool.name: tool.verify.depends_on for tool in tools}
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(name: str) -> bool:
        if name in visiting:
            return True
        if name in visited or name not in graph:
            return False
        visiting.add(name)
        for dep in graph[name]:
            if walk(dep):
                return True
        visiting.remove(name)
        visited.add(name)
        return False

    return any(walk(name) for name in graph)


def _endpoint(spec: ApiSpec, endpoint_id: str) -> Endpoint | None:
    for endpoint in spec.endpoints:
        if endpoint.id == endpoint_id:
            return endpoint
    return None


def _parameter(endpoint: Endpoint, name: str, location: str) -> Parameter | None:
    for parameter in endpoint.parameters:
        if parameter.name == name and parameter.location == location:
            return parameter
    return None


def _body_properties(spec: ApiSpec, endpoint: Endpoint) -> dict[str, JsonSchema]:
    schema = body_schema(spec, endpoint)
    if schema is None:
        return {}
    return _properties(schema, _defs_of(schema))


def _shield_doc(value: str) -> str:
    cleaned = value.replace("<<<UNTRUSTED DOC>>>", "[[[UNTRUSTED DOC]]]").replace(
        "<<<END UNTRUSTED DOC>>>",
        "[[[END UNTRUSTED DOC]]]",
    )
    return f"<<<UNTRUSTED DOC>>>\n{cleaned}\n<<<END UNTRUSTED DOC>>>"


def _yaml_object(text: str) -> JsonObject:
    loaded = yaml.safe_load(text)
    return _object(loaded)


def _object(value: object) -> JsonObject:
    parsed = expect_json(value)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("YAML must be an object")


def _is_admin(endpoint: Endpoint) -> bool:
    return "admin" in _tags(endpoint) or "admin" in _segments(endpoint.path)


def _is_internal(endpoint: Endpoint) -> bool:
    return "internal" in _tags(endpoint) or "internal" in _segments(endpoint.path)


def _is_webhook(endpoint: Endpoint) -> bool:
    return any("webhook" in tag for tag in _tags(endpoint)) or any(
        "webhook" in segment for segment in _segments(endpoint.path)
    )


def _is_bulk(endpoint: Endpoint) -> bool:
    text = " ".join(
        (
            endpoint.path,
            " ".join(endpoint.tags or []),
            endpoint.operation_id or "",
            endpoint.summary or "",
        )
    )
    if _BULK.search(text):
        return True
    return endpoint.method.upper() == "DELETE" and "{" not in endpoint.path


def _waived(reason: ExclusionReason, endpoint: Endpoint, include_tags: set[str]) -> bool:
    if reason in include_tags:
        return True
    causes = {tag for tag in _tags(endpoint) if reason == "admin" and tag == "admin"}
    causes.update(tag for tag in _tags(endpoint) if reason == "internal" and tag == "internal")
    if reason == "webhook-management":
        causes.update(tag for tag in _tags(endpoint) if "webhook" in tag)
    if reason == "destructive-bulk":
        causes.update(tag for tag in _tags(endpoint) if _BULK.search(tag))
    return bool(causes & include_tags)


def _tags(endpoint: Endpoint) -> set[str]:
    return {tag.lower() for tag in endpoint.tags or []}


def _segments(path: str) -> set[str]:
    return {part.lower() for part in path.split("/") if part and not part.startswith("{")}


def _auth_supported(endpoint: Endpoint, schemes: dict[str, SecurityScheme]) -> bool:
    if not endpoint.security:
        return True
    for requirement in endpoint.security:
        if all(name in schemes and _scheme_supported(schemes[name]) for name in requirement):
            return True
    return False


def _scheme_supported(scheme: SecurityScheme) -> bool:
    if scheme.type == "apiKey":
        return True
    return scheme.type == "http" and (scheme.scheme or "").lower() == "bearer"
