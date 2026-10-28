from __future__ import annotations

from collections.abc import Callable

from mcp4api.ingest.load import (
    Dialect,
    _as_string,
    _at_pointer,
    _flag,
    _join_pointer,
    _json_equal,
    _media_base,
    _split_ref,
    _strings,
)
from mcp4api.model import (
    JsonObject,
    JsonSchema,
    JsonValue,
    expect_json,
)

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


_SCHEMA_OBJECTS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)


_SCHEMA_MAPS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)


_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


_SWAGGER_REF_PREFIXES = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/parameters/", "#/components/parameters/"),
    ("#/responses/", "#/components/responses/"),
)


_RefHandler = Callable[[str, str], JsonSchema]


def _rewrite_ref_string(ref: str) -> str:
    for old, new in _SWAGGER_REF_PREFIXES:
        if ref.startswith(old):
            return new + ref[len(old) :]
    return ref


def _rewrite_refs(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten: JsonObject = {}
    for key, item in value.items():
        if key == "$ref" and isinstance(item, str):
            rewritten[key] = _rewrite_ref_string(item)
        else:
            rewritten[key] = _rewrite_refs(item)
    return rewritten


def _local_pointer(document: JsonObject, ref: str) -> JsonValue | None:
    target, fragment = _split_ref(ref)
    if target:
        return None
    return _at_pointer(document, fragment)


def _deref_swagger(value: JsonValue, document: JsonObject, seen: set[str]) -> JsonObject | None:
    current = value
    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        ref = current["$ref"]
        if not isinstance(ref, str) or ref in seen:
            return None
        seen.add(ref)
        found = _local_pointer(document, ref)
        if not isinstance(found, dict):
            return None
        current = found
    if isinstance(current, dict):
        return current
    return None


def _swagger_schema(parameter: JsonObject) -> JsonObject:
    if parameter.get("type") == "file":
        return {"type": "string", "format": "binary"}
    schema: JsonObject = {}
    for key in (
        "type",
        "format",
        "items",
        "default",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "maxItems",
        "minItems",
        "uniqueItems",
        "enum",
        "multipleOf",
    ):
        if key in parameter:
            schema[key] = parameter[key]
    rewritten = _rewrite_refs(schema)
    if isinstance(rewritten, dict):
        return rewritten
    return {}


def _swagger_style(parameter: JsonObject) -> tuple[str, bool] | None:
    collection = parameter.get("collectionFormat")
    if not isinstance(collection, str):
        return None
    if collection == "multi":
        return "form", True
    if collection in {"csv", "tsv"}:
        return "form", False
    if collection == "ssv":
        return "spaceDelimited", False
    if collection == "pipes":
        return "pipeDelimited", False
    return None


def _lift_parameter(parameter: JsonObject) -> JsonObject:
    ref = parameter.get("$ref")
    if isinstance(ref, str):
        return {"$ref": _rewrite_ref_string(ref)}
    location = parameter.get("in")
    name = parameter.get("name")
    if not isinstance(location, str) or not isinstance(name, str):
        return {}
    lifted: JsonObject = {
        "name": name,
        "in": location,
        "required": True if location == "path" else _flag(parameter.get("required", False), False),
        "schema": _swagger_schema(parameter),
    }
    description = _as_string(parameter.get("description", None))
    if description is not None:
        lifted["description"] = description
    style = _swagger_style(parameter)
    if style is not None:
        lifted["style"] = style[0]
        lifted["explode"] = style[1]
    if "example" in parameter:
        lifted["example"] = parameter["example"]
    return lifted


def _lift_header(header: JsonObject) -> JsonObject:
    ref = header.get("$ref")
    if isinstance(ref, str):
        return {"$ref": _rewrite_ref_string(ref)}
    schema = _swagger_schema(header)
    lifted: JsonObject = {"schema": schema or {}}
    description = _as_string(header.get("description", None))
    if description is not None:
        lifted["description"] = description
    return lifted


def _string_items(value: JsonValue, default: list[str]) -> list[str]:
    items = _strings(value)
    if items:
        return items
    return default


def _lift_response(response: JsonValue, produces: list[str], document: JsonObject) -> JsonValue:
    if isinstance(response, dict) and isinstance(response.get("$ref"), str):
        ref = response["$ref"]
        if isinstance(ref, str):
            return {"$ref": _rewrite_ref_string(ref)}
    resolved = _deref_swagger(response, document, set())
    if resolved is None:
        return {"description": ""}
    lifted: JsonObject = {"description": _as_string(resolved.get("description", None)) or ""}
    if "schema" in resolved:
        schema = _rewrite_refs(resolved["schema"])
        content: JsonObject = {}
        for media in produces:
            content[media] = {"schema": schema}
        lifted["content"] = content
    headers = resolved.get("headers")
    if isinstance(headers, dict):
        lifted_headers: JsonObject = {}
        for name, header in headers.items():
            if isinstance(name, str) and isinstance(header, dict):
                lifted_headers[name] = _lift_header(header)
        if lifted_headers:
            lifted["headers"] = lifted_headers
    return lifted


def _form_body(parameters: list[JsonObject], consumes: list[str]) -> JsonObject:
    properties: JsonObject = {}
    required: list[JsonValue] = []
    multipart = any(_media_base(media) == "multipart/form-data" for media in consumes)
    for parameter in parameters:
        name = parameter.get("name")
        if not isinstance(name, str):
            continue
        if parameter.get("type") == "file":
            multipart = True
        properties[name] = _swagger_schema(parameter)
        if _flag(parameter.get("required", False), False):
            required.append(name)
    schema: JsonObject = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    media = "multipart/form-data" if multipart else "application/x-www-form-urlencoded"
    body: JsonObject = {"content": {media: {"schema": schema}}}
    if required:
        body["required"] = True
    return body


def _body_parameter(parameter: JsonObject, consumes: list[str]) -> JsonObject:
    schema = parameter.get("schema", {})
    content: JsonObject = {}
    for media in consumes:
        content[media] = {"schema": _rewrite_refs(schema)}
    body: JsonObject = {
        "content": content,
        "required": _flag(parameter.get("required", False), False),
    }
    description = _as_string(parameter.get("description", None))
    if description is not None:
        body["description"] = description
    return body


def _lift_operation(
    operation: JsonObject, document: JsonObject, consumes: list[str], produces: list[str]
) -> JsonObject:
    lifted: JsonObject = {}
    for key in ("operationId", "summary", "description", "tags", "deprecated", "security"):
        if key in operation:
            lifted[key] = operation[key]
    parameters: list[JsonValue] = []
    form_parameters: list[JsonObject] = []
    body: JsonObject | None = None
    raw_parameters = operation.get("parameters")
    if isinstance(raw_parameters, list):
        for item in raw_parameters:
            resolved = _deref_swagger(item, document, set())
            if resolved is None:
                continue
            location = resolved.get("in")
            if location == "body" and body is None:
                body = _body_parameter(resolved, consumes)
                continue
            if location == "formData":
                form_parameters.append(resolved)
                continue
            parameters.append(_lift_parameter(resolved))
    if parameters:
        lifted["parameters"] = parameters
    if body is None and form_parameters:
        body = _form_body(form_parameters, consumes)
    if body is not None:
        lifted["requestBody"] = body
    responses: JsonObject = {}
    raw_responses = operation.get("responses")
    if isinstance(raw_responses, dict):
        for status, response in raw_responses.items():
            if isinstance(status, str):
                responses[status] = _lift_response(response, produces, document)
    lifted["responses"] = responses
    return lifted


def _lift_path(
    path_item: JsonObject, document: JsonObject, consumes: list[str], produces: list[str]
) -> JsonObject:
    lifted: JsonObject = {}
    parameters: list[JsonValue] = []
    raw_parameters = path_item.get("parameters")
    if isinstance(raw_parameters, list):
        for item in raw_parameters:
            resolved = _deref_swagger(item, document, set())
            if resolved is None or resolved.get("in") in {"body", "formData"}:
                continue
            parameters.append(_lift_parameter(resolved))
    if parameters:
        lifted["parameters"] = parameters
    for method in _METHODS:
        operation = path_item.get(method)
        if isinstance(operation, dict):
            operation_consumes = _string_items(operation.get("consumes", None), consumes)
            operation_produces = _string_items(operation.get("produces", None), produces)
            lifted[method] = _lift_operation(
                operation, document, operation_consumes, operation_produces
            )
    return lifted


def _swagger_servers(document: JsonObject) -> list[JsonValue]:
    host = document.get("host")
    if not isinstance(host, str) or not host:
        return []
    base = document.get("basePath")
    suffix = base if isinstance(base, str) else ""
    schemes = _strings(document.get("schemes", None)) or ["https"]
    return [{"url": f"{scheme}://{host}{suffix}"} for scheme in schemes]


def _lift_security_scheme(raw: JsonObject) -> JsonObject | None:
    kind = raw.get("type")
    lifted: JsonObject = {}
    description = _as_string(raw.get("description", None))
    if description is not None:
        lifted["description"] = description
    if kind == "basic":
        lifted["type"] = "http"
        lifted["scheme"] = "basic"
        return lifted
    if kind == "apiKey":
        lifted["type"] = "apiKey"
        name = _as_string(raw.get("name", None))
        location = _as_string(raw.get("in", None))
        if name is not None:
            lifted["name"] = name
        if location is not None:
            lifted["in"] = location
        return lifted
    if kind != "oauth2":
        return None
    flow = raw.get("flow")
    names = {
        "implicit": "implicit",
        "password": "password",
        "application": "clientCredentials",
        "accessCode": "authorizationCode",
    }
    if not isinstance(flow, str) or flow not in names:
        return None
    flow_object: JsonObject = {}
    scopes = raw.get("scopes")
    flow_object["scopes"] = scopes if isinstance(scopes, dict) else {}
    for source, target in (("authorizationUrl", "authorizationUrl"), ("tokenUrl", "tokenUrl")):
        url = _as_string(raw.get(source, None))
        if url is not None:
            flow_object[target] = url
    lifted["type"] = "oauth2"
    lifted["flows"] = {names[flow]: flow_object}
    return lifted


def _swagger_to_openapi(document: JsonObject) -> JsonObject:
    consumes = _string_items(document.get("consumes", None), ["application/json"])
    produces = _string_items(document.get("produces", None), ["application/json"])
    components: JsonObject = {}
    definitions = document.get("definitions")
    if isinstance(definitions, dict):
        schemas = _rewrite_refs(definitions)
        if isinstance(schemas, dict):
            components["schemas"] = schemas
    parameters = document.get("parameters")
    if isinstance(parameters, dict):
        lifted_parameters: JsonObject = {}
        for name, parameter in parameters.items():
            if not isinstance(name, str) or not isinstance(parameter, dict):
                continue
            if parameter.get("in") in {"body", "formData"}:
                continue
            lifted_parameters[name] = _lift_parameter(parameter)
        if lifted_parameters:
            components["parameters"] = lifted_parameters
    responses = document.get("responses")
    if isinstance(responses, dict):
        lifted_responses: JsonObject = {}
        for name, response in responses.items():
            if isinstance(name, str):
                lifted_responses[name] = _lift_response(response, produces, document)
        if lifted_responses:
            components["responses"] = lifted_responses
    schemes = document.get("securityDefinitions")
    if isinstance(schemes, dict):
        lifted_schemes: JsonObject = {}
        for name, scheme in schemes.items():
            if isinstance(name, str) and isinstance(scheme, dict):
                lifted_scheme = _lift_security_scheme(scheme)
                if lifted_scheme is not None:
                    lifted_schemes[name] = lifted_scheme
        if lifted_schemes:
            components["securitySchemes"] = lifted_schemes
    paths: JsonObject = {}
    raw_paths = document.get("paths")
    if isinstance(raw_paths, dict):
        for path, path_item in raw_paths.items():
            if isinstance(path, str) and isinstance(path_item, dict):
                paths[path] = _lift_path(path_item, document, consumes, produces)
    lifted: JsonObject = {
        "openapi": "3.0.3",
        "info": document.get("info") if isinstance(document.get("info"), dict) else {},
        "paths": paths,
    }
    servers = _swagger_servers(document)
    if servers:
        lifted["servers"] = servers
    if "security" in document:
        lifted["security"] = document["security"]
    if components:
        lifted["components"] = components
    return lifted


def _apply_exclusive(schema: JsonObject, exclusive: str, bound: str) -> JsonObject:
    value = schema.get(exclusive)
    if not isinstance(value, bool):
        return schema
    result = {key: item for key, item in schema.items() if key != exclusive}
    if value and bound in result:
        result[exclusive] = result.pop(bound)
    return result


def _apply_examples(schema: JsonObject) -> JsonObject:
    if "example" not in schema and "examples" not in schema:
        return schema
    examples: list[JsonValue] = []
    current = schema.get("examples")
    if isinstance(current, list):
        examples.extend(current)
    if "example" in schema:
        example = schema["example"]
        if not any(_json_equal(example, item) for item in examples):
            examples.append(example)
    result = {key: value for key, value in schema.items() if key not in {"example", "examples"}}
    if examples:
        result["examples"] = examples
    return result


def _apply_nullable(schema: JsonObject) -> JsonObject:
    nullable = schema.get("nullable")
    if not isinstance(nullable, bool):
        return schema
    rest = {key: value for key, value in schema.items() if key != "nullable"}
    if not nullable:
        return rest
    enum = rest.get("enum")
    if isinstance(enum, list) and not any(item is None for item in enum):
        rest["enum"] = [*enum, None]
    type_value = rest.get("type")
    if isinstance(type_value, str):
        if type_value != "null":
            rest["type"] = [type_value, "null"]
        return rest
    if isinstance(type_value, list):
        if not any(item == "null" for item in type_value):
            rest["type"] = [*type_value, "null"]
        return rest
    if any(key in rest for key in ("allOf", "anyOf", "oneOf")):
        return {"anyOf": [rest, {"type": "null"}]}
    return rest


def _normalize_schema(
    value: JsonValue,
    *,
    dialect: Dialect,
    resolve_ref: _RefHandler | None = None,
    pointer: str = "",
) -> JsonSchema:
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict):
        return True
    ref = value.get("$ref")
    if isinstance(ref, str):
        resolved = resolve_ref(ref, pointer) if resolve_ref is not None else True
        if dialect == "3.0":
            return resolved
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        if not siblings:
            return resolved
        converted = _normalize_schema(
            siblings, dialect=dialect, resolve_ref=resolve_ref, pointer=pointer
        )
        if converted == {}:
            return resolved
        return {"allOf": [resolved, converted]}
    converted = _convert_keywords(value, dialect=dialect, resolve_ref=resolve_ref, pointer=pointer)
    converted = _apply_exclusive(converted, "exclusiveMinimum", "minimum")
    converted = _apply_exclusive(converted, "exclusiveMaximum", "maximum")
    converted = _apply_nullable(converted)
    return _apply_examples(converted)


