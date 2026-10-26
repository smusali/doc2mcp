from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx2
import yaml
from jsonschema.exceptions import ValidationError
from openapi_spec_validator.validation.validators import (
    OpenAPIV2SpecValidator,
    OpenAPIV30SpecValidator,
    OpenAPIV31SpecValidator,
    OpenAPIV32SpecValidator,
    SpecValidator,
)
from referencing.exceptions import Unresolvable
from yaml import MappingNode, Node, SequenceNode

from doc2mcp import __version__
from doc2mcp.cli import UpstreamError, UsageError
from doc2mcp.model import (
    ApiSpec,
    Endpoint,
    Finding,
    FindingCode,
    FindingSeverity,
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
    expect_json,
)

MAX_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_NODES = 2_000_000
MAX_REF_DEPTH = 64
MAX_REDIRECTS = 5
MAX_VALIDATION_FINDINGS = 100
TIMEOUT = 30.0
_USER_AGENT = f"doc2mcp/{__version__}"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_IGNORED_HEADERS = frozenset({"accept", "content-type", "authorization"})
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
_SEVERITY: dict[FindingCode, FindingSeverity] = {
    "duplicate-operation-id": "warning",
    "invalid-against-spec": "error",
    "missing-operation-id": "warning",
    "missing-server": "warning",
    "no-response-schema": "warning",
    "no-success-response": "warning",
    "non-json-body": "warning",
    "undocumented-security": "warning",
    "unresolvable-ref": "error",
    "unsupported-feature": "info",
}
_BOOL_TAG = "tag:yaml.org,2002:bool"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_BOOL_PATTERN = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_LOADER_READY = False
Dialect = Literal["3.0", "3.1"]
_RefHandler = Callable[[str, str], JsonSchema]


class _Loader(yaml.SafeLoader):
    pass


def _configure_loader() -> None:
    global _LOADER_READY
    if _LOADER_READY:
        return
    copied: dict[object, list[tuple[str, re.Pattern[str]]]] = {}
    resolvers: Mapping[object, list[tuple[str, re.Pattern[str]]]] = (
        yaml.SafeLoader.yaml_implicit_resolvers
    )
    for key, items in resolvers.items():
        kept = [(tag, pattern) for tag, pattern in items if tag not in {_TIMESTAMP_TAG, _BOOL_TAG}]
        if kept:
            copied[key] = kept
    for key in ("t", "T", "f", "F"):
        copied.setdefault(key, []).append((_BOOL_TAG, _BOOL_PATTERN))
    _Loader.yaml_implicit_resolvers = copied
    _LOADER_READY = True


def _expanded_size(node: Node) -> int:
    memo: dict[int, int] = {}

    def size(current: Node, stack: set[int]) -> int:
        identity = id(current)
        cached = memo.get(identity)
        if cached is not None:
            return cached
        if identity in stack:
            raise UpstreamError("YAML document is too large")
        stack.add(identity)
        total = 1
        children: list[Node] = []
        if isinstance(current, MappingNode):
            for key, value in current.value:
                children.append(key)
                children.append(value)
        elif isinstance(current, SequenceNode):
            children.extend(current.value)
        for child in children:
            total += size(child, stack)
            if total > MAX_EXPANDED_NODES:
                raise UpstreamError("YAML document is too large")
        stack.remove(identity)
        memo[identity] = total
        return total

    try:
        return size(node, set())
    except RecursionError as exc:
        raise UpstreamError("YAML document is too large") from exc


def _load_yaml(body: bytes) -> JsonValue:
    _configure_loader()
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UpstreamError("not an OpenAPI document") from exc
    loader = _Loader(text)
    try:
        node = loader.get_single_node()
    except RecursionError as exc:
        raise UpstreamError("YAML document is too large") from exc
    except yaml.YAMLError as exc:
        raise UpstreamError("not an OpenAPI document") from exc
    if node is None:
        raise UpstreamError("not an OpenAPI document")
    _expanded_size(node)
    try:
        loaded: object = loader.construct_document(node)
    except RecursionError as exc:
        raise UpstreamError("YAML document is too large") from exc
    except yaml.YAMLError as exc:
        raise UpstreamError("not an OpenAPI document") from exc
    try:
        return expect_json(loaded)
    except TypeError as exc:
        raise UpstreamError("not an OpenAPI document") from exc


def _is_url(value: str) -> bool:
    return urlsplit(value).scheme in {"http", "https"}


def _hostname(url: str) -> str:
    host = urlsplit(url).hostname
    return host.lower() if isinstance(host, str) else ""


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _is_html(body: bytes, content_type: str | None) -> bool:
    if content_type and "text/html" in content_type.lower():
        return True
    sample = body.lstrip()
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:].lstrip()
    head = sample[:32].lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _looks_like_json(body: bytes, content_type: str | None) -> bool:
    sample = body.lstrip()
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:].lstrip()
    if sample.startswith((b"{", b"[")):
        return True
    if content_type is None:
        return False
    lowered = content_type.lower()
    return "json" in lowered and "html" not in lowered


