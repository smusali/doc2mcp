from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import socketserver
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, TypeAlias, TypedDict
from urllib.parse import parse_qsl, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.client import negotiate_auto  # type: ignore[attr-defined]
from mcp.types import CallToolResult

JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
JsonObject: TypeAlias = dict[str, JsonValue]


class Plan(TypedDict):
    allowlist: list[str]
    base_url_env: str
    bindings: list[JsonObject]
    calls: list[JsonObject]
    generated: str
    module: str
    redact_env_names: list[str]
    timeout_env: str
    timeout_seconds: float
    upstream: str
    upstream_url: str


def plan_from(
    *,
    allowlist: list[str],
    base_url_env: str,
    bindings: Sequence[object],
    calls: Sequence[object],
    generated: str,
    module: str,
    redact_env_names: list[str],
    timeout_env: str,
    timeout_seconds: float,
    upstream: str,
    upstream_url: str,
) -> Plan:
    """Check a plan object and return it as a typed plan.

    Args:
        allowlist: Hosts the proxy may dial.
        base_url_env: Environment variable that sets the API base URL.
        bindings: Values copied from an earlier tool into a later call.
        calls: Calls the suite will make.
        generated: Directory that contains the generated server, relative to the plan.
        module: Import name of the generated server.
        redact_env_names: Names of environment variables whose values are redacted.
        timeout_env: Environment variable that sets the request timeout.
        timeout_seconds: Request timeout.
        upstream: ``fixture``, ``sandbox``, or ``live``.
        upstream_url: Upstream base URL. Empty for fixtures.

    Returns:
        The same plan, checked field by field.
    """
    return {
        "allowlist": allowlist,
        "base_url_env": base_url_env,
        "bindings": _object_list(bindings, "bindings"),
        "calls": _object_list(calls, "calls"),
        "generated": generated,
        "module": module,
        "redact_env_names": redact_env_names,
        "timeout_env": timeout_env,
        "timeout_seconds": timeout_seconds,
        "upstream": upstream,
        "upstream_url": upstream_url,
    }


_BODY_LIMIT = 1_000_000
_HEADER_LIMIT = 64 * 1024


@dataclass
class Exchange:
    method: str
    path: str
    query: dict[str, str]
    body: JsonValue
    status: int
    response: JsonValue
    synthesized: bool = False
    hang: bool = False


@dataclass
class Proxy:
    allow: set[str]
    dialed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _httpd: socketserver.ThreadingTCPServer | None = None
    _thread: threading.Thread | None = None

    def start(self) -> str:
        """Start the allowlist proxy.

        Returns:
            The proxy URL on ``127.0.0.1``.
        """
        httpd = _ProxyServer(self)
        self._httpd = httpd
        port = httpd.server_address[1]
        if not isinstance(port, int):
            raise RuntimeError("proxy port is not an integer")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._thread = thread
        return f"http://127.0.0.1:{port}"

    def close(self) -> None:
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self._httpd = None

    def note_dial(self, host: str) -> None:
        with self._lock:
            self.dialed.append(host)

    def note_block(self, host: str) -> None:
        with self._lock:
            self.blocked.append(host)

    def note_exchange(self, exchange: Exchange) -> None:
        with self._lock:
            self.exchanges.append(exchange)

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return len(self.exchanges), len(self.blocked)

    def since(self, exchanges: int, blocked: int) -> tuple[list[Exchange], list[str]]:
        with self._lock:
            return self.exchanges[exchanges:], self.blocked[blocked:]


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, proxy: Proxy) -> None:
        self.proxy = proxy
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _ProxyServer):
            return
        proxy = server.proxy
        try:
            _relay(self.request, proxy)
        except OSError:
            return


