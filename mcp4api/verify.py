from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

from mcp4api import __version__
from mcp4api.curate.manifest import Manifest, Tool, ToolInput, load_manifest, success_schema
from mcp4api.emit import EmitError, emit
from mcp4api.model import (
    ApiSpec,
    Endpoint,
    JsonObject,
    JsonSchema,
    JsonValue,
    expect_json,
)
from mcp4api.report import (
    Failure,
    FindingRow,
    Report,
    Summary,
    ToolRow,
    UsageRow,
    print_report,
    summary_line,
    write_report,
)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|AUTH)", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_ENV_ASSIGN = re.compile(
    r'^(?P<name>_BASE_URL_ENV|_TIMEOUT_ENV|_API_KEY_ENV|_TOKEN_ENV) = "(?P<value>[^"]*)"',
    re.MULTILINE,
)
_CONNECTION = (
    "connection refused",
    "connecterror",
    "nodename",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "all connection attempts failed",
    "failed to establish",
)
_REAL_EVIDENCE = frozenset({"fixtures", "sandbox", "live"})
_GAP = frozenset({"known gap", "blocked-by-allowlist"})
_WARNING = "WARNING: verification failed. "
_SUITE = Path(__file__).resolve().parent / "suite.py"


class VerifyUsage(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class VerifyUpstream(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class VerifyOptions:
    offline: bool = False
    live: bool = False
    sandbox: str = ""
    allow_hosts: tuple[str, ...] = ()
    allow_writes: bool = False
    on_failure: str = "exclude"
    deterministic: bool = False
    html: bool = False
    quiet: bool = False
    as_json: bool = False
    isolate: bool = False
    timeout_seconds: float = 10


@dataclass(frozen=True)
class VerifyOutcome:
    summary: str
    gaps: bool


def verify(out: Path, options: VerifyOptions) -> VerifyOutcome:
    """Call every generated tool and write the verification report.

    Args:
        out: Artifact root that contains tools.yaml, endpoints.json, and generated/.
        options: Verification flags.

    Returns:
        The summary line and whether any tool is a known gap.
    """
    if options.isolate and os.environ.get("MCP4API_ISOLATED") != "1":
        return _reexec(out, options)
    _guard(options)
    spec, manifest, manifest_sha = _load(out)
    _guard_target(manifest.api.base_url, options)
    _quiet_bytecode()
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    _emit(out)
    plan, skipped = _plan(out, spec, manifest, options)
    _write_suite(out, plan)
    results = _execute(out, plan)
    failure = _connection_failure(results, _upstream(options))
    if failure:
        raise VerifyUpstream(failure)
    return _finish(
        out,
        spec,
        manifest,
        manifest_sha,
        options,
        skipped,
        results,
        started,
        started_at,
    )


def record(out: Path, options: VerifyOptions) -> None:
    """Call every tool against a sandbox and write cassettes.

    Args:
        out: Artifact root that contains tools.yaml and endpoints.json.
        options: Must name a sandbox URL. A non-loopback host needs allow_hosts.

    Returns:
        None. Cassettes are written under tests/cassettes.
    """
    if not options.sandbox:
        raise VerifyUsage("record needs --sandbox")
    if options.live:
        raise VerifyUsage("use only one of --live and --sandbox")
    _guard(options)
    spec, manifest, _manifest_sha = _load(out)
    _guard_target(manifest.api.base_url, options)
    _quiet_bytecode()
    _emit(out)
    plan, _skipped = _plan(out, spec, manifest, options)
    _write_suite(out, plan)
    results = _execute(out, plan)
    failure = _connection_failure(results, "sandbox")
    if failure:
        raise VerifyUpstream(failure)
    _write_cassettes(out, results, _secret_values(_secret_envs(out)))


def run_saved(path: Path) -> int:
    """Run a verification configuration saved for an isolated process.

    Args:
        path: JSON file written by the parent process.

    Returns:
        0 when every tool verified, or 3 when a tool is a known gap.
    """
    try:
        options, out = _saved(path)
        outcome = verify(out, options)
    except VerifyUsage as exc:
        sys.stderr.write(f"{exc.message}\n")
        return 2
    except VerifyUpstream as exc:
        sys.stderr.write(f"{exc.message}\n")
        return 4
    return 3 if outcome.gaps else 0


def isolate_prefix() -> list[str] | None:
    """Return the Linux command that drops network access.

    Returns:
        The bwrap or unshare prefix, or None when this machine cannot isolate.
    """
    if sys.platform != "linux":
        return None
    bwrap = shutil.which("bwrap")
    if bwrap:
        return [
            bwrap,
            "--unshare-net",
            "--die-with-parent",
            "--bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
    unshare = shutil.which("unshare")
    if unshare:
        return [unshare, "--user", "--map-root-user", "--net", "--"]
    return None


def _quiet_bytecode() -> None:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _reexec(out: Path, options: VerifyOptions) -> VerifyOutcome:
    if sys.platform != "linux":
        raise VerifyUsage("--isolate is Linux-only")
    prefix = isolate_prefix()
    if prefix is None:
        raise VerifyUsage("--isolate needs bubblewrap or unshare")
    payload = _saved_payload(out, options)
    descriptor, name = tempfile.mkstemp(suffix=".json")
    os.close(descriptor)
    config = Path(name)
    try:
        config.write_text(json.dumps(payload), encoding="utf-8")
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from mcp4api.verify import run_saved\n"
            "sys.exit(run_saved(Path(sys.argv[1])))\n"
        )
        env = dict(os.environ)
        env["MCP4API_ISOLATED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [*prefix, sys.executable, "-c", script, str(config)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        config.unlink(missing_ok=True)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.returncode == 2:
        raise VerifyUsage(completed.stderr.strip() or "isolate failed")
    if completed.returncode == 4:
        raise VerifyUpstream(completed.stderr.strip() or "isolate failed")
    if completed.returncode not in {0, 3}:
        detail = completed.stderr.strip() or f"isolate exited {completed.returncode}"
        raise VerifyUsage(detail)
    return VerifyOutcome(summary=_summary_from(out), gaps=completed.returncode == 3)


def _saved_payload(out: Path, options: VerifyOptions) -> JsonObject:
    return {
        "out": str(out),
        "offline": options.offline,
        "live": options.live,
        "sandbox": options.sandbox,
        "allow_hosts": list(options.allow_hosts),
        "allow_writes": options.allow_writes,
        "on_failure": options.on_failure,
        "deterministic": options.deterministic,
        "html": options.html,
        "quiet": options.quiet,
        "as_json": options.as_json,
        "timeout_seconds": options.timeout_seconds,
    }


def _saved(path: Path) -> tuple[VerifyOptions, Path]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyUsage("cannot read isolate configuration") from exc
    if not isinstance(loaded, dict):
        raise VerifyUsage("isolate configuration must be an object")
    out = loaded.get("out")
    if not isinstance(out, str) or not out:
        raise VerifyUsage("isolate configuration is missing out")
    hosts = loaded.get("allow_hosts")
    allow_hosts = (
        tuple(item for item in hosts if isinstance(item, str)) if isinstance(hosts, list) else ()
    )
    timeout = loaded.get("timeout_seconds")
    seconds = (
        float(timeout)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        else 10.0
    )
    raw_sandbox = loaded.get("sandbox")
    sandbox = raw_sandbox if isinstance(raw_sandbox, str) else ""
    on_failure = loaded.get("on_failure")
    options = VerifyOptions(
        offline=loaded.get("offline") is True,
        live=loaded.get("live") is True,
        sandbox=sandbox,
        allow_hosts=allow_hosts,
        allow_writes=loaded.get("allow_writes") is True,
        on_failure=on_failure if isinstance(on_failure, str) else "exclude",
        deterministic=loaded.get("deterministic") is True,
        html=loaded.get("html") is True,
        quiet=loaded.get("quiet") is True,
        as_json=loaded.get("as_json") is True,
        isolate=True,
        timeout_seconds=seconds,
    )
    return options, Path(out)


def _summary_from(out: Path) -> str:
    path = out / "report" / "verification.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyUsage("isolate did not write a report") from exc
    report = Report.model_validate(loaded)
    return summary_line(report)


def _guard(options: VerifyOptions) -> None:
    if options.on_failure not in {"exclude", "keep"}:
        raise VerifyUsage("on-failure must be exclude or keep")
    if options.offline and (options.live or options.sandbox):
        raise VerifyUsage("offline verification cannot use record, --sandbox, or --live")
    if options.live and options.sandbox:
        raise VerifyUsage("use only one of --live and --sandbox")


def _guard_target(base_url: str, options: VerifyOptions) -> None:
    if options.live:
        _require_host(base_url)
    if options.sandbox:
        _require_host(options.sandbox)
    allowed = {item.lower() for item in options.allow_hosts}
    if options.live and not _permitted(_host(base_url), allowed):
        raise VerifyUsage(f"refusing {_host(base_url)} without --allow-host")
    if options.sandbox and not _permitted(_host(options.sandbox), allowed):
        raise VerifyUsage(f"refusing {_host(options.sandbox)} without --allow-host")


def _require_host(url: str) -> None:
    if not _host(url):
        raise VerifyUsage("sandbox URL needs a host")


def _permitted(host: str, allowed: set[str]) -> bool:
    return host in _LOOPBACK or host in allowed


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _load(out: Path) -> tuple[ApiSpec, Manifest, str]:
    tools_path = out / "tools.yaml"
    endpoints_path = out / "endpoints.json"
    try:
        tools_bytes = tools_path.read_bytes()
        spec = ApiSpec.model_validate_json(endpoints_path.read_text(encoding="utf-8"))
        manifest = load_manifest(tools_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise VerifyUsage(f"not found: {tools_path}") from exc
    return spec, manifest, hashlib.sha256(tools_bytes).hexdigest()


def _emit(out: Path) -> None:
    try:
        emit(out)
    except EmitError as exc:
        raise VerifyUsage(exc.message) from exc


def _plan(
    out: Path,
    spec: ApiSpec,
    manifest: Manifest,
    options: VerifyOptions,
) -> tuple[JsonObject, list[str]]:
    module, base_env, timeout_env, secrets = _layout(out / "generated")
    endpoints = {endpoint.id: endpoint for endpoint in spec.endpoints}
    ordered = _ordered(manifest.tools)
    skipped: list[str] = []
    calls: list[JsonValue] = []
    for tool in ordered:
        if _write_guard(tool, options):
            skipped.append(tool.name)
            continue
        calls.extend(_calls(tool, spec, endpoints))
    upstream = _upstream(options)
    upstream_url = ""
    if options.live:
        upstream_url = manifest.api.base_url
    elif options.sandbox:
        upstream_url = options.sandbox
    plan = _as_object(
        {
            "allowlist": _allowlist(manifest.api.base_url, options),
            "base_url_env": base_env,
            "bindings": _bindings(ordered),
            "calls": calls,
            "generated": "generated",
            "module": module,
            "secret_envs": list(secrets),
            "timeout_env": timeout_env,
            "timeout_seconds": options.timeout_seconds,
            "upstream": upstream,
            "upstream_url": upstream_url,
        }
    )
    return plan, skipped


def _upstream(options: VerifyOptions) -> str:
    if options.live:
        return "live"
    if options.sandbox:
        return "sandbox"
    return "fixture"


def _allowlist(base_url: str, options: VerifyOptions) -> list[str]:
    hosts = {"127.0.0.1", *(item.lower() for item in options.allow_hosts if item)}
    if options.live:
        host = _host(base_url)
        if host:
            hosts.add(host)
    if options.sandbox:
        host = _host(options.sandbox)
        if host:
            hosts.add(host)
    return sorted(hosts)


def _write_guard(tool: Tool, options: VerifyOptions) -> bool:
    if not options.live or options.allow_writes:
        return False
    return tool.annotations.destructiveHint or not tool.annotations.idempotentHint


def _ordered(tools: list[Tool]) -> list[Tool]:
    by_name = {tool.name: tool for tool in tools}
    waiting = {
        tool.name: {name for name in tool.verify.depends_on if name in by_name} for tool in tools
    }
    ready = [name for name, deps in waiting.items() if not deps]
    ordered: list[str] = []
    while ready:
        ready.sort(key=lambda name: _rank(by_name[name]))
        name = ready.pop(0)
        ordered.append(name)
        for other, deps in waiting.items():
            deps.discard(name)
            if not deps and other not in ordered and other not in ready:
                ready.append(other)
    if len(ordered) != len(tools):
        raise VerifyUsage("verification dependencies cycle")
    return [by_name[name] for name in ordered]


def _rank(tool: Tool) -> tuple[int, str]:
    if tool.annotations.readOnlyHint:
        group = 0
    elif tool.annotations.destructiveHint:
        group = 2
    else:
        group = 1
    return group, tool.name


def _calls(tool: Tool, spec: ApiSpec, endpoints: dict[str, Endpoint]) -> list[JsonValue]:
    required_args: JsonObject = {}
    all_args: JsonObject = {}
    required_names: list[str] = []
    for item in tool.inputs:
        value = _argument(item, endpoints, tool.verify.example_args)
        all_args[item.name] = value
        if item.required:
            required_names.append(item.name)
            required_args[item.name] = value
    steps = _steps(tool, spec, endpoints)
    required, nullable = _output_fields(tool.output.schema_)
    found: list[JsonValue] = [
        _call(
            tool,
            "required",
            required_args,
            negative=False,
            steps=steps,
            required=required,
            nullable=nullable,
        ),
        _call(
            tool, "all", all_args, negative=False, steps=steps, required=required, nullable=nullable
        ),
    ]
    if required_names:
        negative = dict(required_args)
        negative.pop(required_names[0], None)
        found.append(
            _call(
                tool,
                "negative",
                negative,
                negative=True,
                steps=steps,
                required=required,
                nullable=nullable,
            )
        )
    return found


def _call(
    tool: Tool,
    kind: str,
    arguments: JsonObject,
    *,
    negative: bool,
    steps: list[JsonValue],
    required: list[str],
    nullable: list[str],
) -> JsonObject:
    return _as_object(
        {
            "arguments": arguments,
            "hang": False,
            "id": f"{tool.name}:{kind}",
            "kind": kind,
            "negative": negative,
            "nullable": nullable,
            "output_step": tool.output.from_step,
            "required": required,
            "steps": steps,
            "tool": tool.name,
        }
    )


def _argument(item: ToolInput, endpoints: dict[str, Endpoint], examples: JsonObject) -> JsonValue:
    if item.name in examples:
        return examples[item.name]
    endpoint = endpoints.get(item.maps_to.endpoint_id)
    if endpoint is not None:
        documented = _documented(endpoint, item)
        if documented is not None:
            return documented
    return _synthesize(item.schema_)


def _documented(endpoint: Endpoint, item: ToolInput) -> JsonValue | None:
    place = item.maps_to.in_
    if place == "body":
        body = endpoint.request_body
        if body is None or not body.examples:
            return None
        example = body.examples[0]
        if isinstance(example, dict) and item.maps_to.param in example:
            return example[item.maps_to.param]
        return None
    for parameter in endpoint.parameters:
        if (
            parameter.name == item.maps_to.param
            and parameter.location == place
            and parameter.examples
        ):
            return parameter.examples[0]
    return None


def _steps(tool: Tool, spec: ApiSpec, endpoints: dict[str, Endpoint]) -> list[JsonValue]:
    found: list[JsonValue] = []
    for step in tool.steps:
        endpoint = endpoints.get(step.endpoint_id)
        if endpoint is None:
            raise VerifyUsage(f"missing endpoint {step.endpoint_id}")
        status = _success_status(endpoint)
        schema = success_schema(spec, endpoint)
        example = _response_example(endpoint)
        body = example if example is not None else _synthesize(schema)
        found.append(
            _as_object(
                {
                    "hang": False,
                    "response_schema": _schema_object(schema),
                    "success_status": status,
                    "synthetic_body": body,
                    "synthetic_status": status,
                }
            )
        )
    return found


def _success_status(endpoint: Endpoint) -> int:
    chosen: int | None = None
    for code in endpoint.responses:
        if not code.isdigit():
            continue
        status = int(code)
        if 200 <= status <= 299 and (chosen is None or status < chosen):
            chosen = status
    return 200 if chosen is None else chosen


def _response_example(endpoint: Endpoint) -> JsonValue | None:
    chosen: tuple[int, JsonValue] | None = None
    for code, response in endpoint.responses.items():
        if not code.isdigit() or not response.examples:
            continue
        status = int(code)
        if status < 200 or status > 299:
            continue
        if chosen is None or status < chosen[0]:
            chosen = (status, response.examples[0])
    if chosen is None:
        return None
    return chosen[1]


def _schema_object(schema: JsonSchema) -> JsonObject:
    if isinstance(schema, dict):
        return schema
    if schema:
        return {}
    return {"not": {}}


def _output_fields(schema: JsonSchema) -> tuple[list[str], list[str]]:
    if not isinstance(schema, dict):
        return [], []
    required_raw = schema.get("required")
    required = (
        [name for name in required_raw if isinstance(name, str)]
        if isinstance(required_raw, list)
        else []
    )
    properties = schema.get("properties")
    nullable: list[str] = []
    if isinstance(properties, dict):
        for name in required:
            prop = properties.get(name)
            if isinstance(prop, dict) and _allows_null(prop):
                nullable.append(name)
    return required, nullable


def _allows_null(schema: JsonObject) -> bool:
    kind = schema.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if not isinstance(options, list):
            continue
        for option in options:
            if isinstance(option, dict) and option.get("type") == "null":
                return True
    return False


def _bindings(tools: list[Tool]) -> list[JsonValue]:
    properties = {tool.name: _property_names(tool.output.schema_) for tool in tools}
    found: list[JsonValue] = []
    for tool in tools:
        for dep in tool.verify.depends_on:
            fields = properties.get(dep, set())
            for item in tool.inputs:
                field = ""
                if item.name in fields:
                    field = item.name
                elif item.name.endswith("_id") and "id" in fields:
                    field = "id"
                if not field:
                    continue
                found.append(
                    _as_object(
                        {"field": field, "from_tool": dep, "input": item.name, "tool": tool.name}
                    )
                )
    return found


def _property_names(schema: JsonSchema) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    if "$ref" in schema and isinstance(schema["$ref"], str):
        defs = schema.get("$defs")
        name = schema["$ref"].rsplit("/", 1)[-1]
        if isinstance(defs, dict):
            target = defs.get(name)
            if isinstance(target, dict):
                return _property_names(target)
    props = schema.get("properties")
    if not isinstance(props, dict):
        return set()
    return {key for key in props if isinstance(key, str)}


def _layout(generated: Path) -> tuple[str, str, str, tuple[str, ...]]:
    if not generated.is_dir():
        raise VerifyUsage(f"not found: {generated}")
    packages = [
        path.name
        for path in sorted(generated.iterdir())
        if path.is_dir() and (path / "client.py").is_file()
    ]
    if len(packages) != 1:
        raise VerifyUsage("generated server package was not found")
    text = (generated / packages[0] / "client.py").read_text(encoding="utf-8")
    found = {match.group("name"): match.group("value") for match in _ENV_ASSIGN.finditer(text)}
    base = found.get("_BASE_URL_ENV", "")
    timeout = found.get("_TIMEOUT_ENV", "")
    if not base or not timeout:
        raise VerifyUsage("generated client is missing base URL settings")
    secrets = tuple(found[name] for name in ("_API_KEY_ENV", "_TOKEN_ENV") if name in found)
    return packages[0], base, timeout, secrets


def _secret_envs(out: Path) -> tuple[str, ...]:
    try:
        _module, _base, _timeout, secrets = _layout(out / "generated")
    except VerifyUsage:
        return ()
    return secrets


def _synthesize(schema: JsonSchema) -> JsonValue:
    if isinstance(schema, bool):
        return {} if schema else None
    try:
        return _drawn(schema)
    except Exception:
        return _fallback(schema, schema)


def _drawn(schema: JsonObject) -> JsonValue:
    from hypothesis import HealthCheck, Phase, given, seed, settings
    from hypothesis_jsonschema import from_schema

    box: list[JsonValue] = []

    @seed(1)
    @settings(
        database=None,
        derandomize=True,
        deadline=None,
        max_examples=1,
        phases=[Phase.generate],
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.filter_too_much,
            HealthCheck.data_too_large,
        ],
    )
    @given(from_schema(schema))
    def draw(value: object) -> None:
        box.append(expect_json(value))

    draw()
    if not box:
        raise RuntimeError("synthesis produced no value")
    return box[0]


def _fallback(schema: JsonSchema, root: JsonSchema) -> JsonValue:
    if isinstance(schema, bool):
        return {} if schema else None
    if "$ref" in schema and isinstance(schema["$ref"], str):
        return _fallback(_resolve_ref(schema["$ref"], root), root)
    if "const" in schema:
        return schema["const"]
    enumerated = schema.get("enum")
    if isinstance(enumerated, list) and enumerated:
        return enumerated[0]
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if isinstance(kind, list):
        non_null = [item for item in kind if item != "null"]
        kind = non_null[0] if non_null else "null"
    if kind == "string":
        return _string_example(schema)
    if kind == "integer":
        minimum = schema.get("minimum")
        return minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else 1
    if kind == "number":
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            return minimum
        return 1
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    if kind == "array":
        items = schema.get("items")
        if isinstance(items, (dict, bool)):
            return [_fallback(items, root)]
        return []
    if kind == "object" or "properties" in schema:
        return _object_example(schema, root)
    for key in ("anyOf", "oneOf", "allOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            first = options[0]
            if isinstance(first, (dict, bool)):
                return _fallback(first, root)
    return "example"


def _object_example(schema: JsonObject, root: JsonSchema) -> JsonObject:
    properties = schema.get("properties")
    required = schema.get("required")
    names = (
        [name for name in required if isinstance(name, str)] if isinstance(required, list) else []
    )
    if not names and isinstance(properties, dict):
        names = [name for name in properties if isinstance(name, str)]
    result: JsonObject = {}
    if not isinstance(properties, dict):
        return result
    for name in names:
        prop = properties.get(name)
        if isinstance(prop, (dict, bool)):
            result[name] = _fallback(prop, root)
    return result


def _resolve_ref(ref: str, root: JsonSchema) -> JsonSchema:
    if not isinstance(root, dict):
        return {"type": "string"}
    defs = root.get("$defs")
    name = ref.rsplit("/", 1)[-1]
    if isinstance(defs, dict):
        target = defs.get(name)
        if isinstance(target, (dict, bool)):
            return target
    return {"type": "string"}


def _string_example(schema: JsonObject) -> str:
    fmt = schema.get("format")
    if fmt == "date-time":
        value = "2024-01-15T00:00:00Z"
    elif fmt == "date":
        value = "2024-01-15"
    elif fmt == "email":
        value = "a@example.test"
    elif fmt in {"uri", "uri-reference"}:
        value = "http://127.0.0.1/"
    elif fmt == "uuid":
        value = "00000000-0000-4000-8000-000000000000"
    else:
        value = "example"
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(value) < minimum:
        value = value * minimum
    return value


def _write_suite(out: Path, plan: JsonObject) -> None:
    tests = out / "tests"
    (tests / "cassettes").mkdir(parents=True, exist_ok=True)
    source = _SUITE.read_text(encoding="utf-8")
    (tests / "suite.py").write_text(source, encoding="utf-8", newline="\n")
    _write_json(tests / "plan.json", plan)
    (tests / "conftest.py").write_text(_CONFTEST, encoding="utf-8", newline="\n")
    (tests / "test_tools.py").write_text(_PYTEST, encoding="utf-8", newline="\n")


def _execute(out: Path, plan: JsonObject) -> JsonObject:
    module = _load_suite(out / "tests" / "suite.py")
    execute = getattr(module, "execute", None)
    if not callable(execute):
        raise VerifyUsage("verification suite is missing execute")
    found = execute(plan)
    parsed = expect_json(found)
    if not isinstance(parsed, dict):
        raise VerifyUsage("verification suite returned an unexpected result")
    return parsed


def _load_suite(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("mcp4api_emitted_suite", path)
    if spec is None or spec.loader is None:
        raise VerifyUsage("cannot load verification suite")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp4api_emitted_suite"] = module
    spec.loader.exec_module(module)
    return module


def _finish(
    out: Path,
    spec: ApiSpec,
    manifest: Manifest,
    manifest_sha: str,
    options: VerifyOptions,
    skipped: list[str],
    results: JsonObject,
    started: float,
    started_at: str,
) -> VerifyOutcome:
    rows, failures = _rows(manifest, skipped, results)
    gaps = [row.tool for row in rows if row.status in _GAP]
    summary = Summary(
        generated=len(rows),
        verified=sum(1 for row in rows if row.status == "verified"),
        synthesized=sum(1 for row in rows if row.status == "verified (synthesized)"),
        gaps=len(gaps),
        skipped=sum(1 for row in rows if row.status == "skipped: write-guard"),
    )
    report = Report(
        mcp4api=__version__,
        source_uri=spec.source.uri,
        source_sha256=spec.source.sha256,
        manifest_sha256=manifest_sha,
        model=manifest.curation.model,
        curation_mode=manifest.curation.mode,
        evidence=_evidence_mode(out, options),
        allowlist=_allowlist(manifest.api.base_url, options),
        summary=summary,
        tools=rows,
        failures=failures,
        excluded=gaps if options.on_failure == "exclude" else [],
        findings=[
            FindingRow(
                code=finding.code,
                severity=finding.severity,
                endpoint_id=finding.endpoint_id,
                pointer=finding.pointer,
                message=finding.message,
            )
            for finding in spec.findings
        ],
        usage=UsageRow(
            input_tokens=manifest.curation.usage.input_tokens,
            output_tokens=manifest.curation.usage.output_tokens,
            cache_read_input_tokens=manifest.curation.usage.cache_read_input_tokens,
            cache_write_input_tokens=manifest.curation.usage.cache_write_input_tokens,
            estimated_usd=manifest.curation.usage.estimated_usd,
        ),
        started_at=None if options.deterministic else started_at,
        duration_ms=None if options.deterministic else int((time.perf_counter() - started) * 1000),
    )
    write_report(
        report, out / "report", html_report=options.html, deterministic=options.deterministic
    )
    if not options.as_json:
        print_report(report, quiet=options.quiet)
    _apply_failure(out, options, gaps)
    return VerifyOutcome(summary=summary_line(report), gaps=bool(gaps))


def _apply_failure(out: Path, options: VerifyOptions, gaps: list[str]) -> None:
    if not gaps:
        return
    try:
        if options.on_failure == "keep":
            emit(out, description_prefixes=dict.fromkeys(gaps, _WARNING))
            return
        emit(out, skip_tools=frozenset(gaps))
    except EmitError as exc:
        raise VerifyUsage(exc.message) from exc


def _evidence_mode(out: Path, options: VerifyOptions) -> str:
    if options.live:
        return "live"
    if options.sandbox:
        return "sandbox"
    cassettes = out / "tests" / "cassettes"
    if cassettes.is_dir() and any(cassettes.glob("*.json")):
        return "fixtures"
    return "synthesized"


def _rows(
    manifest: Manifest,
    skipped: list[str],
    results: JsonObject,
) -> tuple[list[ToolRow], list[Failure]]:
    by_tool: dict[str, list[JsonObject]] = {}
    for call in _result_calls(results):
        name = call.get("tool")
        if isinstance(name, str):
            by_tool.setdefault(name, []).append(call)
    skipped_names = set(skipped)
    rows: list[ToolRow] = []
    failures: list[Failure] = []
    for tool in _ordered(manifest.tools):
        calls = by_tool.get(tool.name, [])
        if tool.name in skipped_names:
            rows.append(_tool_row(tool, "skipped: write-guard", "", "", 0, "none"))
            continue
        status = _tool_status(calls)
        failed, reason = _first_failure(calls)
        rows.append(_tool_row(tool, status, failed, reason, len(calls), _evidence_label(calls)))
        if status in _GAP:
            failures.extend(_failure(call) for call in calls if call.get("ok") is not True)
    return rows, failures


def _tool_row(
    tool: Tool, status: str, failed: str, reason: str, calls: int, evidence: str
) -> ToolRow:
    return ToolRow(
        tool=tool.name,
        endpoints=[step.endpoint_id for step in tool.steps],
        status=status,
        failed=failed,
        reason=reason,
        calls=calls,
        evidence=evidence,
    )


def _tool_status(calls: list[JsonObject]) -> str:
    if any(call.get("blocked") is True for call in calls):
        return "blocked-by-allowlist"
    if calls and all(call.get("ok") is True for call in calls):
        happy = [call for call in calls if call.get("kind") != "negative"]
        if any(call.get("evidence") in _REAL_EVIDENCE for call in happy):
            return "verified"
        return "verified (synthesized)"
    return "known gap"


def _first_failure(calls: list[JsonObject]) -> tuple[str, str]:
    for call in calls:
        if call.get("ok") is True:
            continue
        return _text(call.get("failed")), _text(call.get("reason"))
    return "", ""


def _evidence_label(calls: list[JsonObject]) -> str:
    labels = {call.get("evidence") for call in calls}
    for name in ("live", "sandbox", "fixtures", "synthesized"):
        if name in labels:
            return name
    return "none"


def _failure(call: JsonObject) -> Failure:
    assertion = _text(call.get("failed"))
    expected, actual = _assertion_values(call, assertion)
    status = call.get("upstream_status")
    return Failure(
        tool=_text(call.get("tool")),
        call=_text(call.get("id")),
        arguments=_object(call.get("arguments")),
        assertion=assertion,
        expected=expected,
        actual=actual,
        field=_text(call.get("field")),
        upstream_status=status
        if isinstance(status, int) and not isinstance(status, bool)
        else None,
        body=_text(call.get("body")),
    )


def _assertion_values(call: JsonObject, name: str) -> tuple[str, str]:
    raw = call.get("assertions")
    if not isinstance(raw, list):
        return "", ""
    for item in raw:
        if isinstance(item, dict) and item.get("name") == name:
            return _text(item.get("expected")), _text(item.get("actual"))
    return "", ""


def _result_calls(results: JsonObject) -> list[JsonObject]:
    raw = results.get("calls")
    if not isinstance(raw, list):
        raise VerifyUsage("verification suite returned no calls")
    found: list[JsonObject] = []
    for item in raw:
        if isinstance(item, dict):
            found.append(item)
    return found


def _connection_failure(results: JsonObject, upstream: str) -> str:
    if upstream == "fixture":
        return ""
    for call in _result_calls(results):
        if call.get("failed") != "transport":
            continue
        reason = _text(call.get("reason"))
        lowered = reason.lower()
        if any(token in lowered for token in _CONNECTION):
            return reason or "upstream connection failed"
    return ""


def _write_cassettes(out: Path, results: JsonObject, secrets: list[str]) -> None:
    grouped: dict[str, list[JsonValue]] = {}
    for call in _result_calls(results):
        if call.get("kind") == "negative":
            continue
        tool = call.get("tool")
        exchanges = call.get("exchanges")
        if not isinstance(tool, str) or not isinstance(exchanges, list):
            continue
        grouped.setdefault(tool, []).extend(item for item in exchanges if isinstance(item, dict))
    directory = out / "tests" / "cassettes"
    directory.mkdir(parents=True, exist_ok=True)
    for tool, rows in grouped.items():
        cleaned = _scrub(rows, secrets)
        if not isinstance(cleaned, list):
            continue
        _write_json(directory / f"{tool}.json", {"exchanges": cleaned})


def _secret_values(names: tuple[str, ...]) -> list[str]:
    wanted = set(names)
    found: list[str] = []
    for name, value in os.environ.items():
        if (name in wanted or _SECRET_NAME.search(name)) and len(value) >= 4:
            found.append(value)
    return sorted(set(found), key=len, reverse=True)


def _scrub(value: JsonValue, secrets: list[str]) -> JsonValue:
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            result[_scrub_text(key, secrets)] = _scrub(item, secrets)
        return result
    return value


def _scrub_text(value: str, secrets: list[str]) -> str:
    text = value
    for secret in secrets:
        text = text.replace(secret, "***")
    return _BEARER.sub("Bearer ***", text)


def _write_json(path: Path, value: JsonObject) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _as_object(value: object) -> JsonObject:
    parsed = expect_json(value)
    if isinstance(parsed, dict):
        return parsed
    raise VerifyUsage("expected an object")


def _object(value: object) -> JsonObject:
    parsed = expect_json(value) if value is not None else {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


_CONFTEST = """\
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def results() -> dict[str, object]:
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    from suite import execute

    loaded: object = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    found = execute(loaded)
    if not isinstance(found, dict):
        raise TypeError("results must be an object")
    return found
"""


_PYTEST = """\
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _ids() -> list[str]:
    path = Path(__file__).resolve().parent / "plan.json"
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return []
    calls = loaded.get("calls")
    if not isinstance(calls, list):
        return []
    found: list[str] = []
    for call in calls:
        if isinstance(call, dict):
            name = call.get("id")
            if isinstance(name, str):
                found.append(name)
    return found


def test_suite_loaded(results: dict[str, object]) -> None:
    assert isinstance(results.get("calls"), list)


@pytest.mark.parametrize("call_id", _ids() or ["none"])
def test_call(results: dict[str, object], call_id: str) -> None:
    if call_id == "none":
        return
    calls = results.get("calls")
    assert isinstance(calls, list)
    for call in calls:
        if isinstance(call, dict) and call.get("id") == call_id:
            assert call.get("ok") is True
            return
    raise AssertionError(call_id)
"""