def _parse_bytes(body: bytes, content_type: str | None) -> JsonObject:
    if _is_html(body, content_type):
        raise UpstreamError(
            "HTML page is not an OpenAPI document; pass the URL of the OpenAPI document itself."
        )
    if _looks_like_json(body, content_type):
        try:
            parsed: object = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError("not an OpenAPI document") from exc
        try:
            value = expect_json(parsed)
        except TypeError as exc:
            raise UpstreamError("not an OpenAPI document") from exc
    else:
        value = _load_yaml(body)
    if isinstance(value, dict):
        return value
    raise UpstreamError("not an OpenAPI document")


def _read_limited(response: httpx2.Response, url: str) -> bytes:
    declared = response.headers.get("content-length")
    if isinstance(declared, str) and declared.isdigit() and int(declared) > MAX_BYTES:
        raise UpstreamError(f"response exceeds 25 MiB: {url}")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_BYTES:
            raise UpstreamError(f"response exceeds 25 MiB: {url}")
        chunks.append(chunk)
    return b"".join(chunks)


def _request(
    url: str,
    *,
    client: httpx2.Client,
    allow_hosts: frozenset[str] | None,
) -> tuple[bytes, str | None]:
    current = _strip_userinfo(url)
    for _hop in range(MAX_REDIRECTS + 1):
        if allow_hosts is not None and _hostname(current) not in allow_hosts:
            raise UpstreamError(f"refusing {current}")
        try:
            with client.stream(
                "GET",
                current,
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            ) as response:
                location = response.headers.get("location")
                content_type = response.headers.get("content-type")
                if response.status_code in _REDIRECTS and isinstance(location, str) and location:
                    current = _strip_userinfo(urljoin(current, location))
                    continue
                if response.status_code >= 400:
                    raise UpstreamError(f"failed to fetch {url}")
                return _read_limited(response, current), content_type
        except UpstreamError:
            raise
        except httpx2.HTTPError as exc:
            raise UpstreamError(f"failed to fetch {url}") from exc
    raise UpstreamError(f"failed to fetch {url}")


def _fetch_primary(url: str) -> tuple[bytes, str | None]:
    with httpx2.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=True) as client:
        return _request(url, client=client, allow_hosts=None)


class _RefFetch:
    def __init__(self, *, offline: bool, allow_hosts: frozenset[str]) -> None:
        self.offline = offline
        self.allow_hosts = allow_hosts
        self._client: httpx2.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def get(self, url: str) -> tuple[bytes, str | None] | None:
        clean = _strip_userinfo(url)
        if self.offline:
            raise UsageError(f"offline: refusing {clean}")
        if _hostname(clean) not in self.allow_hosts:
            raise UpstreamError(f"refusing {clean}")
        if self._client is None:
            self._client = httpx2.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)
        try:
            return _request(clean, client=self._client, allow_hosts=self.allow_hosts)
        except UpstreamError as exc:
            if exc.message.startswith("response exceeds 25 MiB"):
                raise
            return None


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _join_pointer(base: str, token: str) -> str:
    suffix = _escape_pointer(token)
    if not base:
        return f"/{suffix}"
    return f"{base}/{suffix}"


def _split_ref(ref: str) -> tuple[str, str]:
    if ref.startswith("#"):
        return "", ref
    if "#" in ref:
        target, fragment = ref.split("#", 1)
        return target, f"#{fragment}"
    return ref, "#"


def _internal_ref(ref: str) -> bool:
    return ref == "#" or ref.startswith("#/")


def _fragment_pointer(fragment: str) -> str:
    if fragment in {"", "#"}:
        return ""
    if fragment.startswith("#"):
        return fragment[1:]
    return fragment


def _at_pointer(data: JsonValue, pointer: str) -> JsonValue | None:
    if pointer in {"", "#"}:
        return data
    body = pointer[1:] if pointer.startswith("#") else pointer
    if not body.startswith("/"):
        return None
    current: JsonValue = data
    for raw in body.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        if isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _json_equal(left: JsonValue, right: JsonValue) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(item, other) for item, other in zip(left, right, strict=True)
        )
    return left == right