@dataclass
class _Fixture:
    queues: dict[str, list[Exchange]]
    served: list[Exchange] = field(default_factory=list)
    tool: str = ""
    synthetics: list[Exchange] = field(default_factory=list)
    synthetic_at: int = 0
    negative: bool = False
    hang: bool = False
    mark: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _httpd: socketserver.ThreadingTCPServer | None = None

    def start(self) -> str:
        httpd = _FixtureServer(self)
        self._httpd = httpd
        port = httpd.server_address[1]
        if not isinstance(port, int):
            raise RuntimeError("fixture port is not an integer")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    def close(self) -> None:
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self._httpd = None

    def begin(self, tool: str, synthetics: list[Exchange], *, negative: bool, hang: bool) -> None:
        with self._lock:
            self.tool = tool
            self.synthetics = synthetics
            self.synthetic_at = 0
            self.negative = negative
            self.hang = hang
            self.mark = len(self.served)

    def take(self) -> list[Exchange]:
        with self._lock:
            return list(self.served[self.mark :])

    def answer(self, method: str, path: str, query: dict[str, str], body: JsonValue) -> Exchange:
        with self._lock:
            if self.hang:
                chosen = Exchange(method, path, query, body, 200, {}, synthesized=True, hang=True)
            elif self.negative:
                chosen = Exchange(method, path, query, body, 500, {"error": "negative call"})
                chosen.synthesized = True
            else:
                queue = self.queues.setdefault(self.tool, [])
                if queue and _matches(queue[0], method, path, query, body):
                    chosen = queue.pop(0)
                    chosen.synthesized = False
                elif self.synthetic_at < len(self.synthetics):
                    base = self.synthetics[self.synthetic_at]
                    self.synthetic_at += 1
                    chosen = Exchange(
                        method,
                        path,
                        query,
                        body,
                        base.status,
                        base.response,
                        synthesized=True,
                        hang=base.hang,
                    )
                else:
                    chosen = Exchange(
                        method,
                        path,
                        query,
                        body,
                        500,
                        {"error": "no fixture"},
                        synthesized=True,
                    )
            self.served.append(chosen)
            should_hang = chosen.hang
        if should_hang:
            time.sleep(30)
        return chosen


class _FixtureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, fixture: _Fixture) -> None:
        self.fixture = fixture
        super().__init__(("127.0.0.1", 0), _FixtureHandler)


class _FixtureHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _FixtureServer):
            return
        try:
            head, rest = _read_head(self.request)
        except OSError:
            return
        if head is None:
            return
        request, headers, extra = head
        parts = request.split(" ")
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]
        parsed = urlsplit(target)
        path = parsed.path or "/"
        query = {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
        payload = _body_bytes(headers, extra + rest, self.request)
        body = _json_body(payload)
        chosen = server.fixture.answer(method, path, query, body)
        if chosen.hang:
            return
        encoded = b"" if chosen.response is None else json.dumps(chosen.response).encode("utf-8")
        if chosen.status == 204:
            encoded = b""
        status_line = f"HTTP/1.1 {chosen.status} {_status_text(chosen.status)}\r\n".encode()
        header = (
            b"Content-Type: application/json\r\nContent-Length: "
            + str(len(encoded)).encode()
            + b"\r\nConnection: close\r\n\r\n"
        )
        try:
            self.request.sendall(status_line + header + encoded)
        except OSError:
            return


def execute(plan: object) -> JsonObject:
    """Run a verification plan over stdio and return one result per call.

    Args:
        plan: Plan object written for this run.

    Returns:
        An object with a ``calls`` array. Each call lists assertion outcomes.
    """
    parsed = _parse_plan(plan)
    proxy = Proxy(set(parsed.allowlist))
    proxy_url = proxy.start()
    fixture: _Fixture | None = None
    base_url = parsed.upstream_url
    try:
        if parsed.upstream == "fixture":
            queues = _load_cassettes(Path(__file__).resolve().parent / "cassettes")
            fixture = _Fixture(queues)
            base_url = fixture.start()
        return _run(parsed, proxy, proxy_url, fixture, base_url)
    finally:
        if fixture is not None:
            fixture.close()
        proxy.close()


@dataclass(frozen=True)
class _Step:
    success_status: int
    response_schema: JsonObject
    synthetic_status: int
    synthetic_body: JsonValue
    hang: bool


@dataclass(frozen=True)
class _Call:
    id: str
    tool: str
    kind: str
    arguments: JsonObject
    negative: bool
    steps: tuple[_Step, ...]
    required: tuple[str, ...]
    nullable: tuple[str, ...]
    output_step: int
    hang: bool


@dataclass(frozen=True)
class _Binding:
    tool: str
    input: str
    from_tool: str
    field: str


@dataclass(frozen=True)
class _Plan:
    module: str
    generated: str
    base_url_env: str
    timeout_env: str
    redact_env_names: tuple[str, ...]
    timeout_seconds: float
    allowlist: tuple[str, ...]
    upstream: str
    upstream_url: str
    calls: tuple[_Call, ...]
    bindings: tuple[_Binding, ...]


def _run(
    plan: _Plan,
    proxy: Proxy,
    proxy_url: str,
    fixture: _Fixture | None,
    base_url: str,
) -> JsonObject:
    env = _child_env(plan, proxy_url, base_url)
    produced: dict[str, list[JsonValue]] = {}
    happy_seen: dict[str, int] = {}
    calls: list[JsonObject] = []
    with _stderr_log() as errlog:
        asyncio.run(_session(plan, env, fixture, proxy, produced, happy_seen, calls, errlog))
    return _pack({"calls": calls})


@contextmanager
def _stderr_log() -> Iterator[TextIO]:
    descriptor, name = tempfile.mkstemp(prefix="mcp4api-")
    os.close(descriptor)
    path = Path(name)
    try:
        with path.open("w+", encoding="utf-8") as handle:
            yield handle
    finally:
        path.unlink(missing_ok=True)


async def _session(
    plan: _Plan,
    env: dict[str, str],
    fixture: _Fixture | None,
    proxy: Proxy,
    produced: dict[str, list[JsonValue]],
    happy_seen: dict[str, int],
    calls: list[JsonObject],
    errlog: TextIO,
) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", plan.module],
        cwd=plan.generated,
        env=env,
    )
    try:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            _Session(read, write) as session,
        ):
            await negotiate_auto(session)
            listed = await session.list_tools()
            schemas = _output_schemas(listed.tools)
            for call in plan.calls:
                arguments = _with_bindings(call, plan.bindings, produced, happy_seen)
                calls.append(
                    await _one(session, call, arguments, schemas, fixture, proxy, plan, produced)
                )
                if not call.negative:
                    happy_seen[call.tool] = happy_seen.get(call.tool, 0) + 1
    except Exception as exc:
        pending = {call.id for call in plan.calls if call.id not in {item["id"] for item in calls}}
        for call in plan.calls:
            if call.id not in pending:
                continue
            calls.append(_crashed(call, str(exc) or exc.__class__.__name__))


