from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from mcp4api.curate.models import (
    _NOTES,
    Annotations,
    Bind,
    DetailDecision,
    DetailResult,
    ExampleDraft,
    Excluded,
    ExclusionReason,
    FixedParam,
    InputDraft,
    Manifest,
    MapsTo,
    ParameterIn,
    PlanDecision,
    PlanResult,
    PlanTool,
    Step,
    Tool,
    ToolDraft,
    ToolInput,
    ToolOutput,
    Verify,
    annotations_for,
    apply_namespace,
    name_error,
    parse_location,
    parse_reason,
    parse_scalar,
)
from mcp4api.model import (
    ApiSpec,
    Endpoint,
    JsonObject,
    JsonSchema,
    JsonValue,
    Parameter,
    SecurityScheme,
    bundle_schema,
    expect_json,
)

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_INPUT_REF = re.compile(r"^\$input\.([A-Za-z_][A-Za-z0-9_]*)$")


_STEP_REF = re.compile(r"^\$steps\[(\d+)\]\.body((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\])*)$")


_BULK = re.compile(r"bulk|batch|purge", re.IGNORECASE)


_DEFS = "#/$defs/"


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
