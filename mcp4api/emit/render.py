from __future__ import annotations

import json
import keyword
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from jinja2 import Environment, PackageLoader, StrictUndefined, TemplateError, UndefinedError

from mcp4api import __version__
from mcp4api.curate.manifest import Manifest, Tool
from mcp4api.model import ApiSpec, Endpoint, JsonObject, JsonSchema, JsonValue, SecurityScheme

_INPUT_REF = re.compile(r"^\$input\.([A-Za-z_][A-Za-z0-9_]*)$")
_STEP_REF = re.compile(r"^\$steps\[(\d+)\]\.body((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\])*)$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION = re.compile(
    r"^(?:[0-9]+!)?"
    r"[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?"
    r"(?:\.dev[0-9]+)?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?$",
    re.IGNORECASE,
)
_DOC_KEYS = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "contentEncoding",
        "contentMediaType",
        "default",
        "deprecated",
        "description",
        "discriminator",
        "example",
        "examples",
        "externalDocs",
        "format",
        "readOnly",
        "title",
        "writeOnly",
        "xml",
    }
)
_CONSTRAINT_KEYS = frozenset(
    {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
    }
)
_CORE_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "items",
        "nullable",
        "properties",
        "required",
        "type",
    }
)
_LINE = 100
_RESERVED_MODELS = frozenset({"Annotated", "BaseModel", "ConfigDict", "Field", "Literal"})


