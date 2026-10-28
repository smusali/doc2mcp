from __future__ import annotations

from collections import Counter

from mcp4api.curate.models import (
    _NOTES,
    Excluded,
    FixedOverride,
    FixedParam,
    Manifest,
    MapsTo,
    Overrides,
    Step,
    Tool,
    ToolInput,
    ToolOutput,
    Verify,
    annotations_for,
    apply_namespace,
    name_error,
    parse_reason,
)
from mcp4api.curate.rules import (
    _attach_defs,
    _body_properties,
    _defs_of,
    _deref,
    _detach,
    _endpoint,
    _properties,
    _required_names,
    body_schema,
    forced_exclusions,
    projected_schema,
    success_schema,
)
from mcp4api.model import (
    ApiSpec,
    Endpoint,
    JsonObject,
    JsonSchema,
    JsonValue,
    Parameter,
    bundle_schema,
)

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
