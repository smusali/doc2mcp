from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from mcp4api.errors import UpstreamError, UsageError
from mcp4api.ingest.convert import (
    _METHODS,
    _def_name,
    _normalize_schema,
    _swagger_to_openapi,
)
from mcp4api.ingest.load import (
    Dialect,
    _allow_hosts,
    _as_string,
    _at_pointer,
    _choose_media,
    _detect,
    _enforce_ref_policy,
    _escape_pointer,
    _fetch_primary,
    _finding,
    _flag,
    _fragment_pointer,
    _hide_url_refs,
    _is_json_media,
    _is_url,
    _join_pointer,
    _json_equal,
    _parse_bytes,
    _RefFetch,
    _split_ref,
    _strings,
    _strip_userinfo,
    _validate,
    _Version,
)
from mcp4api.model import (
    ApiSpec,
    Endpoint,
    Finding,
    Header,
    JsonObject,
    JsonSchema,
    JsonValue,
    Parameter,
    ParameterLocation,
    RequestBody,
    Response,
    SecurityScheme,
    SecuritySchemeLocation,
    SecuritySchemeType,
    Server,
    ServerVariable,
    Source,
    dump_endpoints,
)

MAX_REF_DEPTH = 64


_IGNORED_HEADERS = frozenset({"accept", "content-type", "authorization"})


@dataclass
class _Document:
    key: str
    data: JsonObject
    directory: Path | None
    url: str | None