class EmitError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _SchemaProblem(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Rendered:
    files: dict[str, str]
    header: str


@dataclass(frozen=True)
class _Type:
    annotation: str
    nullable: bool
    constraints: str
    model: str | None


@dataclass(frozen=True)
class _Const:
    name: str
    literal: str
    reason: str


class _Flags:
    def __init__(self) -> None:
        self.annotated = False
        self.literal = False
        self.field = False
        self.adapter = False
        self.pick = False
        self.api_key = False
        self.bearer = False
        self.keys: list[tuple[str, str]] = []


def render_files(spec: ApiSpec, manifest: Manifest, *, tools_sha256: str) -> Rendered:
    slug = _slug(manifest.api.title)
    module = f"{slug.replace('-', '_')}_mcp"
    env_name = slug.replace("-", "_").upper()
    program = f"{slug}-mcp"
    line1, line2 = _header_lines(spec.source.uri, spec.source.sha256, tools_sha256)
    catalog = _Catalog(_spec_defs(spec))
    endpoints = {endpoint.id: endpoint for endpoint in spec.endpoints}
    server, flags, annotations = _build_server(
        manifest,
        endpoints,
        spec.security_schemes,
        catalog,
    )
    base = _py(manifest.api.base_url)
    if len(f"_DEFAULT_BASE_URL = {base}") > _LINE:
        base = "(\n    " + _py(manifest.api.base_url) + "\n)"
    context = {"line1": line1, "line2": line2}
    environment = _environment()
    files = {
        "README.md": _render(
            environment,
            "README.md.j2",
            {
                **context,
                "title": manifest.api.title,
                "program": program,
                "env_rows": _env_rows(env_name, manifest.api.base_url, flags),
                "tools": [
                    {
                        "name": tool.name,
                        "title": tool.title,
                        "description": _flat(tool.description),
                    }
                    for tool in manifest.tools
                ],
                "claude_desktop": _claude(program, env_name, flags),
            },
        ),
        "pyproject.toml": _render(
            environment,
            "pyproject.toml.j2",
            {
                **context,
                "distribution": program,
                "version": _package_version(spec.version),
                "description": _toml(f"{manifest.api.title} MCP server"),
                "program": program,
                "module": module,
            },
        ),
        f"{module}/__init__.py": _render(environment, "file.py.j2", {**context, "body": ""}),
        f"{module}/__main__.py": _render(
            environment,
            "main.py.j2",
            {**context, "module": module, "program_literal": _py(program)},
        ),
        f"{module}/client.py": _render(
            environment,
            "client.py.j2",
            {
                **context,
                "default_base_url": base,
                "base_url_env": _py(f"{env_name}_BASE_URL"),
                "timeout_env": _py(f"{env_name}_TIMEOUT"),
                "has_api_key": flags.api_key,
                "api_key_env": _py(f"{env_name}_API_KEY"),
                "has_bearer": flags.bearer,
                "token_env": _py(f"{env_name}_TOKEN"),
                "has_pick": flags.pick,
                "secret_envs": _tuple(_secret_names(env_name, flags)),
            },
        ),
        f"{module}/models.py": _render(
            environment,
            "file.py.j2",
            {**context, "body": catalog.source().rstrip("\n")},
        ),
        f"{module}/server.py": _render(
            environment,
            "file.py.j2",
            {
                **context,
                "body": _server_text(
                    module, slug, manifest.api.title, flags, server, annotations, catalog
                ).rstrip("\n"),
            },
        ),
    }
    return Rendered(files=files, header=f"{line1}\n{line2}")


def _build_server(
    manifest: Manifest,
    endpoints: dict[str, Endpoint],
    schemes: dict[str, SecurityScheme],
    catalog: _Catalog,
) -> tuple[tuple[list[_Const], list[list[str]]], _Flags, list[str]]:
    flags = _Flags()
    constants: list[_Const] = []
    functions: list[list[str]] = []
    annotations: list[str] = []
    used_functions = {"mcp", "api"}
    for tool in manifest.tools:
        try:
            output = catalog.compile_output(tool.output.schema_, tool.name)
            compiled = {
                item.name: catalog.compile_input(item.schema_, tool.name, item.name)
                for item in tool.inputs
            }
        except _SchemaProblem as exc:
            raise EmitError(f"{tool.name} schema is not supported: {exc.detail}") from exc
        function_name = _unique(_ident(tool.name), used_functions)
        lines = _tool_lines(
            tool,
            endpoints,
            schemes,
            output,
            compiled,
            function_name,
            constants,
            flags,
        )
        functions.append(lines)
        annotations.append(output.annotation)
        annotations.extend(item.annotation for item in compiled.values())
    return (constants, functions), flags, annotations


def _server_text(
    module: str,
    slug: str,
    title: str,
    flags: _Flags,
    built: tuple[list[_Const], list[list[str]]],
    annotations: list[str],
    catalog: _Catalog,
) -> str:
    constants, functions = built
    text = "\n".join("\n".join(lines) for lines in functions)
    kept = [item for item in constants if re.search(rf"\b{re.escape(item.name)}\b", text)]
    models = _mentioned(annotations, catalog.classes)
    lines = ["from __future__ import annotations"]
    typing_names: list[str] = []
    if flags.annotated:
        typing_names.append("Annotated")
    if flags.literal:
        typing_names.append("Literal")
    if typing_names:
        lines.extend(["", f"from typing import {', '.join(typing_names)}"])
    lines.extend(["", "from mcp.server import MCPServer"])
    if functions:
        lines.append("from mcp.types import ToolAnnotations")
    pydantic: list[str] = []
    if flags.field:
        pydantic.append("Field")
    if flags.adapter:
        pydantic.append("TypeAdapter")
    if pydantic:
        lines.append(f"from pydantic import {', '.join(pydantic)}")
    if functions:
        lines.append("")
        lines.append(f"from {module}.client import ApiClient")
        if models:
            lines.extend(_from_import(f"{module}.models", sorted(models)))
    lines.append("")
    lines.extend(_assignment(slug, title))
    if functions:
        lines.append("api = ApiClient()")
    for const in kept:
        lines.extend(_const_lines(const))
    for function in functions:
        lines.extend(["", ""])
        lines.extend(function)
    return "\n".join(lines)


def _tool_lines(
    tool: Tool,
    endpoints: dict[str, Endpoint],
    schemes: dict[str, SecurityScheme],
    output: _Type,
    compiled: dict[str, _Type],
    function_name: str,
    constants: list[_Const],
    flags: _Flags,
) -> list[str]:
    if tool.output.from_step < 0 or tool.output.from_step >= len(tool.steps):
        raise EmitError(f"{tool.name} output step is outside the tool")
    for step in tool.steps:
        _known(endpoints, step.endpoint_id)
        _require_json(tool.name, endpoints[step.endpoint_id])
    for item in tool.inputs:
        _known(endpoints, item.maps_to.endpoint_id)
    for fixed in tool.fixed:
        _known(endpoints, fixed.endpoint_id)
    ordered = [item for item in tool.inputs if item.required]
    ordered.extend(item for item in tool.inputs if not item.required)
    params: list[str] = []
    param_names: dict[str, str] = {}
    used_params = {"api"}
    for item in ordered:
        ident = _unique(_ident(item.name), used_params)
        alias = item.name if ident != item.name else None
        param_names[item.name] = ident
        params.append(
            _parameter(
                ident, compiled[item.name], item.description, alias, optional=not item.required
            )
        )
        _note(compiled[item.name].annotation, flags)
    if params:
        flags.annotated = True
        flags.field = True
    _note(output.annotation, flags)
    const_for = _constants(tool, constants)
    referenced = _referenced(tool)
    step_names = _step_names(tool, endpoints, function_name, set(param_names.values()), referenced)
    calls: list[str] = []
    for index, step in enumerate(tool.steps):
        endpoint = endpoints[step.endpoint_id]
        auth = _auth_plan(endpoint, schemes)
        _note_auth(auth, flags)
        slots = _slots(tool, index, endpoint.id, param_names, const_for, step_names, flags)
        calls.extend(_request(tool.name, step_names[index], endpoint, slots, auth))
    lines = [
        *_decorator(tool, function_name),
        *_signature(function_name, params, output.annotation),
        *_docstring(tool.description),
        *calls,
    ]
    if calls:
        lines.append("")
    target = step_names[tool.output.from_step]
    if output.model is not None and not output.nullable:
        expr = f"{output.model}.model_validate({target})"
    else:
        flags.adapter = True
        expr = f"TypeAdapter({output.annotation}).validate_python({target})"
    lines.extend(_indent(_return(expr), 4))
    return lines


def _constants(tool: Tool, constants: list[_Const]) -> dict[tuple[str, str, str], str]:
    winning: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    order: list[tuple[str, str, str]] = []
    step_of: dict[str, int] = {}
    for index, step in enumerate(tool.steps):
        step_of.setdefault(step.endpoint_id, index)
    for fixed in tool.fixed:
        if fixed.endpoint_id not in step_of:
            continue
        key = (fixed.endpoint_id, fixed.in_, fixed.param)
        if key not in winning:
            order.append(key)
        try:
            literal = _fixed(fixed.value)
        except EmitError as exc:
            raise EmitError(f"{tool.name} {exc.message}") from exc
        winning[key] = (literal, fixed.reason, fixed.param)
    found: dict[tuple[str, str, str], str] = {}
    for saved in order:
        literal, reason, param = winning[saved]
        base = _upper_name(tool.name, param)
        found[saved] = _take_const(
            base,
            literal,
            reason,
            constants,
            tool.name,
            param,
            step_of[saved[0]],
        )
    return found


def _take_const(
    base: str,
    literal: str,
    reason: str,
    constants: list[_Const],
    tool: str,
    param: str,
    step_index: int,
) -> str:
    for item in constants:
        if item.name == base and item.literal == literal:
            return base
    name = base
    if any(item.name == name for item in constants):
        name = _upper_name(f"{tool}_step{step_index}", param)
        suffix = 2
        while any(item.name == name for item in constants):
            name = f"{name}_{suffix}"
            suffix += 1
    constants.append(_Const(name, literal, reason))
    return name


def _step_names(
    tool: Tool,
    endpoints: dict[str, Endpoint],
    function_name: str,
    params: set[str],
    referenced: set[int],
) -> list[str]:
    used = set(params)
    used.add("api")
    used.add(function_name)
    names: list[str] = []
    for index, step in enumerate(tool.steps):
        operation = endpoints[step.endpoint_id].operation_id
        raw = _snake(operation) if operation else f"step_{index}"
        base = _ident(raw or f"step_{index}")
        if base in used:
            base = _ident(f"{base}_response")
        if index not in referenced and not base.startswith("_"):
            base = f"_{base}"
        names.append(_unique(base, used))
    return names


def _referenced(tool: Tool) -> set[int]:
    found = {tool.output.from_step}
    for step in tool.steps:
        for bind in step.bind:
            match = _STEP_REF.match(bind.ref)
            if match:
                found.add(int(match.group(1)))
    return found


def _slots(
    tool: Tool,
    index: int,
    endpoint_id: str,
    param_names: dict[str, str],
    const_for: dict[tuple[str, str, str], str],
    step_names: list[str],
    flags: _Flags,
) -> list[tuple[str, str, str]]:
    order: list[tuple[str, str]] = []
    values: dict[tuple[str, str], str] = {}

    def put(location: str, name: str, expr: str) -> None:
        key = (location, name)
        if key not in values:
            order.append(key)
        values[key] = expr

    for item in tool.inputs:
        mapped = item.maps_to
        if mapped.endpoint_id == endpoint_id:
            put(mapped.in_, mapped.param, param_names[item.name])
    for key, expr in const_for.items():
        if key[0] == endpoint_id:
            put(key[1], key[2], expr)
    for bind in tool.steps[index].bind:
        put(bind.in_, bind.param, _bind_expr(tool, bind.ref, index, param_names, step_names, flags))
    return [(location, name, values[(location, name)]) for location, name in order]


def _bind_expr(
    tool: Tool,
    ref: str,
    index: int,
    param_names: dict[str, str],
    step_names: list[str],
    flags: _Flags,
) -> str:
    input_match = _INPUT_REF.match(ref)
    if input_match:
        source = input_match.group(1)
        if source not in param_names:
            raise EmitError(f"{tool.name} bind {ref} is not supported")
        return param_names[source]
    step_match = _STEP_REF.match(ref)
    if not step_match:
        raise EmitError(f"{tool.name} bind {ref} is not supported")
    bind_index = int(step_match.group(1))
    if bind_index < 0 or bind_index >= index or bind_index >= len(step_names):
        raise EmitError(f"{tool.name} bind {ref} is outside the tool")
    keys = [
        match.group(1) for match in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_]*)", step_match.group(2))
    ]
    var = step_names[bind_index]
    if not keys:
        return var
    flags.pick = True
    quoted = ", ".join(_py(key) for key in keys)
    return f"api.pick({var}, {quoted})"