class _Session(ClientSession):
    async def validate_tool_result(self, name: str, result: CallToolResult) -> None:
        return None


async def _one(
    session: _Session,
    call: _Call,
    arguments: JsonObject,
    schemas: dict[str, JsonObject | None],
    fixture: _Fixture | None,
    proxy: Proxy,
    plan: _Plan,
    produced: dict[str, list[JsonValue]],
) -> JsonObject:
    if fixture is not None:
        fixture.begin(
            call.tool,
            [_synthetic(step) for step in call.steps],
            negative=call.negative,
            hang=call.hang,
        )
    mark, blocked_at = proxy.snapshot()
    result, transport_error = await _call(session, call, arguments, plan.timeout_seconds)
    fixture_rows = fixture.take() if fixture is not None else []
    proxy_rows, blocked_hosts = proxy.since(mark, blocked_at)
    rows = fixture_rows if fixture is not None else proxy_rows
    message = "" if result is None else _message(result)
    blocked = bool(blocked_hosts) or "is not allowed" in message
    structured = result.structured_content if result is not None else None
    if not call.negative:
        produced.setdefault(call.tool, []).append(
            _json(structured) if isinstance(structured, dict) else None
        )
    output_schema = schemas.get(call.tool)
    return _judge(
        call,
        arguments,
        rows,
        result,
        transport_error,
        message,
        blocked,
        structured,
        output_schema,
        plan.upstream,
    )


async def _call(
    session: _Session,
    call: _Call,
    arguments: JsonObject,
    timeout: float,
) -> tuple[CallToolResult | None, str]:
    try:
        found = await session.call_tool(
            call.tool,
            _arguments(arguments),
            read_timeout_seconds=timeout + 5,
        )
    except Exception as exc:
        return None, str(exc) or exc.__class__.__name__
    if isinstance(found, CallToolResult):
        return found, ""
    return None, "protocol result was not a tool result"


