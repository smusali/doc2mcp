from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
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

from mcp4api import __version__
from mcp4api.errors import UpstreamError, UsageError
from mcp4api.model import (
    Finding,
    FindingCode,
    FindingSeverity,
    JsonObject,
    JsonValue,
    expect_json,
)

MAX_BYTES = 25 * 1024 * 1024


MAX_EXPANDED_NODES = 2_000_000


MAX_REDIRECTS = 5


MAX_VALIDATION_FINDINGS = 100


TIMEOUT = 30.0


_USER_AGENT = f"mcp4api/{__version__}"


_REDIRECTS = frozenset({301, 302, 303, 307, 308})


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


def _allow_hosts(spec: str, extra: tuple[str, ...]) -> frozenset[str]:
    hosts = {host.lower() for host in extra if host}
    if _is_url(spec):
        host = _hostname(spec)
        if host:
            hosts.add(host)
    return frozenset(hosts)