def _request(
    tool_name: str,
    local: str,
    endpoint: Endpoint,
    slots: list[tuple[str, str, str]],
    auth: tuple[tuple[str, str, str], ...],
) -> list[str]:
    arguments = [_py(endpoint.method.upper()), _py(endpoint.path)]
    groups = {
        "path_params": [(name, expr) for location, name, expr in slots if location == "path"],
        "query": [(name, expr) for location, name, expr in slots if location == "query"],
        "headers": [(name, expr) for location, name, expr in slots if location == "header"],
        "cookies": [(name, expr) for location, name, expr in slots if location == "cookie"],
    }
    for key in ("path_params", "query", "headers", "cookies"):
        pairs = groups[key]
        if pairs:
            arguments.append(f"{key}={_mapping(pairs)}")
    whole = [expr for location, name, expr in slots if location == "body" and name == "$body"]
    fields = [
        (name, expr) for location, name, expr in slots if location == "body" and name != "$body"
    ]
    if whole and fields:
        raise EmitError(f"{tool_name} schema is not supported: mixed request body")
    if whole:
        arguments.append(f"body={whole[0]}")
    elif fields:
        arguments.append(f"body={_mapping(fields)}")
    if auth:
        arguments.append(f"auth={_auth_literal(auth)}")
    return _indent(_invoke(f"{local} = await api.request", arguments, indent=4), 4)


def _auth_plan(
    endpoint: Endpoint, schemes: dict[str, SecurityScheme]
) -> tuple[tuple[str, str, str], ...]:
    if not endpoint.security:
        return ()
    for requirement in endpoint.security:
        chosen: list[tuple[str, str, str]] = []
        supported = True
        for name in requirement:
            scheme = schemes.get(name)
            if scheme is None or not _scheme_ok(scheme):
                supported = False
                break
            chosen.append(_scheme_tuple(scheme))
        if supported:
            return tuple(chosen)
    raise EmitError(f"unsupported authentication for {endpoint.id}")


def _scheme_ok(scheme: SecurityScheme) -> bool:
    if scheme.type == "apiKey":
        return bool(scheme.name) and scheme.location in {"header", "query", "cookie"}
    return scheme.type == "http" and (scheme.scheme or "").lower() == "bearer"


def _scheme_tuple(scheme: SecurityScheme) -> tuple[str, str, str]:
    if scheme.type == "apiKey":
        return ("api_key", scheme.location or "", scheme.name or "")
    return ("bearer", "header", "Authorization")


def _note_auth(auth: tuple[tuple[str, str, str], ...], flags: _Flags) -> None:
    for kind, place, name in auth:
        if kind == "api_key":
            flags.api_key = True
            slot = (place, name)
            if slot not in flags.keys:
                flags.keys.append(slot)
        elif kind == "bearer":
            flags.bearer = True


def _decorator(tool: Tool, function_name: str) -> list[str]:
    lines = ["@mcp.tool("]
    if function_name != tool.name:
        lines.append(f"    name={_py(tool.name)},")
    lines.append(f"    title={_py(tool.title)},")
    lines.append("    annotations=ToolAnnotations(")
    lines.append(f"        read_only_hint={_py(tool.annotations.readOnlyHint)},")
    lines.append(f"        destructive_hint={_py(tool.annotations.destructiveHint)},")
    lines.append(f"        idempotent_hint={_py(tool.annotations.idempotentHint)},")
    lines.append(f"        open_world_hint={_py(tool.annotations.openWorldHint)},")
    lines.append("    ),")
    lines.append(")")
    return lines