def _judge(
    call: _Call,
    arguments: JsonObject,
    rows: list[Exchange],
    result: CallToolResult | None,
    transport_error: str,
    message: str,
    blocked: bool,
    structured: object,
    output_schema: JsonObject | None,
    upstream: str,
) -> JsonObject:
    assertions: list[JsonObject] = []
    timeout = _timed_out(transport_error, message) or any(row.hang for row in rows)
    missing = _missing_required(call, structured, rows)
    schema_field, schema_expected, schema_actual = _schema_problem(
        call, structured, output_schema, rows
    )
    if blocked:
        assertions.append(_assertion("transport", True, "tool result", "blocked"))
    elif timeout:
        detail = transport_error or message or "timeout"
        assertions.append(_assertion("transport", False, "tool result", detail))
    elif result is None and (schema_field or missing):
        assertions.append(_assertion("transport", True, "tool result", "tool result"))
    elif result is None or transport_error:
        detail = transport_error or message or "no tool result"
        assertions.append(_assertion("transport", False, "tool result", detail))
    else:
        assertions.append(_assertion("transport", True, "tool result", "tool result"))
    status_ok, status_expected, status_actual = _status(call, rows)
    assertions.append(_assertion("status", status_ok, status_expected, status_actual))
    if schema_field:
        assertions.append(_assertion("schema", False, schema_expected, schema_actual, schema_field))
    else:
        assertions.append(_assertion("schema", True, "schema", "schema"))
    if missing:
        assertions.append(_assertion("required", False, missing, "missing", missing))
    else:
        assertions.append(_assertion("required", True, "present", "present"))
    error_ok, error_actual = _error_shape(call, result, message, transport_error)
    assertions.append(_assertion("error-shape", error_ok, "isError result", error_actual))
    failed = _primary(assertions)
    evidence = _evidence(call, rows, upstream)
    body, upstream_status = _excerpt(rows, call.output_step, schema_field)
    return _pack(
        {
            "id": call.id,
            "tool": call.tool,
            "kind": call.kind,
            "arguments": arguments,
            "ok": failed == "",
            "blocked": blocked,
            "evidence": evidence,
            "failed": failed,
            "reason": _reason(assertions, failed),
            "assertions": assertions,
            "upstream_status": upstream_status,
            "body": body,
            "field": _field_of(assertions, failed),
            "message": message,
            "exchanges": [_exchange_json(row) for row in rows if not row.synthesized],
        }
    )


def _pack(payload: object) -> JsonObject:
    parsed = _json(payload)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _crashed(call: _Call, message: str) -> JsonObject:
    assertions = [
        _assertion("transport", False, "tool result", message),
        _assertion("status", False, "success status", "no response"),
        _assertion("schema", False, "schema", "no response"),
        _assertion("required", False, "present", "no response"),
        _assertion("error-shape", False, "isError result", message),
    ]
    return _pack(
        {
            "id": call.id,
            "tool": call.tool,
            "kind": call.kind,
            "arguments": call.arguments,
            "ok": False,
            "blocked": False,
            "evidence": "fixtures",
            "failed": "transport",
            "reason": message,
            "assertions": assertions,
            "upstream_status": None,
            "body": "",
            "field": "",
            "exchanges": [],
        }
    )


def _status(call: _Call, rows: list[Exchange]) -> tuple[bool, str, str]:
    if call.negative:
        if rows:
            return False, "input validation", f"upstream {rows[0].status}"
        return True, "input validation", "input validation"
    if not rows:
        return False, _expected_status(call), "no response"
    for index, row in enumerate(rows):
        expected = call.steps[index].success_status if index < len(call.steps) else 200
        if row.status != expected:
            return False, str(expected), str(row.status)
    return True, _expected_status(call), _expected_status(call)


def _schema_problem(
    call: _Call,
    structured: object,
    output_schema: JsonObject | None,
    rows: list[Exchange],
) -> tuple[str, str, str]:
    if call.negative:
        return "", "schema", "schema"
    if output_schema is not None and isinstance(structured, dict):
        problem = _schema_error(output_schema, structured)
        if problem is not None:
            return problem
    for index, row in enumerate(rows):
        if index >= len(call.steps):
            break
        problem = _schema_error(call.steps[index].response_schema, row.response)
        if problem is not None:
            return problem
    return "", "schema", "schema"


def _schema_error(schema: JsonObject, instance: object) -> tuple[str, str, str] | None:
    if not schema:
        return None
    try:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(instance),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except Exception as exc:
        return "schema", "valid schema", str(exc)
    for error in errors:
        if error.validator == "required":
            continue
        return _describe(error)
    return None