class _Resolver:
    def __init__(
        self,
        root: _Document,
        *,
        dialect: Dialect,
        fetch: _RefFetch,
        swagger: bool,
    ) -> None:
        self.root = root
        self.dialect = dialect
        self.fetch = fetch
        self.swagger = swagger
        self.defs: dict[str, JsonSchema] = {}
        self.findings: list[Finding] = []
        self.endpoint_id: str | None = None
        self.scheme_names: set[str] = set()
        self._documents = {root.key: root}
        self._names: dict[str, str] = {}
        self._used: set[str] = set()
        self._stack: list[str] = []

    def source_pointer(self, pointer: str) -> str:
        if not self.swagger:
            return pointer
        replacements = (
            ("/components/schemas/", "/definitions/"),
            ("/components/parameters/", "/parameters/"),
            ("/components/responses/", "/responses/"),
        )
        for lifted, original in replacements:
            if pointer.startswith(lifted):
                return original + pointer[len(lifted) :]
        return pointer

    def unresolvable(self, pointer: str, ref: str) -> None:
        self.findings.append(
            _finding(
                "unresolvable-ref",
                pointer=self.source_pointer(pointer),
                message=f"cannot resolve {ref}",
                endpoint_id=self.endpoint_id,
            )
        )

    def schema(
        self, value: JsonValue, *, pointer: str, base: _Document | None = None
    ) -> JsonSchema:
        document = self.root if base is None else base

        def resolve_ref(ref: str, ref_pointer: str) -> JsonSchema:
            return self.schema_ref(ref, pointer=ref_pointer, base=document)

        return _normalize_schema(
            value, dialect=self.dialect, resolve_ref=resolve_ref, pointer=pointer
        )

    def schema_ref(self, ref: str, *, pointer: str, base: _Document) -> JsonSchema:
        key = self._identity(ref, base)
        if key in self._names and key not in self._stack:
            return {"$ref": f"#/defs/{self._names[key]}"}
        if key in self._stack:
            return {"$ref": f"#/defs/{self._names[key]}"}
        if len(self._stack) >= MAX_REF_DEPTH:
            self.unresolvable(pointer, ref)
            return True
        loaded = self._lookup(ref, base, pointer)
        if loaded is None:
            self.unresolvable(pointer, ref)
            return True
        target, document, target_pointer = loaded
        name = self._assign(key, ref)
        self._stack.append(key)

        def resolve_ref(inner: str, inner_pointer: str) -> JsonSchema:
            return self.schema_ref(inner, pointer=inner_pointer, base=document)

        try:
            converted = _normalize_schema(
                target,
                dialect=self.dialect,
                resolve_ref=resolve_ref,
                pointer=target_pointer,
            )
        finally:
            self._stack.pop()
        self.defs[name] = converted
        return {"$ref": f"#/defs/{name}"}

    def object_ref(
        self, ref: str, *, pointer: str, base: _Document, stack: tuple[str, ...] = ()
    ) -> JsonObject | None:
        key = self._identity(ref, base)
        if key in stack:
            self.unresolvable(pointer, ref)
            return None
        loaded = self._lookup(ref, base, pointer)
        if loaded is None:
            self.unresolvable(pointer, ref)
            return None
        target, document, _target_pointer = loaded
        if (
            isinstance(target, dict)
            and isinstance(target.get("$ref"), str)
            and list(target) == ["$ref"]
        ):
            inner = target["$ref"]
            if isinstance(inner, str):
                return self.object_ref(
                    inner,
                    pointer=pointer,
                    base=document,
                    stack=(*stack, key),
                )
        if isinstance(target, dict):
            return target
        self.unresolvable(pointer, ref)
        return None

    def _assign(self, key: str, ref: str) -> str:
        existing = self._names.get(key)
        if existing is not None:
            return existing
        base_name = _def_name(ref)
        name = base_name
        suffix = 2
        while name in self._used:
            name = f"{base_name}_{suffix}"
            suffix += 1
        self._used.add(name)
        self._names[key] = name
        return name

    def _identity(self, ref: str, base: _Document) -> str:
        target, fragment = _split_ref(ref)
        if not target:
            return f"{base.key}{fragment}"
        if _is_url(target):
            return f"{_strip_userinfo(target)}{fragment}"
        if base.url is not None:
            return f"{_strip_userinfo(urljoin(base.url, target))}{fragment}"
        resolved = self._file_path(target, base)
        if resolved is None:
            return f"{target}{fragment}"
        return f"{resolved.as_posix()}{fragment}"

    def _lookup(
        self,
        ref: str,
        base: _Document,
        pointer: str,
    ) -> tuple[JsonValue, _Document, str] | None:
        target, fragment = _split_ref(ref)
        fragment_pointer = _fragment_pointer(fragment)
        if not target:
            found = _at_pointer(base.data, fragment)
            if found is None:
                return None
            return found, base, fragment_pointer
        if base.url is not None and not _is_url(target):
            target = urljoin(base.url, target)
        document = self._load_target(target, base, pointer)
        if document is None:
            return None
        found = _at_pointer(document.data, fragment)
        if found is None:
            return None
        return found, document, fragment_pointer

    def _load_target(self, target: str, base: _Document, pointer: str) -> _Document | None:
        if _is_url(target):
            return self._load_url(target)
        path = self._file_path(target, base)
        if path is None:
            return None
        key = path.as_posix()
        cached = self._documents.get(key)
        if cached is not None:
            return cached
        if not path.is_file():
            return None
        try:
            body = path.read_bytes()
        except OSError:
            return None
        try:
            data = _parse_bytes(body, None)
        except UpstreamError:
            return None
        document = _Document(key=key, data=data, directory=path.parent, url=None)
        self._documents[key] = document
        del pointer
        return document

    def _load_url(self, url: str) -> _Document | None:
        clean = _strip_userinfo(url)
        cached = self._documents.get(clean)
        if cached is not None:
            return cached
        fetched = self.fetch.get(clean)
        if fetched is None:
            return None
        body, content_type = fetched
        try:
            data = _parse_bytes(body, content_type)
        except UpstreamError:
            return None
        document = _Document(key=clean, data=data, directory=None, url=clean)
        self._documents[clean] = document
        return document

    def _file_path(self, target: str, base: _Document) -> Path | None:
        if base.url is not None:
            return None
        if base.directory is None:
            return None
        return (base.directory / target).resolve()


def _examples_from(node: JsonObject) -> list[JsonValue]:
    named: list[JsonValue] = []
    raw = node.get("examples")
    if isinstance(raw, dict):
        for name in sorted(raw):
            item = raw[name]
            if isinstance(item, dict) and "value" in item:
                named.append(item["value"])
    elif isinstance(raw, list):
        named.extend(raw)
    if "example" not in node:
        return named
    example = node["example"]
    if any(_json_equal(example, item) for item in named):
        return named
    return [example, *named]


def _effective_style(location: str, raw: JsonValue) -> str:
    if isinstance(raw, str) and raw:
        return raw
    if location in {"path", "header"}:
        return "simple"
    return "form"


def _effective_explode(style: str, raw: JsonValue) -> bool:
    if isinstance(raw, bool):
        return raw
    return style in {"form", "cookie"}