def _signature(name: str, params: list[str], returns: str) -> list[str]:
    if not params:
        return [f"async def {name}() -> {returns}:"]
    one = f"async def {name}({', '.join(params)}) -> {returns}:"
    if "\n" not in one and len(one) <= _LINE:
        return [one]
    lines = [f"async def {name}("]
    for param in params:
        parts = param.split("\n")
        for index, part in enumerate(parts):
            suffix = "," if index == len(parts) - 1 else ""
            lines.append(f"    {part}{suffix}")
    lines.append(f") -> {returns}:")
    return lines


def _parameter(
    name: str,
    typ: _Type,
    description: str,
    alias: str | None,
    *,
    optional: bool,
) -> str:
    annotation = typ.annotation
    if optional and not typ.nullable and annotation != "None":
        annotation = f"{annotation} | None"
    parts = [f"description={_py(description)}"]
    if alias is not None:
        parts.append(f"alias={_py(alias)}")
    if typ.constraints:
        parts.append(typ.constraints)
    field_text = f"Field({', '.join(parts)})"
    default = " = None" if optional else ""
    one = f"{name}: Annotated[{annotation}, {field_text}]{default}"
    if len(one) + 5 <= _LINE:
        return one
    return "\n".join(
        [
            f"{name}: Annotated[",
            f"    {annotation},",
            f"    {field_text},",
            f"]{default}",
        ]
    )


def _docstring(text: str) -> list[str]:
    quote = '"""' if '"""' not in text else "'''"
    if quote in text:
        raise EmitError("description cannot be rendered")
    parts = text.replace("\\", "\\\\").split("\n")
    if len(parts) == 1:
        line = f"    {quote}{parts[0]}{quote}"
        if len(line) > _LINE:
            line += "  # noqa: E501"
        return [line]
    lines = [f"    {quote}{parts[0]}", *parts[1:-1], f"{parts[-1]}{quote}"]
    return [line + ("  # noqa: E501" if len(line) > _LINE else "") for line in lines]


def _const_lines(const: _Const) -> list[str]:
    lines = _reason_lines(const.reason)
    assignment = f"{const.name} = {const.literal}"
    if len(assignment) <= _LINE:
        lines.append(assignment)
        return lines
    lines.extend([f"{const.name} = (", f"    {const.literal}", ")"])
    return lines


def _reason_lines(reason: str) -> list[str]:
    text = _flat(reason)
    if not text:
        return []
    lines: list[str] = []
    while text:
        room = _LINE - 2
        if len(text) <= room:
            lines.append(f"# {text}")
            break
        cut = text.rfind(" ", 0, room + 1)
        if cut <= 0:
            cut = room
        lines.append(f"# {text[:cut].rstrip()}")
        text = text[cut:].lstrip()
    return lines


def _assignment(slug: str, title: str) -> list[str]:
    line = f"mcp = MCPServer({_py(slug)}, title={_py(title)})"
    if len(line) <= _LINE:
        return [line]
    return ["mcp = MCPServer(", f"    {_py(slug)},", f"    title={_py(title)},", ")"]


def _invoke(prefix: str, arguments: list[str], *, indent: int) -> list[str]:
    if all("\n" not in item for item in arguments):
        single = f"{prefix}({', '.join(arguments)})"
        if len(single) + indent <= _LINE:
            return [single]
    lines = [f"{prefix}("]
    for argument in arguments:
        parts = argument.split("\n")
        for index, part in enumerate(parts):
            suffix = "," if index == len(parts) - 1 else ""
            lines.append(f"    {part}{suffix}")
    lines.append(")")
    return lines


def _return(expr: str) -> list[str]:
    single = f"return {expr}"
    if "\n" not in expr and len(single) + 4 <= _LINE:
        return [single]
    if "\n" not in expr and expr.endswith(")"):
        open_at = expr.rfind("(")
        head = expr[:open_at]
        inner = expr[open_at + 1 : -1]
        return [f"return {head}(", f"    {inner},", ")"]
    parts = expr.split("\n")
    return [f"return {parts[0]}", *parts[1:]]


def _mapping(pairs: list[tuple[str, str]]) -> str:
    body = ", ".join(f"{_py(key)}: {expr}" for key, expr in pairs)
    single = "{" + body + "}"
    if len(single) <= 72:
        return single
    lines = ["{"]
    for key, expr in pairs:
        parts = expr.split("\n")
        if len(parts) == 1:
            lines.append(f"    {_py(key)}: {expr},")
            continue
        lines.append(f"    {_py(key)}: {parts[0]}")
        lines.extend(f"    {part}" for part in parts[1:])
        lines[-1] = f"{lines[-1]},"
    lines.append("}")
    return "\n".join(lines)


def _auth_literal(auth: tuple[tuple[str, str, str], ...]) -> str:
    items = ", ".join("(" + ", ".join(_py(part) for part in triple) + ")" for triple in auth)
    if len(auth) == 1:
        return f"({items},)"
    return f"({items})"


def _indent(lines: list[str], spaces: int) -> list[str]:
    pad = " " * spaces
    return [f"{pad}{line}" if line else line for line in lines]


def _from_import(module: str, names: list[str]) -> list[str]:
    single = f"from {module} import {', '.join(names)}"
    if len(single) <= _LINE:
        return [single]
    return [f"from {module} import (", *[f"    {name}," for name in names], ")"]