def _describe(error: ValidationError) -> tuple[str, str, str]:
    field_name = ".".join(str(part) for part in error.path) or "value"
    expected = error.validator_value
    if isinstance(expected, list):
        expected_text = ", ".join(str(item) for item in expected)
    elif isinstance(expected, str):
        expected_text = expected
    else:
        validator = error.validator
        expected_text = validator if isinstance(validator, str) else "schema"
    return field_name, expected_text, _kind(error.instance)


def _missing_required(call: _Call, structured: object, rows: list[Exchange]) -> str:
    if call.negative or not call.required:
        return ""
    sources: list[JsonObject] = []
    if isinstance(structured, dict):
        sources.append(_object(structured))
    step = call.output_step
    if 0 <= step < len(rows):
        response = rows[step].response
        if isinstance(response, dict):
            sources.append(response)
    if not sources:
        return ""
    for name in call.required:
        absent = name not in call.nullable
        if any(name not in source or (source[name] is None and absent) for source in sources):
            return name
    return ""


def _error_shape(
    call: _Call,
    result: CallToolResult | None,
    message: str,
    transport_error: str,
) -> tuple[bool, str]:
    if call.negative or (result is not None and result.is_error):
        if result is None or not result.is_error or not message.strip():
            return False, transport_error or "missing error result"
        if not call.negative:
            return False, message
        return True, "isError result"
    if transport_error:
        return False, transport_error
    return True, "isError result"


def _primary(assertions: list[JsonObject]) -> str:
    order = ("transport", "status", "schema", "required", "error-shape")
    failed = {item["name"] for item in assertions if item.get("ok") is not True}
    for name in order:
        if name in failed:
            return name
    return ""


def _reason(assertions: list[JsonObject], failed: str) -> str:
    for item in assertions:
        if item.get("name") != failed:
            continue
        field_name = item.get("field")
        expected = item.get("expected")
        actual = item.get("actual")
        if isinstance(field_name, str) and field_name and field_name not in {failed, "schema"}:
            return f"{field_name}: expected {expected}, got {actual}"
        if isinstance(expected, str) and isinstance(actual, str):
            return f"expected {expected}, got {actual}"
    return ""


def _field_of(assertions: list[JsonObject], failed: str) -> str:
    for item in assertions:
        if item.get("name") == failed:
            field_name = item.get("field")
            if isinstance(field_name, str):
                return field_name
    return ""


def _evidence(call: _Call, rows: list[Exchange], upstream: str) -> str:
    if call.negative:
        return "fixtures" if upstream == "fixture" else upstream
    if any(row.synthesized for row in rows) or not rows:
        return "synthesized"
    if upstream == "sandbox":
        return "sandbox"
    if upstream == "live":
        return "live"
    return "fixtures"


def _excerpt(rows: list[Exchange], output_step: int, field_name: str) -> tuple[str, int | None]:
    chosen: Exchange | None = None
    needle = field_name.rsplit(".", 1)[-1] if field_name else ""
    if needle:
        for row in rows:
            if _has_key(row.response, needle):
                chosen = row
                break
    if chosen is None and rows:
        index = output_step if 0 <= output_step < len(rows) else 0
        chosen = rows[index]
    if chosen is None:
        return "", None
    text = json.dumps(chosen.response, ensure_ascii=False, sort_keys=True)
    if len(text) > 500:
        text = text[:500]
    return text, chosen.status


def _has_key(value: JsonValue, name: str) -> bool:
    if isinstance(value, dict):
        if name in value:
            return True
        return any(_has_key(item, name) for item in value.values())
    if isinstance(value, list):
        return any(_has_key(item, name) for item in value)
    return False


def _assertion(
    name: str,
    ok: bool,
    expected: str,
    actual: str,
    field_name: str = "",
) -> JsonObject:
    return {
        "name": name,
        "ok": ok,
        "expected": expected,
        "actual": actual,
        "field": field_name,
    }


def _expected_status(call: _Call) -> str:
    if not call.steps:
        return "200"
    return str(call.steps[0].success_status)


def _exchange_json(row: Exchange) -> JsonObject:
    return _pack(
        {
            "method": row.method,
            "path": row.path,
            "query": row.query,
            "body": row.body,
            "status": row.status,
            "response": row.response,
        }
    )