def _servers(value: JsonValue) -> list[Server]:
    if not isinstance(value, list):
        return []
    servers: list[Server] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _as_string(item.get("url", None))
        if url is None:
            continue
        description = _as_string(item.get("description", None))
        variables: dict[str, ServerVariable] | None = None
        raw_variables = item.get("variables")
        if isinstance(raw_variables, dict):
            built: dict[str, ServerVariable] = {}
            for name, raw in raw_variables.items():
                if not isinstance(name, str) or not isinstance(raw, dict):
                    continue
                default = _as_string(raw.get("default", None))
                if default is None:
                    continue
                enum = _strings(raw.get("enum", None))
                variable_description = _as_string(raw.get("description", None))
                built[name] = ServerVariable(
                    default=default,
                    enum=enum,
                    description=variable_description,
                )
            variables = built or None
        servers.append(Server(url=url, description=description, variables=variables))
    return servers


def _security_requirements(value: JsonValue) -> list[dict[str, list[str]]]:
    if not isinstance(value, list):
        return []
    requirements: list[dict[str, list[str]]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        requirement: dict[str, list[str]] = {}
        for name, scopes in item.items():
            if not isinstance(name, str):
                continue
            requirement[name] = _strings(scopes) or []
        requirements.append(requirement)
    return requirements


def _scheme_type(value: JsonValue) -> SecuritySchemeType | None:
    if value == "apiKey":
        return "apiKey"
    if value == "http":
        return "http"
    if value == "mutualTLS":
        return "mutualTLS"
    if value == "oauth2":
        return "oauth2"
    if value == "openIdConnect":
        return "openIdConnect"
    return None


def _scheme_location(value: JsonValue) -> SecuritySchemeLocation | None:
    if value == "query":
        return "query"
    if value == "header":
        return "header"
    if value == "cookie":
        return "cookie"
    return None


def _security_scheme(raw: JsonObject) -> SecurityScheme | None:
    scheme_type = _scheme_type(raw.get("type", None))
    if scheme_type is None:
        return None
    flows: JsonObject | None = None
    raw_flows = raw.get("flows")
    if isinstance(raw_flows, dict):
        flows = raw_flows
    return SecurityScheme(
        type=scheme_type,
        description=_as_string(raw.get("description", None)),
        name=_as_string(raw.get("name", None)),
        location=_scheme_location(raw.get("in", None)),
        scheme=_as_string(raw.get("scheme", None)),
        bearer_format=_as_string(raw.get("bearerFormat", None)),
        flows=flows,
        open_id_connect_url=_as_string(raw.get("openIdConnectUrl", None)),
    )


def _media_schema(entry: JsonObject) -> JsonValue | None:
    if "schema" in entry:
        return entry["schema"]
    if "itemSchema" in entry:
        return entry["itemSchema"]
    return None


def _is_success(status: str) -> bool:
    return len(status) == 3 and status.isdigit() and status.startswith("2")


def _deref(
    value: JsonValue,
    resolver: _Resolver,
    *,
    pointer: str,
    base: _Document,
) -> JsonObject | None:
    if isinstance(value, dict) and isinstance(value.get("$ref"), str):
        ref = value["$ref"]
        if isinstance(ref, str):
            resolved = resolver.object_ref(ref, pointer=pointer, base=base)
            if resolved is None or len(value) == 1 or resolver.dialect == "3.0":
                return resolved
            merged = dict(resolved)
            for key, item in value.items():
                if key != "$ref":
                    merged[key] = item
            return merged
    if isinstance(value, dict):
        return value
    return None


def _parameter_location(value: str) -> ParameterLocation | None:
    if value == "path":
        return "path"
    if value == "query":
        return "query"
    if value == "header":
        return "header"
    if value == "cookie":
        return "cookie"
    return None


def _parameter_objects(
    value: JsonValue,
    resolver: _Resolver,
    *,
    pointer: str,
    base: _Document,
) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    parameters: list[JsonObject] = []
    for index, item in enumerate(value):
        resolved = _deref(item, resolver, pointer=_join_pointer(pointer, str(index)), base=base)
        if resolved is not None:
            parameters.append(resolved)
    return parameters


def _merge_parameters(
    path_level: list[JsonObject], operation_level: list[JsonObject]
) -> list[JsonObject]:
    merged = list(path_level)
    index: dict[tuple[str, str], int] = {}
    for position, parameter in enumerate(merged):
        name = parameter.get("name")
        location = parameter.get("in")
        if isinstance(name, str) and isinstance(location, str):
            index[(name, location)] = position
    for parameter in operation_level:
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or not isinstance(location, str):
            continue
        key = (name, location)
        if key in index:
            merged[index[key]] = parameter
        else:
            index[key] = len(merged)
            merged.append(parameter)
    return merged


def _build_parameter(
    parameter: JsonObject,
    resolver: _Resolver,
    *,
    pointer: str,
    base: _Document,
) -> Parameter | None:
    location = parameter.get("in")
    name = parameter.get("name")
    if not isinstance(name, str) or not isinstance(location, str):
        return None
    parameter_location = _parameter_location(location)
    if location == "querystring":
        resolver.findings.append(
            _finding(
                "unsupported-feature",
                pointer=resolver.source_pointer(pointer),
                message="querystring parameters are not modelled",
                endpoint_id=resolver.endpoint_id,
            )
        )
        return None
    if parameter_location is None:
        resolver.findings.append(
            _finding(
                "unsupported-feature",
                pointer=resolver.source_pointer(pointer),
                message=f"parameter location {location} is not modelled",
                endpoint_id=resolver.endpoint_id,
            )
        )
        return None
    if parameter_location == "header" and name.lower() in _IGNORED_HEADERS:
        return None
    schema: JsonSchema = True
    examples = _examples_from(parameter)
    content = parameter.get("content")
    if isinstance(content, dict) and content:
        media = _choose_media(content)
        entry = content.get(media) if media is not None else None
        if isinstance(entry, dict):
            media_schema = _media_schema(entry)
            if media_schema is not None:
                schema = resolver.schema(
                    media_schema, pointer=_join_pointer(pointer, "content"), base=base
                )
            examples = _examples_from(entry) or examples
    elif "schema" in parameter:
        schema = resolver.schema(
            parameter["schema"], pointer=_join_pointer(pointer, "schema"), base=base
        )
    style = _effective_style(parameter_location, parameter.get("style", None))
    required = (
        True if parameter_location == "path" else _flag(parameter.get("required", False), False)
    )
    return Parameter(
        name=name,
        location=parameter_location,
        required=required,
        schema_=schema,
        style=style,
        explode=_effective_explode(style, parameter.get("explode", None)),
        description=_as_string(parameter.get("description", None)),
        examples=examples or None,
    )


def _build_headers(
    value: JsonValue,
    resolver: _Resolver,
    *,
    pointer: str,
    base: _Document,
) -> dict[str, Header] | None:
    if not isinstance(value, dict):
        return None
    headers: dict[str, Header] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or name.lower() == "content-type":
            continue
        resolved = _deref(raw, resolver, pointer=_join_pointer(pointer, name), base=base)
        if resolved is None:
            continue
        schema: JsonSchema = True
        if "schema" in resolved:
            schema = resolver.schema(
                resolved["schema"],
                pointer=_join_pointer(pointer, name),
                base=base,
            )
        elif isinstance(resolved.get("content"), dict):
            content = resolved["content"]
            if isinstance(content, dict):
                media = _choose_media(content)
                entry = content.get(media) if media is not None else None
                if isinstance(entry, dict):
                    media_schema = _media_schema(entry)
                    if media_schema is not None:
                        schema = resolver.schema(media_schema, pointer=pointer, base=base)
        examples = _examples_from(resolved)
        headers[name] = Header(
            required=_flag(resolved.get("required", False), False),
            schema_=schema,
            description=_as_string(resolved.get("description", None)),
            examples=examples or None,
        )
    return headers or None


def _build_request_body(
    operation: JsonObject,
    resolver: _Resolver,
    *,
    pointer: str,
    base: _Document,
) -> RequestBody | None:
    if "requestBody" not in operation:
        return None
    resolved = _deref(operation["requestBody"], resolver, pointer=pointer, base=base)
    if resolved is None:
        return None
    content = resolved.get("content")
    if not isinstance(content, dict):
        return None
    media = _choose_media(content)
    if media is None:
        return None
    entry = content.get(media)
    if not isinstance(entry, dict):
        return None
    media_schema = _media_schema(entry)
    schema = (
        resolver.schema(media_schema, pointer=_join_pointer(pointer, "content"), base=base)
        if media_schema is not None
        else True
    )
    examples = _examples_from(entry)
    if not _is_json_media(media):
        resolver.findings.append(
            _finding(
                "non-json-body",
                pointer=resolver.source_pointer(pointer),
                message=f"request body is {media}",
                endpoint_id=resolver.endpoint_id,
            )
        )
    return RequestBody(
        content_type=media,
        schema_=schema,
        required=_flag(resolved.get("required", False), False),
        examples=examples or None,
    )


def _build_responses(
    operation: JsonObject,
    resolver: _Resolver,
    *,
    pointer: str,
    base: _Document,
) -> dict[str, Response]:
    raw = operation.get("responses")
    mapping = (
        _deref(raw, resolver, pointer=pointer, base=base) if not isinstance(raw, dict) else raw
    )
    if not isinstance(mapping, dict):
        return {}
    responses: dict[str, Response] = {}
    for status, response in mapping.items():
        if not isinstance(status, str):
            continue
        resolved = _deref(response, resolver, pointer=_join_pointer(pointer, status), base=base)
        if resolved is None:
            continue
        content_type: str | None = None
        schema: JsonSchema | None = None
        examples: list[JsonValue] | None = None
        content = resolved.get("content")
        if isinstance(content, dict) and content:
            media = _choose_media(content)
            entry = content.get(media) if media is not None else None
            if isinstance(entry, dict) and media is not None:
                content_type = media
                media_schema = _media_schema(entry)
                if media_schema is not None:
                    schema = resolver.schema(
                        media_schema,
                        pointer=_join_pointer(_join_pointer(pointer, status), "content"),
                        base=base,
                    )
                found = _examples_from(entry)
                examples = found or None
        links = resolved.get("links")
        if isinstance(links, dict) and links:
            resolver.findings.append(
                _finding(
                    "unsupported-feature",
                    pointer=resolver.source_pointer(
                        _join_pointer(_join_pointer(pointer, status), "links")
                    ),
                    message="links are not modelled",
                    endpoint_id=resolver.endpoint_id,
                )
            )
        responses[status] = Response(
            content_type=content_type,
            schema_=schema,
            examples=examples,
            headers=_build_headers(
                resolved.get("headers", None),
                resolver,
                pointer=_join_pointer(_join_pointer(pointer, status), "headers"),
                base=base,
            ),
        )
    return responses


def _response_findings(
    responses: dict[str, Response], resolver: _Resolver, provenance: str
) -> None:
    success = [status for status in responses if _is_success(status)]
    if not success:
        resolver.findings.append(
            _finding(
                "no-success-response",
                pointer=resolver.source_pointer(provenance),
                message="no 2xx response",
                endpoint_id=resolver.endpoint_id,
            )
        )
        return
    for status in success:
        if status in {"204", "205"}:
            continue
        if responses[status].schema_ is None:
            resolver.findings.append(
                _finding(
                    "no-response-schema",
                    pointer=resolver.source_pointer(provenance),
                    message="success response has no schema",
                    endpoint_id=resolver.endpoint_id,
                )
            )
            return


def _operations(path_item: JsonObject, version: _Version) -> Iterator[tuple[str, str, JsonObject]]:
    for method in _METHODS:
        operation = path_item.get(method)
        if isinstance(operation, dict):
            yield method.upper(), method, operation
    if version.numbers < (3, 2):
        return
    query = path_item.get("query")
    if isinstance(query, dict):
        yield "QUERY", "query", query
    extra = path_item.get("additionalOperations")
    if not isinstance(extra, dict):
        return
    known = {method.upper() for method in _METHODS} | {"QUERY"}
    for name, operation in extra.items():
        if isinstance(name, str) and isinstance(operation, dict) and name.upper() not in known:
            yield name.upper(), name, operation


def _operation_pointer(path: str, token: str) -> str:
    base = f"/paths/{_escape_pointer(path)}"
    if token in _METHODS or token == "query":
        return f"{base}/{token}"
    return f"{base}/additionalOperations/{_escape_pointer(token)}"


def _build_endpoint(
    *,
    method: str,
    token: str,
    path: str,
    operation: JsonObject,
    path_item: JsonObject,
    root_servers: list[Server],
    root_security: list[dict[str, list[str]]],
    resolver: _Resolver,
    base: _Document,
) -> Endpoint:
    normalized = path if path.startswith("/") else f"/{path}"
    provenance = _operation_pointer(path, token)
    endpoint_id = f"{method} {normalized}"
    resolver.endpoint_id = endpoint_id
    operation_id = _as_string(operation.get("operationId", None))
    if not operation_id:
        operation_id = None
        resolver.findings.append(
            _finding(
                "missing-operation-id",
                pointer=resolver.source_pointer(provenance),
                message="operationId is missing",
                endpoint_id=endpoint_id,
            )
        )
    if isinstance(operation.get("callbacks"), dict) and operation["callbacks"]:
        resolver.findings.append(
            _finding(
                "unsupported-feature",
                pointer=resolver.source_pointer(f"{provenance}/callbacks"),
                message="callbacks are not modelled",
                endpoint_id=endpoint_id,
            )
        )
    merged = _merge_parameters(
        _parameter_objects(
            path_item.get("parameters", None),
            resolver,
            pointer=f"/paths/{_escape_pointer(path)}/parameters",
            base=base,
        ),
        _parameter_objects(
            operation.get("parameters", None),
            resolver,
            pointer=f"{provenance}/parameters",
            base=base,
        ),
    )
    parameters: list[Parameter] = []
    for parameter in merged:
        built = _build_parameter(parameter, resolver, pointer=provenance, base=base)
        if built is not None:
            parameters.append(built)
    responses = _build_responses(operation, resolver, pointer=f"{provenance}/responses", base=base)
    _response_findings(responses, resolver, provenance)
    if "servers" in operation and isinstance(operation.get("servers"), list):
        effective = _servers(operation["servers"])
    elif "servers" in path_item and isinstance(path_item.get("servers"), list):
        effective = _servers(path_item["servers"])
    else:
        effective = root_servers
    if not effective:
        resolver.findings.append(
            _finding(
                "missing-server",
                pointer=resolver.source_pointer(provenance),
                message="no server URL",
                endpoint_id=endpoint_id,
            )
        )
    security = (
        _security_requirements(operation["security"]) if "security" in operation else root_security
    )
    for requirement in security:
        for name in requirement:
            if name not in resolver.scheme_names:
                resolver.findings.append(
                    _finding(
                        "undocumented-security",
                        pointer=resolver.source_pointer(provenance),
                        message=f"security scheme {name} is not defined",
                        endpoint_id=endpoint_id,
                    )
                )
    return Endpoint(
        id=endpoint_id,
        operation_id=operation_id,
        method=method,
        path=normalized,
        summary=_as_string(operation.get("summary", None)),
        description=_as_string(operation.get("description", None)),
        tags=_strings(operation.get("tags", None)),
        deprecated=_flag(operation.get("deprecated", False), False),
        parameters=parameters,
        request_body=_build_request_body(
            operation,
            resolver,
            pointer=f"{provenance}/requestBody",
            base=base,
        ),
        responses=responses,
        security=security,
        provenance=resolver.source_pointer(provenance),
        servers=effective if effective != root_servers else None,
    )


def _load_schemes(
    document: JsonObject, resolver: _Resolver, base: _Document
) -> dict[str, SecurityScheme]:
    components = document.get("components")
    if not isinstance(components, dict):
        resolver.scheme_names = set()
        return {}
    raw = components.get("securitySchemes")
    if not isinstance(raw, dict):
        resolver.scheme_names = set()
        return {}
    schemes: dict[str, SecurityScheme] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        resolved = _deref(
            value,
            resolver,
            pointer=f"/components/securitySchemes/{_escape_pointer(name)}",
            base=base,
        )
        if resolved is None:
            continue
        scheme = _security_scheme(resolved)
        if scheme is not None:
            schemes[name] = scheme
    resolver.scheme_names = set(schemes)
    return schemes


def _duplicate_findings(endpoints: list[Endpoint], resolver: _Resolver) -> None:
    counts: dict[str, int] = {}
    for endpoint in endpoints:
        if endpoint.operation_id:
            counts[endpoint.operation_id] = counts.get(endpoint.operation_id, 0) + 1
    for endpoint in endpoints:
        operation_id = endpoint.operation_id
        if operation_id and counts.get(operation_id, 0) > 1:
            resolver.findings.append(
                _finding(
                    "duplicate-operation-id",
                    pointer=endpoint.provenance,
                    message=f"operationId {operation_id} is duplicated",
                    endpoint_id=endpoint.id,
                )
            )


def ingest(spec: str, *, offline: bool, allow_ref_hosts: tuple[str, ...]) -> ApiSpec:
    if _is_url(spec):
        if offline:
            raise UsageError(f"offline: refusing {spec}")
        body, content_type = _fetch_primary(spec)
        uri = _strip_userinfo(spec)
        directory = None
        url: str | None = uri
    else:
        spec_path = Path(spec)
        if not spec_path.is_file():
            raise UsageError(f"not found: {spec_path}")
        try:
            body = spec_path.read_bytes()
        except OSError as exc:
            raise UpstreamError("not an OpenAPI document") from exc
        content_type = None
        uri = spec_path.name
        directory = spec_path.parent
        url = None
    digest = hashlib.sha256(body).hexdigest()
    document = _parse_bytes(body, content_type)
    version = _detect(document)
    allow_hosts = _allow_hosts(spec, allow_ref_hosts)
    saw_url = _enforce_ref_policy(document, offline=offline, allow_hosts=allow_hosts)
    findings = _validate(_hide_url_refs(document) if saw_url else document, version)
    fetch = _RefFetch(offline=offline, allow_hosts=allow_hosts)
    try:
        walked = _swagger_to_openapi(document) if version.kind == "swagger" else document
        root = _Document(key=uri, data=walked, directory=directory, url=url)
        resolver = _Resolver(
            root, dialect=version.dialect, fetch=fetch, swagger=version.kind == "swagger"
        )
        resolver.findings.extend(findings)
        schemes = _load_schemes(walked, resolver, root)
        info = walked.get("info")
        info_object = info if isinstance(info, dict) else {}
        root_servers = _servers(walked.get("servers", None))
        root_security = _security_requirements(walked.get("security", []))
        webhooks = walked.get("webhooks")
        if isinstance(webhooks, dict) and webhooks:
            resolver.findings.append(
                _finding(
                    "unsupported-feature",
                    pointer="/webhooks",
                    message="webhooks are not modelled",
                )
            )
        endpoints: list[Endpoint] = []
        raw_paths = walked.get("paths")
        if isinstance(raw_paths, dict):
            for path, path_item in raw_paths.items():
                if not isinstance(path, str):
                    continue
                resolved = _deref(
                    path_item,
                    resolver,
                    pointer=f"/paths/{_escape_pointer(path)}",
                    base=root,
                )
                if resolved is None:
                    continue
                for method, token, operation in _operations(resolved, version):
                    endpoints.append(
                        _build_endpoint(
                            method=method,
                            token=token,
                            path=path,
                            operation=operation,
                            path_item=resolved,
                            root_servers=root_servers,
                            root_security=root_security,
                            resolver=resolver,
                            base=root,
                        )
                    )
        if not endpoints and not root_servers:
            resolver.findings.append(
                _finding("missing-server", pointer="/servers", message="no server URL")
            )
        _duplicate_findings(endpoints, resolver)
        endpoints.sort(key=lambda endpoint: (endpoint.path, endpoint.method))
        resolver.findings.sort(
            key=lambda finding: (
                finding.code,
                finding.pointer,
                finding.endpoint_id or "",
                finding.message,
            )
        )
        return ApiSpec(
            title=_as_string(info_object.get("title", None)) or "",
            version=_as_string(info_object.get("version", None)) or "",
            servers=root_servers,
            security_schemes=schemes,
            defs=resolver.defs,
            endpoints=endpoints,
            findings=resolver.findings,
            source=Source(kind=version.kind, uri=uri, sha256=digest),
        )
    finally:
        fetch.close()


def write_endpoints(spec: ApiSpec, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "endpoints.json").write_bytes(dump_endpoints(spec).encode("utf-8"))


def read_endpoints(path: Path) -> tuple[ApiSpec, bytes]:
    """Load an endpoints document.

    Args:
        path: Path to ``endpoints.json``.

    Returns:
        The parsed spec and the exact bytes that were read.
    """
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"not found: {path}") from exc
    try:
        return ApiSpec.model_validate_json(payload), payload
    except (ValueError, UnicodeDecodeError) as exc:
        raise UsageError("endpoints.json is invalid") from exc
