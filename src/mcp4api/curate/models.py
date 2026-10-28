from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field

from mcp4api.model import (
    ApiSpec,
    JsonObject,
    JsonObjectField,
    JsonSchema,
    JsonSchemaField,
    JsonValue,
    JsonValueField,
    Parameter,
    bundle_schema,
    expect_json,
)

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


class CurationMode(StrEnum):
    AGENT = "agent"
    HEURISTIC = "heuristic"
    MANUAL = "manual"


ParameterIn: TypeAlias = Literal["path", "query", "header", "cookie", "body"]


_FROZEN = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")


_DECISION = ConfigDict(extra="ignore")


_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


_NAMESPACE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


_VERSION_TOKEN = re.compile(r"^v\d+$", re.IGNORECASE)


_HTTP = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})


_IDEMPOTENT = _SAFE | {"PUT", "DELETE"}


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
    input_hash: str
    usage: Usage
    fallback_tools: list[str]


class Manifest(BaseModel):
    model_config = _FROZEN

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
    prompt: str,
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
            prompt.encode("utf-8"),
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


def bundle_parameter(spec: ApiSpec, parameter: Parameter) -> JsonSchema:
    return bundle_schema(spec, parameter.schema_)


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


def invalid_exclusions(endpoint_ids: list[str], note: str) -> list[Excluded]:
    text = note.strip() or _NOTES["invalid-curation"]
    return [
        Excluded(endpoint_id=endpoint_id, reason="invalid-curation", note=text)
        for endpoint_id in endpoint_ids
    ]


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


def _yaml_object(text: str) -> JsonObject:
    loaded = yaml.safe_load(text)
    return _object(loaded)


def _object(value: object) -> JsonObject:
    parsed = expect_json(value)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("YAML must be an object")