def _synthetic(step: _Step) -> Exchange:
    return Exchange(
        "GET", "/", {}, None, step.synthetic_status, step.synthetic_body, hang=step.hang
    )


def _with_bindings(
    call: _Call,
    bindings: tuple[_Binding, ...],
    produced: dict[str, list[JsonValue]],
    happy_seen: dict[str, int],
) -> JsonObject:
    arguments = dict(call.arguments)
    if call.negative:
        return arguments
    index = happy_seen.get(call.tool, 0)
    for binding in bindings:
        if binding.tool != call.tool:
            continue
        values = produced.get(binding.from_tool, [])
        if not values:
            continue
        chosen = values[index] if index < len(values) else values[-1]
        if not isinstance(chosen, dict) or binding.field not in chosen:
            continue
        found = chosen[binding.field]
        if isinstance(found, (str, int, float, bool)) or found is None:
            arguments[binding.input] = found
    return arguments


def _output_schemas(tools: object) -> dict[str, JsonObject | None]:
    found: dict[str, JsonObject | None] = {}
    if not isinstance(tools, list):
        return found
    for tool in tools:
        name = getattr(tool, "name", None)
        schema = getattr(tool, "output_schema", None)
        if not isinstance(name, str):
            continue
        if isinstance(schema, dict):
            found[name] = {
                key: _json(value) for key, value in schema.items() if isinstance(key, str)
            }
        else:
            found[name] = None
    return found