def _convert_keywords(
    value: JsonObject,
    *,
    dialect: Dialect,
    resolve_ref: _RefHandler | None,
    pointer: str,
) -> JsonObject:
    result: JsonObject = {}
    for key, raw in value.items():
        target = "$defs" if key == "definitions" else key
        if target in result:
            continue
        child = _join_pointer(pointer, target)
        if target in _SCHEMA_OBJECTS:
            result[target] = _normalize_schema(
                raw, dialect=dialect, resolve_ref=resolve_ref, pointer=child
            )
            continue
        if target in _SCHEMA_MAPS and isinstance(raw, dict):
            mapped: JsonObject = {}
            for name, schema in raw.items():
                if isinstance(name, str):
                    mapped[name] = _normalize_schema(
                        schema,
                        dialect=dialect,
                        resolve_ref=resolve_ref,
                        pointer=_join_pointer(child, name),
                    )
            result[target] = mapped
            continue
        if target in _SCHEMA_LISTS and isinstance(raw, list):
            result[target] = [
                _normalize_schema(
                    item,
                    dialect=dialect,
                    resolve_ref=resolve_ref,
                    pointer=_join_pointer(child, str(index)),
                )
                for index, item in enumerate(raw)
            ]
            continue
        try:
            result[target] = expect_json(raw)
        except TypeError:
            continue
    return result


def _def_name(ref: str) -> str:
    chars = [char if char.isalnum() or char in "._-" else "_" for char in ref]
    name = "".join(chars).strip("._")
    return name or "schema"