def _media_base(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _is_json_media(value: str) -> bool:
    base = _media_base(value)
    return base == "application/json" or base.endswith("+json")


def _choose_media(content: JsonObject) -> str | None:
    names = [key for key in content if isinstance(key, str)]
    for name in names:
        if _media_base(name) == "application/json":
            return name
    plus_json = sorted(name for name in names if _media_base(name).endswith("+json"))
    if plus_json:
        return plus_json[0]
    for preferred in ("application/x-www-form-urlencoded", "multipart/form-data"):
        for name in names:
            if _media_base(name) == preferred:
                return name
    if not names:
        return None
    return sorted(names)[0]


def _as_string(value: JsonValue) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _strings(value: JsonValue) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [item for item in value if isinstance(item, str)]
    return items or None


def _flag(value: JsonValue, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _finding(
    code: FindingCode,
    *,
    pointer: str,
    message: str,
    endpoint_id: str | None = None,
) -> Finding:
    return Finding(
        code=code,
        severity=_SEVERITY[code],
        endpoint_id=endpoint_id,
        pointer=pointer or "/",
        message=message,
    )


def _one_line(message: str) -> str:
    line = message.splitlines()[0].strip() if message else ""
    if not line:
        return "document does not match the OpenAPI specification"
    if len(line) > 300:
        return line[:300]
    return line


def _error_pointer(error: ValidationError) -> str:
    parts = [""]
    for segment in error.absolute_path:
        if isinstance(segment, int):
            parts.append(str(segment))
        elif isinstance(segment, str):
            parts.append(_escape_pointer(segment))
        else:
            parts.append(_escape_pointer(str(segment)))
    if len(parts) == 1:
        return "/"
    return "/".join(parts)


@dataclass(frozen=True)
class _Version:
    kind: Literal["openapi", "swagger"]
    numbers: tuple[int, int]
    dialect: Dialect


def _detect(document: JsonObject) -> _Version:
    swagger = document.get("swagger")
    if isinstance(swagger, str) and swagger.split(".", 1)[0] == "2":
        return _Version("swagger", (2, 0), "3.0")
    openapi = document.get("openapi")
    if isinstance(openapi, str):
        parts = openapi.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            numbers = (int(parts[0]), int(parts[1]))
            if numbers == (3, 0):
                return _Version("openapi", numbers, "3.0")
            if numbers in {(3, 1), (3, 2)}:
                return _Version("openapi", numbers, "3.1")
    raise UpstreamError("not an OpenAPI document")


def _enforce_ref_policy(
    value: JsonValue,
    *,
    offline: bool,
    allow_hosts: frozenset[str],
) -> bool:
    saw_url = False
    seen: set[int] = set()

    def walk(current: JsonValue) -> None:
        nonlocal saw_url
        if isinstance(current, list):
            identity = id(current)
            if identity in seen:
                return
            seen.add(identity)
            for item in current:
                walk(item)
            return
        if not isinstance(current, dict):
            return
        identity = id(current)
        if identity in seen:
            return
        seen.add(identity)
        ref = current.get("$ref")
        if isinstance(ref, str) and not _internal_ref(ref):
            target, _fragment = _split_ref(ref)
            if _is_url(target):
                saw_url = True
                clean = _strip_userinfo(target)
                if offline:
                    raise UsageError(f"offline: refusing {clean}")
                if _hostname(clean) not in allow_hosts:
                    raise UpstreamError(f"refusing {clean}")
        for child in current.values():
            walk(child)

    walk(value)
    return saw_url


def _hide_url_refs(value: JsonValue) -> JsonObject:
    memo: dict[int, JsonValue] = {}

    def copy(current: JsonValue) -> JsonValue:
        if not isinstance(current, dict) and not isinstance(current, list):
            return current
        identity = id(current)
        cached = memo.get(identity)
        if cached is not None:
            return cached
        if isinstance(current, list):
            built: list[JsonValue] = []
            memo[identity] = built
            built.extend(copy(item) for item in current)
            return built
        ref = current.get("$ref")
        if isinstance(ref, str):
            target, _fragment = _split_ref(ref)
            if _is_url(target):
                hidden: JsonValue = {"type": "object"}
                memo[identity] = hidden
                return hidden
        mapped: JsonObject = {}
        memo[identity] = mapped
        for key, item in current.items():
            mapped[key] = copy(item)
        return mapped

    copied = copy(value)
    if isinstance(copied, dict):
        return copied
    return {}


def _validator_for(version: _Version) -> type[SpecValidator]:
    if version.kind == "swagger":
        return OpenAPIV2SpecValidator
    if version.numbers == (3, 0):
        return OpenAPIV30SpecValidator
    if version.numbers == (3, 1):
        return OpenAPIV31SpecValidator
    return OpenAPIV32SpecValidator


def _validate(document: JsonObject, version: _Version) -> list[Finding]:
    findings: list[Finding] = []
    try:
        validator = _validator_for(version)(document, base_uri="")
        errors = validator.schema_validator.iter_errors(validator.schema)
        for count, error in enumerate(errors):
            if count >= MAX_VALIDATION_FINDINGS:
                break
            if not isinstance(error, ValidationError):
                continue
            findings.append(
                _finding(
                    "invalid-against-spec",
                    pointer=_error_pointer(error),
                    message=_one_line(error.message),
                )
            )
    except Unresolvable:
        return findings
    except Exception as exc:
        findings.append(
            _finding(
                "invalid-against-spec",
                pointer="/",
                message=_one_line(exc.__class__.__name__),
            )
        )
    return findings


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


def _allow_hosts(spec: str, extra: tuple[str, ...]) -> frozenset[str]:
    hosts = {host.lower() for host in extra if host}
    if _is_url(spec):
        host = _hostname(spec)
        if host:
            hosts.add(host)
    return frozenset(hosts)


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