def _message(result: CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _arguments(arguments: JsonObject) -> dict[str, str | int | float | bool | None]:
    payload: dict[str, str | int | float | bool | None] = {}
    for key, value in arguments.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
        else:
            payload[key] = json.dumps(value)
    return payload


def _child_env(plan: _Plan, proxy_url: str, base_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    env["all_proxy"] = proxy_url
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    env["MCP4API_ALLOWED_HOSTS"] = ",".join(plan.allowlist)
    env[plan.base_url_env] = base_url
    env[plan.timeout_env] = _number(plan.timeout_seconds)
    if plan.upstream == "fixture":
        for name in plan.redact_env_names:
            if not env.get(name):
                env[name] = "fixture"
    return env


def _load_cassettes(root: Path) -> dict[str, list[Exchange]]:
    found: dict[str, list[Exchange]] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = _exchange_list(loaded)
        if rows is not None:
            found[path.stem] = rows
    return found


def _exchange_list(loaded: object) -> list[Exchange] | None:
    if not isinstance(loaded, dict):
        return None
    raw = loaded.get("exchanges")
    if not isinstance(raw, list):
        return None
    rows: list[Exchange] = []
    for item in raw:
        parsed = _exchange(item)
        if parsed is None:
            return None
        rows.append(parsed)
    return rows


def _exchange(item: object) -> Exchange | None:
    if not isinstance(item, dict):
        return None
    method = item.get("method")
    path = item.get("path")
    status = item.get("status")
    if not isinstance(method, str) or not isinstance(path, str) or not isinstance(status, int):
        return None
    query = item.get("query")
    parsed_query: dict[str, str] = {}
    if isinstance(query, dict):
        for key, value in query.items():
            if isinstance(key, str) and isinstance(value, str):
                parsed_query[key] = value
    return Exchange(
        method.upper(),
        path,
        parsed_query,
        _json(item.get("body")),
        status,
        _json(item.get("response")),
        hang=item.get("hang") is True,
    )


def _matches(
    exchange: Exchange,
    method: str,
    path: str,
    query: dict[str, str],
    body: JsonValue,
) -> bool:
    return (
        exchange.method == method.upper()
        and exchange.path == path
        and exchange.query == query
        and exchange.body == body
    )


def _parse_plan(plan: object) -> _Plan:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    module = _str(plan, "module")
    generated = _str(plan, "generated")
    calls_raw = plan.get("calls")
    if not isinstance(calls_raw, list):
        raise ValueError("plan calls must be a list")
    bindings_raw = plan.get("bindings")
    bindings: list[_Binding] = []
    if isinstance(bindings_raw, list):
        for item in bindings_raw:
            if not isinstance(item, dict):
                continue
            bindings.append(
                _Binding(
                    _str(item, "tool"),
                    _str(item, "input"),
                    _str(item, "from_tool"),
                    _str(item, "field"),
                )
            )
    allow = plan.get("allowlist")
    hosts = [item for item in allow if isinstance(item, str)] if isinstance(allow, list) else []
    secrets = plan.get("redact_env_names")
    redact_env_names = (
        tuple(item for item in secrets if isinstance(item, str))
        if isinstance(secrets, list)
        else ()
    )
    timeout = plan.get("timeout_seconds")
    seconds = (
        float(timeout)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        else 10.0
    )
    return _Plan(
        module=module,
        generated=_resolve_generated(generated),
        base_url_env=_str(plan, "base_url_env"),
        timeout_env=_str(plan, "timeout_env"),
        redact_env_names=redact_env_names,
        timeout_seconds=seconds,
        allowlist=tuple(hosts or ["127.0.0.1"]),
        upstream=_str(plan, "upstream") or "fixture",
        upstream_url=_str(plan, "upstream_url"),
        calls=tuple(_parse_call(item) for item in calls_raw),
        bindings=tuple(bindings),
    )


def _parse_call(item: object) -> _Call:
    if not isinstance(item, dict):
        raise ValueError("call must be an object")
    steps_raw = item.get("steps")
    steps: list[_Step] = []
    if isinstance(steps_raw, list):
        for step in steps_raw:
            if isinstance(step, dict):
                steps.append(_parse_step(step))
    required_raw = item.get("required")
    required = (
        tuple(name for name in required_raw if isinstance(name, str))
        if isinstance(required_raw, list)
        else ()
    )
    nullable_raw = item.get("nullable")
    nullable = (
        tuple(name for name in nullable_raw if isinstance(name, str))
        if isinstance(nullable_raw, list)
        else ()
    )
    arguments = item.get("arguments")
    output_step = item.get("output_step")
    return _Call(
        id=_str(item, "id"),
        tool=_str(item, "tool"),
        kind=_str(item, "kind"),
        arguments=_object(arguments),
        negative=item.get("negative") is True,
        steps=tuple(steps),
        required=required,
        nullable=nullable,
        output_step=output_step if isinstance(output_step, int) else 0,
        hang=item.get("hang") is True,
    )


def _parse_step(item: JsonObject) -> _Step:
    schema = item.get("response_schema")
    status = item.get("success_status")
    synthetic_status = item.get("synthetic_status")
    return _Step(
        success_status=status if isinstance(status, int) else 200,
        response_schema=_object(schema),
        synthetic_status=synthetic_status if isinstance(synthetic_status, int) else 200,
        synthetic_body=_json(item.get("synthetic_body")),
        hang=item.get("hang") is True,
    )


def _relay(sock: socket.socket, proxy: Proxy) -> None:
    sock.settimeout(30)
    head, rest = _read_head(sock)
    if head is None:
        return
    request, headers, extra = head
    parts = request.split(" ")
    if len(parts) < 2:
        return
    method, target = parts[0], parts[1]
    if method.upper() == "CONNECT":
        host = target.split(":", 1)[0].lower()
        if not _allowed(proxy, host):
            proxy.note_block(host)
            _deny(sock)
            return
        _tunnel(sock, proxy, host, target)
        return
    parsed = urlsplit(target)
    host = (parsed.hostname or "").lower()
    if not _allowed(proxy, host):
        proxy.note_block(host)
        _deny(sock)
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    payload = _body_bytes(headers, extra + rest, sock)
    try:
        upstream = socket.create_connection((host, port), timeout=30)
    except OSError:
        _deny(sock)
        return
    proxy.note_dial(host)
    upstream.settimeout(30)
    try:
        origin = f"{method} {path} HTTP/1.1\r\n"
        forwarded = _forward_headers(headers, host, port)
        upstream.sendall(origin.encode() + forwarded + b"\r\n\r\n" + payload)
        status, response_headers, response_body = _read_response(upstream)
        sock.sendall(_restore(status, response_headers, response_body))
        query = {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
        proxy.note_exchange(
            Exchange(
                method.upper(),
                parsed.path or "/",
                query,
                _json_body(payload),
                status,
                _json_body(response_body),
            )
        )
    except OSError:
        _deny(sock)
    finally:
        upstream.close()


def _tunnel(sock: socket.socket, proxy: Proxy, host: str, target: str) -> None:
    host_name, separator, port_text = target.partition(":")
    port = int(port_text) if separator and port_text.isdigit() else 443
    try:
        upstream = socket.create_connection((host_name, port), timeout=30)
    except OSError:
        _deny(sock)
        return
    proxy.note_dial(host)
    try:
        sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        sockets = [sock, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                break
            for ready in readable:
                other = upstream if ready is sock else sock
                chunk = ready.recv(65536)
                if not chunk:
                    return
                other.sendall(chunk)
    except OSError:
        return
    finally:
        upstream.close()


def _allowed(proxy: Proxy, host: str) -> bool:
    return host.lower() in proxy.allow


def _deny(sock: socket.socket) -> None:
    try:
        sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    except OSError:
        return


def _read_head(
    sock: socket.socket,
) -> tuple[tuple[str, dict[str, str], bytes], bytes] | tuple[None, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > _HEADER_LIMIT:
            break
    if b"\r\n\r\n" not in data:
        return None, data
    head, _, rest = data.partition(b"\r\n\r\n")
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return (lines[0], headers, b""), rest


def _body_bytes(headers: dict[str, str], already: bytes, sock: socket.socket) -> bytes:
    length = headers.get("content-length")
    if length is None or not length.isdigit():
        return already
    needed = int(length)
    if needed > _BODY_LIMIT:
        needed = _BODY_LIMIT
    while len(already) < needed:
        chunk = sock.recv(min(4096, needed - len(already)))
        if not chunk:
            break
        already += chunk
    return already[:needed]


def _read_response(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
    head, rest = _read_head(sock)
    if head is None:
        return 502, {}, rest
    request, headers, _extra = head
    parts = request.split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 502
    body = _body_bytes(headers, rest, sock)
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _unchunk(body)
    return status, headers, body


def _unchunk(payload: bytes) -> bytes:
    pieces: list[bytes] = []
    rest = payload
    while rest:
        line, separator, tail = rest.partition(b"\r\n")
        if not separator:
            break
        size_text = line.split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError:
            break
        if size == 0:
            break
        pieces.append(tail[:size])
        rest = tail[size + 2 :]
    return b"".join(pieces)


def _restore(status: int, headers: dict[str, str], body: bytes) -> bytes:
    kept = [
        f"{name}: {value}"
        for name, value in headers.items()
        if name not in {"transfer-encoding", "connection", "keep-alive"}
    ]
    kept.append("Connection: close")
    if "content-length" not in headers:
        kept.append(f"Content-Length: {len(body)}")
    text = f"HTTP/1.1 {status} {_status_text(status)}\r\n" + "\r\n".join(kept) + "\r\n\r\n"
    return text.encode("iso-8859-1", errors="replace") + body


def _forward_headers(headers: dict[str, str], host: str, port: int) -> bytes:
    lines = [f"Host: {host}:{port}"]
    for name, value in headers.items():
        if name in {"host", "proxy-connection", "proxy-authorization", "connection"}:
            continue
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    return ("\r\n".join(lines)).encode("iso-8859-1", errors="replace")


def _json_body(payload: bytes) -> JsonValue:
    if not payload:
        return None
    try:
        return _json(json.loads(payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace")


def _json(value: object) -> JsonValue:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if isinstance(key, str):
                result[key] = _json(item)
        return result
    return str(value)


def _object_list(items: Sequence[object], label: str) -> list[JsonObject]:
    found: list[JsonObject] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"plan {label} must be objects")
        found.append(_object(item))
    return found


def _object(value: object) -> JsonObject:
    parsed = _json(value)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _str(value: Mapping[str, object], key: str) -> str:
    found = value.get(key)
    if isinstance(found, str):
        return found
    return ""


def _resolve_generated(raw: str) -> str:
    if not raw:
        raw = "generated"
    path = Path(raw)
    if path.is_absolute():
        return str(path)
    return str(Path(__file__).resolve().parent.parent / path)


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "value"


def _timed_out(transport_error: str, message: str) -> bool:
    text = f"{transport_error} {message}".lower()
    return "timed out" in text or "timeout" in text


def _number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def _status_text(status: int) -> str:
    reasons = {
        200: "OK",
        201: "Created",
        204: "No Content",
        400: "Bad Request",
        403: "Forbidden",
        500: "Error",
        502: "Bad Gateway",
    }
    return reasons.get(status, "OK")