class _Catalog:
    def __init__(self, spec_defs: dict[str, JsonObject]) -> None:
        self._spec_defs = spec_defs
        self._taken = set(_RESERVED_MODELS)
        self._order: list[str] = []
        self._fields: dict[str, tuple[list[str], bool]] = {}
        self._by_body: dict[str, str] = {}
        self._refs: dict[str, _Type] = {}
        self._building: set[str] = set()
        self._finished: set[str] = set()
        self._cyclic: set[str] = set()
        self._ref_stack: set[str] = set()
        self.needs_annotated = False
        self.needs_literal = False
        self.needs_field = False
        self.needs_config = False

    def _reserve(self, base: str) -> str:
        name = base or "Model"
        if name[0].isdigit():
            name = f"Model{name}"
        if keyword.iskeyword(name):
            name = f"{name}_"
        candidate = name
        index = 2
        while candidate in self._taken:
            candidate = f"{name}{index}"
            index += 1
        self._taken.add(candidate)
        return candidate

    @property
    def classes(self) -> set[str]:
        return set(self._finished)

    def compile_output(self, schema: JsonSchema, tool_name: str) -> _Type:
        hint = f"{_pascal(tool_name)}Result"
        if isinstance(schema, dict) and _named_object(schema):
            return self._compile(schema, hint=hint, model_name=self._reserve(hint), defs={})
        return self._compile(schema, hint=hint, model_name=None, defs={})

    def compile_input(self, schema: JsonSchema, tool_name: str, param: str) -> _Type:
        hint = f"{_pascal(tool_name)}{_pascal(param)}"
        return self._compile(schema, hint=hint, model_name=None, defs={})

    def source(self) -> str:
        if not self._order:
            return ""
        lines = ["from __future__ import annotations", ""]
        typing_names: list[str] = []
        if self.needs_annotated:
            typing_names.append("Annotated")
        if self.needs_literal:
            typing_names.append("Literal")
        if typing_names:
            lines.extend([f"from typing import {', '.join(typing_names)}", ""])
        pydantic = ["BaseModel"]
        if self.needs_config:
            pydantic.append("ConfigDict")
        if self.needs_field:
            pydantic.append("Field")
        lines.extend([f"from pydantic import {', '.join(pydantic)}", "", ""])
        for name in self._order:
            fields, forbid = self._fields[name]
            lines.extend(_class_lines(name, fields, forbid))
            lines.extend(["", ""])
        while lines and lines[-1] == "":
            lines.pop()
        if self._cyclic:
            lines.extend(["", ""])
            for name in self._order:
                if name in self._cyclic:
                    lines.append(f"{name}.model_rebuild()")
        return "\n".join(lines)

    def _compile(
        self,
        schema: JsonSchema,
        *,
        hint: str,
        model_name: str | None,
        defs: dict[str, JsonObject],
    ) -> _Type:
        if isinstance(schema, bool) or not isinstance(schema, dict):
            raise _SchemaProblem("boolean schema")
        _reject(schema)
        local = _child_defs(defs, schema)
        peeled, nullable = _peel_null(schema)
        if peeled is not schema:
            return _nullify(
                self._compile(peeled, hint=hint, model_name=model_name, defs=local), nullable
            )
        if "$ref" in schema:
            if _ref_blocked(schema):
                raise _SchemaProblem("ref is combined with other keywords")
            ref = schema.get("$ref")
            if not isinstance(ref, str):
                raise _SchemaProblem("ref is not a string")
            inner = self._compile_ref(ref, local, hint)
            return _nullify(_with_constraints(inner, _constraints(schema)), nullable)
        if "allOf" in schema:
            if _combined(schema, "allOf"):
                raise _SchemaProblem("allOf is combined with other keywords")
            merged, merged_defs = self._merge_all(schema.get("allOf"), local)
            return _nullify(
                self._object(merged, hint=hint, model_name=model_name, defs=merged_defs),
                nullable,
            )
        if "enum" in schema:
            return _nullify(self._enum(schema), nullable)
        if "const" in schema:
            return _nullify(self._const(schema), nullable)
        if _is_object(schema):
            return _nullify(
                self._object(schema, hint=hint, model_name=model_name, defs=local), nullable
            )
        if schema.get("type") == "array":
            return _nullify(self._array(schema, hint=hint, defs=local), nullable)
        return _nullify(self._scalar(schema), nullable)

    def _compile_ref(self, ref: str, defs: dict[str, JsonObject], hint: str) -> _Type:
        cached = self._refs.get(ref)
        if cached is not None:
            if cached.model is not None and cached.model in self._building:
                self._cyclic.add(cached.model)
            return cached
        if ref in self._ref_stack:
            raise _SchemaProblem(f"cyclic ref {ref}")
        body = _lookup(ref, defs, self._spec_defs)
        local = _child_defs(defs, body)
        if _is_object(body) and "$ref" not in body and "allOf" not in body:
            key = _canonical(body)
            found = self._by_body.get(key)
            if found is not None:
                typ = _Type(found, False, "", found)
                self._refs[ref] = typ
                if found in self._building:
                    self._cyclic.add(found)
                return typ
            name = self._reserve(_def_name(ref))
            self._by_body[key] = name
            self._refs[ref] = _Type(name, False, "", name)
            compiled = self._object(body, hint=hint, model_name=name, defs=local)
            self._refs[ref] = compiled
            return compiled
        self._ref_stack.add(ref)
        try:
            compiled = self._compile(body, hint=_def_name(ref), model_name=None, defs=local)
        finally:
            self._ref_stack.discard(ref)
        self._refs[ref] = compiled
        return compiled

    def _object(
        self,
        schema: JsonObject,
        *,
        hint: str,
        model_name: str | None,
        defs: dict[str, JsonObject],
    ) -> _Type:
        if model_name is None:
            key = _canonical(schema)
            found = self._by_body.get(key)
            if found is not None:
                if found in self._building:
                    self._cyclic.add(found)
                return _Type(found, False, "", found)
            name = self._reserve(_pascal(hint))
            self._by_body[key] = name
        else:
            name = model_name
            if name in self._finished:
                return _Type(name, False, "", name)
        if name in self._building:
            self._cyclic.add(name)
            return _Type(name, False, "", name)
        self._building.add(name)
        try:
            fields, forbid = self._properties(schema, owner=name, defs=defs)
        finally:
            self._building.discard(name)
        self._finished.add(name)
        self._order.append(name)
        self._fields[name] = (fields, forbid)
        if forbid:
            self.needs_config = True
        return _Type(name, False, "", name)

    def _properties(
        self, schema: JsonObject, *, owner: str, defs: dict[str, JsonObject]
    ) -> tuple[list[str], bool]:
        if "additionalProperties" in schema and schema.get("additionalProperties") is not False:
            if schema.get("additionalProperties") is True:
                raise _SchemaProblem("additionalProperties is true")
            raise _SchemaProblem("additionalProperties schema is not supported")
        raw_props = schema.get("properties", {})
        if raw_props is None:
            raw_props = {}
        props = _as_object(raw_props, "properties")
        required = _required(schema)
        fields: list[str] = []
        for name, prop in props.items():
            prop_schema = _as_object(prop, "property schema")
            description = prop_schema.get("description")
            text = description if isinstance(description, str) else ""
            typ = self._compile(
                prop_schema, hint=f"{owner}{_pascal(name)}", model_name=None, defs=defs
            )
            fields.append(self._field(name, typ, required=name in required, description=text))
        return fields, schema.get("additionalProperties") is False

    def _field(self, name: str, typ: _Type, *, required: bool, description: str) -> str:
        py_name = _ident(name)
        alias = name if py_name != name else None
        annotation = typ.annotation
        if not required and not typ.nullable and annotation != "None":
            annotation = f"{annotation} | None"
        parts: list[str] = []
        if description:
            parts.append(f"description={_py(description)}")
        if alias is not None:
            parts.append(f"alias={_py(alias)}")
        if typ.constraints:
            parts.append(typ.constraints)
        default = "" if required else " = None"
        if not parts:
            return f"{py_name}: {annotation}{default}"
        self.needs_field = True
        self.needs_annotated = True
        rendered = f"Field({', '.join(parts)})"
        one = f"{py_name}: Annotated[{annotation}, {rendered}]{default}"
        if len(one) + 4 <= _LINE:
            return one
        return "\n".join(
            [f"{py_name}: Annotated[", f"    {annotation},", f"    {rendered},", f"]{default}"]
        )

    def _array(self, schema: JsonObject, *, hint: str, defs: dict[str, JsonObject]) -> _Type:
        items = schema.get("items")
        if isinstance(items, list) or not isinstance(items, dict):
            raise _SchemaProblem("array items are not supported")
        item = self._compile(items, hint=f"{hint}Item", model_name=None, defs=defs)
        return _Type(f"list[{item.annotation}]", False, _constraints(schema), None)

    def _enum(self, schema: JsonObject) -> _Type:
        raw = schema.get("enum")
        if not isinstance(raw, list) or not raw:
            raise _SchemaProblem("enum is empty")
        values: list[JsonValue] = []
        nullable = False
        for item in raw:
            if item is None:
                nullable = True
                continue
            if not _scalar(item):
                raise _SchemaProblem("enum value is not a scalar")
            values.append(item)
        self.needs_literal = True
        if not values:
            return _Type("None", True, _constraints(schema), None)
        annotation = "Literal[" + ", ".join(_py(item) for item in values) + "]"
        if nullable:
            annotation = f"{annotation} | None"
        return _Type(annotation, nullable, _constraints(schema), None)

    def _const(self, schema: JsonObject) -> _Type:
        if "const" not in schema:
            raise _SchemaProblem("const is missing")
        value = schema.get("const")
        if not _scalar(value):
            raise _SchemaProblem("const value is not a scalar")
        self.needs_literal = True
        if value is None:
            return _Type("None", True, "", None)
        return _Type(f"Literal[{_py(value)}]", False, _constraints(schema), None)

    def _scalar(self, schema: JsonObject) -> _Type:
        for key in ("properties", "required", "additionalProperties", "items"):
            if key in schema:
                raise _SchemaProblem(f"keyword {key} is not supported")
        kind = schema.get("type")
        constraints = _constraints(schema)
        if kind == "string":
            return _Type("str", False, constraints, None)
        if kind == "integer":
            return _Type("int", False, constraints, None)
        if kind == "number":
            return _Type("float", False, constraints, None)
        if kind == "boolean":
            return _Type("bool", False, constraints, None)
        if kind == "null":
            return _Type("None", True, "", None)
        raise _SchemaProblem("schema is not supported")

    def _merge_all(
        self, branches: object, defs: dict[str, JsonObject]
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        if not isinstance(branches, list) or not branches:
            raise _SchemaProblem("allOf is empty")
        properties: JsonObject = {}
        required: list[JsonValue] = []
        seen: set[str] = set()
        forbid = False
        accumulated = dict(defs)
        for branch in branches:
            flat, accumulated = self._flatten_object(branch, accumulated, set())
            branch_props = flat.get("properties", {})
            if isinstance(branch_props, dict):
                for key, value in branch_props.items():
                    if key in properties and properties[key] != value:
                        raise _SchemaProblem(f"allOf property {key} conflicts")
                    properties[key] = value
            for name in _required(flat):
                if name not in seen:
                    seen.add(name)
                    required.append(name)
            if flat.get("additionalProperties") is False:
                forbid = True
        merged: JsonObject = {"type": "object", "properties": properties, "required": required}
        if forbid:
            merged["additionalProperties"] = False
        return merged, accumulated

    def _flatten_object(
        self,
        schema: object,
        defs: dict[str, JsonObject],
        seen: set[str],
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        if not isinstance(schema, dict):
            raise _SchemaProblem("allOf branch is not an object")
        _reject(schema)
        local = _child_defs(defs, schema)
        peeled, nullable = _peel_null(schema)
        if nullable:
            raise _SchemaProblem("allOf branch is nullable")
        current = peeled
        if "$ref" in current:
            ref = current.get("$ref")
            if not isinstance(ref, str):
                raise _SchemaProblem("ref is not a string")
            if ref in seen:
                raise _SchemaProblem("cyclic allOf")
            return self._flatten_object(_lookup(ref, local, self._spec_defs), local, seen | {ref})
        if "allOf" in current:
            merged, merged_defs = self._merge_all(current.get("allOf"), local)
            return merged, merged_defs
        if not _is_object(current):
            raise _SchemaProblem("allOf branch is not an object")
        return current, local


def _class_lines(name: str, fields: list[str], forbid: bool) -> list[str]:
    lines = [f"class {name}(BaseModel):"]
    if forbid:
        lines.append('    model_config = ConfigDict(extra="forbid")')
    if not fields:
        if not forbid:
            lines.append("    pass")
        return lines
    if forbid:
        lines.append("")
    for field_line in fields:
        lines.extend(f"    {line}" for line in field_line.split("\n"))
    return lines


def _named_object(schema: JsonObject) -> bool:
    current = schema
    for _ in range(32):
        if "$ref" in current:
            return False
        peeled, _nullable = _peel_null(current)
        if peeled is not current:
            current = peeled
            continue
        if "allOf" in current:
            return True
        return _is_object(current)
    return False


def _is_object(schema: JsonObject) -> bool:
    if schema.get("type") == "object":
        return True
    return "properties" in schema and "type" not in schema and "$ref" not in schema


def _peel_null(schema: JsonObject) -> tuple[JsonObject, bool]:
    nullable = schema.get("nullable") is True
    if "anyOf" in schema:
        if _combined(schema, "anyOf"):
            raise _SchemaProblem("anyOf is combined with other keywords")
        options = schema.get("anyOf")
        if not isinstance(options, list):
            raise _SchemaProblem("anyOf is not a list")
        branches = [item for item in options if not _is_null_schema(item)]
        nulls = [item for item in options if _is_null_schema(item)]
        if len(branches) != 1 or not nulls or len(branches) + len(nulls) != len(options):
            raise _SchemaProblem("anyOf is not a null union")
        branch = branches[0]
        if not isinstance(branch, dict):
            raise _SchemaProblem("anyOf branch is not an object")
        return branch, True
    kinds = schema.get("type")
    if isinstance(kinds, list):
        if not kinds or any(not isinstance(item, str) for item in kinds):
            raise _SchemaProblem("type union is not supported")
        rest = [item for item in kinds if item != "null"]
        if "null" not in kinds or len(rest) != 1:
            raise _SchemaProblem("type union is not supported")
        rewritten: JsonObject = {}
        for key, value in schema.items():
            if key != "type":
                rewritten[key] = value
        rewritten["type"] = rest[0]
        return rewritten, True
    return schema, nullable


def _is_null_schema(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "null":
        return True
    return "const" in value and value.get("const") is None


def _reject(schema: JsonObject) -> None:
    for key in schema:
        if key not in _DOC_KEYS and key not in _CONSTRAINT_KEYS and key not in _CORE_KEYS:
            raise _SchemaProblem(f"keyword {key} is not supported")


def _combined(schema: JsonObject, key: str) -> bool:
    allowed = _DOC_KEYS | {key, "$defs", "nullable"}
    return any(item not in allowed for item in schema)


def _ref_blocked(schema: JsonObject) -> bool:
    allowed = _DOC_KEYS | _CONSTRAINT_KEYS | {"$ref", "$defs"}
    return any(key not in allowed for key in schema)


def _constraints(schema: JsonObject) -> str:
    parts: list[str] = []
    _number_bound(schema, "minimum", "ge", parts)
    _number_bound(schema, "maximum", "le", parts)
    _number_bound(schema, "exclusiveMinimum", "gt", parts)
    _number_bound(schema, "exclusiveMaximum", "lt", parts)
    _int_bound(schema, "minLength", "min_length", parts)
    _int_bound(schema, "maxLength", "max_length", parts)
    _int_bound(schema, "minItems", "min_items", parts)
    _int_bound(schema, "maxItems", "max_items", parts)
    if "pattern" in schema:
        pattern = schema.get("pattern")
        if not isinstance(pattern, str):
            raise _SchemaProblem("pattern is not a string")
        parts.append(f"pattern={_py(pattern)}")
    return ", ".join(parts)


def _number_bound(schema: JsonObject, key: str, flag: str, parts: list[str]) -> None:
    if key not in schema:
        return
    value = schema.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _SchemaProblem(f"{key} is not a number")
    parts.append(f"{flag}={_number(value)}")


def _int_bound(schema: JsonObject, key: str, flag: str, parts: list[str]) -> None:
    if key not in schema:
        return
    value = schema.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SchemaProblem(f"{key} is not an integer")
    parts.append(f"{flag}={value}")


def _number(value: int | float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise _SchemaProblem("constraint is not a finite number")


def _required(schema: JsonObject) -> set[str]:
    raw = schema.get("required", [])
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise _SchemaProblem("required is not a list")
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise _SchemaProblem("required entry is not a string")
        names.add(item)
    return names


def _as_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise _SchemaProblem(f"{label} is not an object")
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _SchemaProblem(f"{label} name is not a string")
        result[key] = item
    return result


def _child_defs(parent: dict[str, JsonObject], schema: JsonObject) -> dict[str, JsonObject]:
    raw = schema.get("$defs")
    if raw is None:
        return parent
    if not isinstance(raw, dict):
        raise _SchemaProblem("$defs is not an object")
    if not raw:
        return parent
    child = dict(parent)
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise _SchemaProblem("$defs entries must be schemas")
        child[key] = value
    return child


def _lookup(ref: str, defs: dict[str, JsonObject], spec_defs: dict[str, JsonObject]) -> JsonObject:
    if ref.startswith("#/$defs/"):
        body = defs.get(_unescape(ref[len("#/$defs/") :]))
    elif ref.startswith("#/defs/"):
        body = spec_defs.get(_unescape(ref[len("#/defs/") :]))
    else:
        raise _SchemaProblem(f"unsupported ref {ref}")
    if not isinstance(body, dict):
        raise _SchemaProblem(f"unknown ref {ref}")
    return body


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _def_name(ref: str) -> str:
    raw = _unescape(ref.rsplit("/", 1)[-1])
    segment = raw.rsplit("_", 1)[-1]
    return _pascal(segment or raw)


def _canonical(schema: JsonObject) -> str:
    return json.dumps(_strip(schema), sort_keys=True, ensure_ascii=False)


def _strip(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: _strip(item)
            for key, item in value.items()
            if key not in _DOC_KEYS and key != "$defs"
        }
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def _nullify(typ: _Type, nullable: bool) -> _Type:
    if not nullable or typ.nullable or typ.annotation == "None":
        return typ
    return _Type(f"{typ.annotation} | None", True, typ.constraints, typ.model)


def _with_constraints(typ: _Type, extra: str) -> _Type:
    if not extra:
        return typ
    if not typ.constraints:
        return _Type(typ.annotation, typ.nullable, extra, typ.model)
    return _Type(typ.annotation, typ.nullable, f"{typ.constraints}, {extra}", typ.model)


def _note(annotation: str, flags: _Flags) -> None:
    if "Literal[" in annotation:
        flags.literal = True


def _mentioned(annotations: list[str], classes: set[str]) -> set[str]:
    found: set[str] = set()
    for annotation in annotations:
        for name in classes:
            if re.search(rf"\b{re.escape(name)}\b", annotation):
                found.add(name)
    return found


def _known(endpoints: dict[str, Endpoint], endpoint_id: str) -> None:
    if endpoint_id not in endpoints:
        raise EmitError(f"unknown endpoint {endpoint_id}")


def _require_json(tool_name: str, endpoint: Endpoint) -> None:
    body = endpoint.request_body
    if body is None:
        return
    media = body.content_type.split(";", 1)[0].strip().lower()
    if media == "application/json" or media.endswith("+json"):
        return
    raise EmitError(f"{tool_name} request body is not JSON")


def _fixed(value: JsonValue) -> str:
    if isinstance(value, (dict, list)) or not _scalar(value):
        raise EmitError("fixed value must be a scalar")
    return _py(value)


def _scalar(value: object) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _py(value: JsonValue) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _SchemaProblem("number is not finite")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise _SchemaProblem("value is not a scalar")


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "api"
    if slug[0].isdigit():
        slug = f"api-{slug}"
    return slug


def _package_version(version: str) -> str:
    if _VERSION.match(version):
        return version
    return "0.1.0"


def _header_lines(uri: str, source_sha: str, tools_sha: str) -> tuple[str, str]:
    line1 = (
        f"GENERATED BY mcp4api {__version__} from {uri} "
        f"(sha256 {source_sha}) and tools.yaml (sha256 {tools_sha})."
    )
    line2 = (
        "DO NOT EDIT. Change tools.yaml or overrides.yaml and run `mcp4api` again; "
        "edits here are overwritten."
    )
    return line1, line2


def _pascal(text: str) -> str:
    parts = re.findall(r"[0-9A-Za-z]+", text)
    if not parts:
        return "Model"
    name = "".join(part[:1].upper() + part[1:] for part in parts)
    if name[0].isdigit():
        name = f"Model{name}"
    if keyword.iskeyword(name):
        name = f"{name}_"
    return name


def _ident(name: str) -> str:
    if _IDENT.match(name) and not keyword.iskeyword(name):
        return name
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"param_{cleaned}" if cleaned else "param"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned


def _unique(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    name = f"{base}_{index}"
    used.add(name)
    return name


def _snake(text: str) -> str:
    spaced = re.sub(r"[^0-9A-Za-z]+", "_", text)
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", spaced)
    step2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1)
    return re.sub(r"_+", "_", step2).strip("_").lower()


def _upper_name(tool: str, param: str) -> str:
    return f"{_upper(tool)}_{_upper(param)}"


def _upper(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").upper()
    if not cleaned:
        return "VALUE"
    if cleaned[0].isdigit():
        return f"V_{cleaned}"
    return cleaned


def _flat(text: str) -> str:
    return " ".join(text.split())


def _secret_names(env_name: str, flags: _Flags) -> list[str]:
    names: list[str] = []
    if flags.api_key:
        names.append(f"{env_name}_API_KEY")
    if flags.bearer:
        names.append(f"{env_name}_TOKEN")
    return names


def _tuple(names: list[str]) -> str:
    if not names:
        return "()"
    inner = ", ".join(_py(name) for name in names)
    if len(names) == 1:
        return f"({inner},)"
    return f"({inner})"


def _env_rows(env_name: str, base_url: str, flags: _Flags) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if flags.api_key:
        rows.append({"name": f"{env_name}_API_KEY", "purpose": _key_purpose(flags.keys)})
    if flags.bearer:
        rows.append(
            {
                "name": f"{env_name}_TOKEN",
                "purpose": "Bearer token sent in the Authorization header.",
            }
        )
    rows.append({"name": f"{env_name}_BASE_URL", "purpose": f"Base URL. Defaults to {base_url}."})
    rows.append(
        {"name": f"{env_name}_TIMEOUT", "purpose": "Request timeout in seconds. Defaults to 30."}
    )
    rows.append(
        {
            "name": "MCP4API_ALLOWED_HOSTS",
            "purpose": "Comma-separated hosts the client may call. Unset allows any host.",
        }
    )
    return rows


def _key_purpose(keys: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for place, name in keys:
        if place == "header":
            parts.append(f"API key sent in the {name} header")
        elif place == "query":
            parts.append(f"API key sent in the {name} query parameter")
        else:
            parts.append(f"API key sent in the {name} cookie")
    if not parts:
        return "API key."
    return ". ".join(parts) + "."


def _claude(program: str, env_name: str, flags: _Flags) -> str:
    env = {row["name"]: "" for row in _env_rows(env_name, "", flags)}
    payload = {
        "mcpServers": {
            program: {
                "command": "uvx",
                "args": ["--from", "./generated", program],
                "env": env,
            }
        }
    }
    return json.dumps(payload, indent=2)


def _toml(text: str) -> str:
    literal = json.dumps(text, ensure_ascii=False)
    return literal[1:-1]


def _spec_defs(spec: ApiSpec) -> dict[str, JsonObject]:
    return {name: schema for name, schema in spec.defs.items() if isinstance(schema, dict)}


def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("mcp4api.emit", "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        newline_sequence="\n",
    )


def _render(environment: Environment, name: str, context: Mapping[str, object]) -> str:
    try:
        rendered: object = environment.get_template(name).render(context)
    except (TemplateError, UndefinedError) as exc:
        raise EmitError(f"could not render {name}") from exc
    if not isinstance(rendered, str):
        raise EmitError(f"could not render {name}")
    return rendered
