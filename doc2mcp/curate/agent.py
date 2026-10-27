from __future__ import annotations

import importlib.resources
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ValidationError

from doc2mcp import __version__
from doc2mcp.curate.manifest import (
    CurationMode,
    DetailDecision,
    Excluded,
    Manifest,
    Overrides,
    PlanDecision,
    PlanTool,
    Tool,
    Usage,
    _shield_doc,
    apply_overrides,
    body_schema,
    build_manifest,
    bundle_parameter,
    curation_hash,
    dump_manifest,
    endpoint_index_document,
    forced_exclusions,
    heuristic_tools,
    interpret_detail,
    interpret_plan,
    invalid_exclusions,
    load_manifest,
    load_overrides,
    namespace_error,
    validation_errors,
    zero_usage,
)
from doc2mcp.model import ApiSpec, Endpoint, JsonObject, JsonValue, bundle_schema, expect_json

if TYPE_CHECKING:
    from anthropic.types.beta import BetaTextBlockParam, BetaUserMessageParam

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_BUDGET_TOKENS = 200_000
DEFAULT_BUDGET_USD = 5.0
DEFAULT_MAX_TOOLS = 20
PLAN_LIMIT = 80
MAX_OUTPUT_TOKENS = 16_000
RESERVE_OUTPUT_TOKENS = 4_096
_FALLBACK = "Heuristic fallback:"
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 5.0, 0.2, 20.0),
    "claude-sonnet-5-5": (2.0, 2.5, 0.1, 10.0),
}
_INSTRUCTIONS = (
    "Do not write JSON Schema. "
    "Choose names, titles, descriptions, intents, annotations, parameters, fixed scalars, "
    "step bindings, output field paths, example arguments, and a short rationale. "
    "Bind with $input.name or $steps[n].body.field. "
    "A list tool returns the first page. "
    "The rationale is a short justification, not a chain of thought."
)


class CurationUsage(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CurationUpstream(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CurationOptions:
    model: str = DEFAULT_MODEL
    mode: CurationMode = "agent"
    max_tools: int = DEFAULT_MAX_TOOLS
    namespace: str = ""
    include_tags: tuple[str, ...] = ()
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    budget_usd: float = DEFAULT_BUDGET_USD
    dry_run: bool = False
    recurate: bool = False
    offline: bool = False


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int


@dataclass(frozen=True)
class ModelTurn:
    stop_reason: str
    payload: JsonObject | None
    usage: ModelUsage


@dataclass(frozen=True)
class CurationRequest:
    kind: Literal["plan", "detail"]
    system: str
    index: str
    group: str


@dataclass(frozen=True)
class CurateResult:
    manifest: Manifest | None
    report: str
    warnings: tuple[str, ...]


class CurationClient(Protocol):
    def count_tokens(self, request: CurationRequest) -> int: ...

    def complete(self, request: CurationRequest) -> ModelTurn: ...


class _TokenUsage(Protocol):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


def curate(
    out: Path,
    options: CurationOptions,
    *,
    client: CurationClient | None = None,
) -> CurateResult:
    _check_options(options)
    spec, raw = _load_spec(out)
    if options.dry_run and options.offline:
        raise CurationUsage("cannot estimate tokens offline")
    digest = curation_hash(
        raw,
        model=options.model,
        mode=options.mode,
        include_tags=options.include_tags,
        max_tools=options.max_tools,
        namespace=options.namespace,
        version=__version__,
    )
    if options.mode == "manual":
        return _manual(out, spec, options, digest)
    if options.mode == "heuristic":
        return _store_heuristic(out, spec, options, digest)
    if not options.recurate:
        cached = _matching_base(out, digest)
        if cached is not None:
            return _publish(out, spec, options, cached)
    if options.offline:
        raise CurationUsage("offline curation needs a matching tools.yaml or --curation heuristic")
    live = client if client is not None else _require_client(options.model)
    if options.dry_run:
        return CurateResult(
            manifest=None,
            report=_dry_run(spec, options, live),
            warnings=(),
        )
    base = _agent_manifest(spec, options, live, digest)
    return _publish(out, spec, options, base)


def request_body(request: CurationRequest) -> JsonObject:
    return {
        "messages": [
            {
                "content": [
                    {
                        "cache_control": {"type": "ephemeral"},
                        "text": request.index,
                        "type": "text",
                    },
                    {"text": request.group, "type": "text"},
                ],
                "role": "user",
            }
        ],
        "system": [
            {
                "cache_control": {"type": "ephemeral"},
                "text": request.system,
                "type": "text",
            }
        ],
    }


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    model: str,
) -> float:
    input_price, write_price, read_price, output_price = _PRICES.get(model, _PRICES[DEFAULT_MODEL])
    raw = (
        input_tokens * input_price
        + cache_write_tokens * write_price
        + cache_read_tokens * read_price
        + output_tokens * output_price
    ) / 1_000_000
    return round(raw, 6)


def system_prompt() -> str:
    criteria = (
        importlib.resources.files("doc2mcp.curate")
        .joinpath("criteria.md")
        .read_text(encoding="utf-8")
    )
    return criteria.rstrip() + "\n\n" + _INSTRUCTIONS + "\n"


class AnthropicCurationClient:
    def __init__(self, model: str) -> None:
        self.model = model

    def count_tokens(self, request: CurationRequest) -> int:
        try:
            return self._count(request)
        except CurationUpstream:
            raise
        except Exception as exc:
            raise CurationUpstream(str(exc) or exc.__class__.__name__) from exc

    def complete(self, request: CurationRequest) -> ModelTurn:
        try:
            return self._complete(request)
        except CurationUpstream:
            raise
        except Exception as exc:
            raise CurationUpstream(str(exc) or exc.__class__.__name__) from exc

    def _count(self, request: CurationRequest) -> int:
        from anthropic import Anthropic, transform_schema

        system, messages = _sdk_messages(request)
        schema = transform_schema(PlanDecision if request.kind == "plan" else DetailDecision)
        counted = Anthropic().beta.messages.count_tokens(
            betas=["server-side-fallback-2026-07-01"],
            messages=messages,
            model=self.model,
            output_config={
                "effort": "high",
                "format": {"schema": schema, "type": "json_schema"},
            },
            system=system,
            thinking={"type": "adaptive"},
        )
        return counted.input_tokens

    def _complete(self, request: CurationRequest) -> ModelTurn:
        from anthropic import Anthropic

        system, messages = _sdk_messages(request)
        model_type = PlanDecision if request.kind == "plan" else DetailDecision
        with Anthropic().beta.messages.stream(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=messages,
            model=self.model,
            output_config={"effort": "high"},
            output_format=model_type,
            system=system,
            thinking={"type": "adaptive"},
        ) as stream:
            final = stream.get_final_message()
        usage = _usage(final.usage)
        stop = final.stop_reason or ""
        parsed = final.parsed_output
        if stop != "end_turn" or not isinstance(parsed, BaseModel):
            return ModelTurn(stop_reason=stop or "unknown", payload=None, usage=usage)
        body = expect_json(parsed.model_dump(mode="json", by_alias=True))
        if not isinstance(body, dict):
            return ModelTurn(stop_reason=stop, payload=None, usage=usage)
        return ModelTurn(stop_reason=stop, payload=body, usage=usage)


def _check_options(options: CurationOptions) -> None:
    problem = namespace_error(options.namespace)
    if problem:
        raise CurationUsage(problem)
    if options.max_tools < 1:
        raise CurationUsage("max tools must be positive")
    if options.budget_tokens < 0 or options.budget_usd < 0:
        raise CurationUsage("budget must not be negative")
    if options.mode not in {"agent", "heuristic", "manual"}:
        raise CurationUsage("curation mode is not supported")


def _load_spec(out: Path) -> tuple[ApiSpec, bytes]:
    path = out / "endpoints.json"
    if not path.is_file():
        raise CurationUsage(f"not found: {path}")
    raw = path.read_bytes()
    try:
        spec = ApiSpec.model_validate_json(raw)
    except ValidationError as exc:
        raise CurationUsage("invalid endpoints.json") from exc
    return spec, raw


def _require_client(model: str) -> CurationClient:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CurationUsage("missing ANTHROPIC_API_KEY")
    return AnthropicCurationClient(model)


def _manual(out: Path, spec: ApiSpec, options: CurationOptions, digest: str) -> CurateResult:
    path = out / "tools.yaml"
    if not path.is_file():
        raise CurationUsage(f"not found: {path}")
    current = _read_manifest(path)
    cache = _read_manifest(out / ".doc2mcp" / "base-tools.yaml")
    base = cache if cache is not None else current
    if base is None:
        raise CurationUsage("invalid tools.yaml")
    if validation_errors(
        base,
        spec,
        max_tools=options.max_tools,
        include_tags=options.include_tags,
    ):
        raise CurationUsage("invalid tools.yaml")
    return _publish(out, spec, options, base, previous=_names(current))


def _store_heuristic(
    out: Path,
    spec: ApiSpec,
    options: CurationOptions,
    digest: str,
) -> CurateResult:
    tools, excluded = heuristic_tools(
        spec,
        [endpoint.id for endpoint in spec.endpoints],
        namespace=options.namespace,
        include_tags=options.include_tags,
    )
    base = build_manifest(
        spec,
        tools,
        excluded,
        namespace=options.namespace,
        mode="heuristic",
        model=options.model,
        digest=digest,
        usage=zero_usage(),
        fallback_tools=[],
        max_tools=options.max_tools,
    )
    return _publish(out, spec, options, base)


def _publish(
    out: Path,
    spec: ApiSpec,
    options: CurationOptions,
    base: Manifest,
    *,
    previous: set[str] | None = None,
) -> CurateResult:
    if previous is None:
        previous = _names(_read_manifest(out / "tools.yaml"))
    overrides = _read_overrides(out / "overrides.yaml")
    final, warnings = apply_overrides(base, overrides, spec)
    _write(out / ".doc2mcp" / "base-tools.yaml", dump_manifest(base))
    _write(out / "tools.yaml", dump_manifest(final))
    notes = list(warnings)
    if previous is not None:
        notes.extend(_name_diff(previous, {tool.name for tool in final.tools}))
    return CurateResult(manifest=final, report="", warnings=tuple(notes))


def _agent_manifest(
    spec: ApiSpec,
    options: CurationOptions,
    client: CurationClient,
    digest: str,
) -> Manifest:
    prompt = system_prompt()
    index = endpoint_index_document(spec, options.include_tags)
    budget = _Budget(
        tokens_left=options.budget_tokens, usd_left=options.budget_usd, usage=zero_usage()
    )
    caller = _Caller(client=client, budget=budget, model=options.model)
    forced = forced_exclusions(spec, options.include_tags)
    forced_ids = {item.endpoint_id for item in forced}
    eligible = [endpoint for endpoint in spec.endpoints if endpoint.id not in forced_ids]
    tools: list[Tool] = []
    excluded: list[Excluded] = list(forced)
    fallback: list[str] = []
    accepted: set[str] = set()
    for chunk in _chunks(eligible):
        planned, chunk_excluded, uncovered, failed = _plan_chunk(
            spec,
            chunk,
            options,
            caller,
            prompt,
            index,
        )
        if failed:
            added, _out = heuristic_tools(
                spec,
                [endpoint.id for endpoint in chunk],
                namespace=options.namespace,
                include_tags=options.include_tags,
                fallback=True,
                reserved_names=accepted,
            )
            tools.extend(added)
            fallback.extend(tool.name for tool in added)
            accepted.update(tool.name for tool in added)
            continue
        excluded.extend(chunk_excluded)
        for endpoint_id in uncovered:
            added, _out = heuristic_tools(
                spec,
                [endpoint_id],
                namespace=options.namespace,
                include_tags=options.include_tags,
                reserved_names=accepted,
            )
            tools.extend(added)
            accepted.update(tool.name for tool in added)
        for plan in planned:
            detail_tools, detail_excluded, detail_fallback = _detail_group(
                spec,
                plan,
                options,
                caller,
                prompt,
                index,
                accepted,
            )
            tools.extend(detail_tools)
            excluded.extend(detail_excluded)
            fallback.extend(detail_fallback)
            accepted.update(tool.name for tool in detail_tools)
    claimed = {step.endpoint_id for tool in tools for step in tool.steps}
    claimed.update(item.endpoint_id for item in excluded)
    missing = [endpoint.id for endpoint in spec.endpoints if endpoint.id not in claimed]
    if missing:
        added, still_out = heuristic_tools(
            spec,
            missing,
            namespace=options.namespace,
            include_tags=options.include_tags,
            reserved_names=accepted,
        )
        tools.extend(added)
        excluded.extend(still_out)
    return build_manifest(
        spec,
        tools,
        excluded,
        namespace=options.namespace,
        mode="agent",
        model=options.model,
        digest=digest,
        usage=budget.usage,
        fallback_tools=fallback,
        max_tools=options.max_tools,
    )


def _plan_chunk(
    spec: ApiSpec,
    chunk: list[Endpoint],
    options: CurationOptions,
    caller: _Caller,
    prompt: str,
    index: str,
) -> tuple[list[PlanTool], list[Excluded], list[str], bool]:
    request = CurationRequest(
        kind="plan",
        system=prompt,
        index=index,
        group=_plan_group(chunk, options.max_tools),
    )
    decision = _decision(caller, request, PlanDecision)
    if not isinstance(decision, PlanDecision):
        return [], [], [], True
    result = interpret_plan(spec, decision, options.include_tags)
    if result.errors:
        corrected = _decision(caller, _with_errors(request, list(result.errors)), PlanDecision)
        if not isinstance(corrected, PlanDecision):
            return [], [], [], True
        result = interpret_plan(spec, corrected, options.include_tags)
    return list(result.tools), list(result.excluded), list(result.uncovered), False


def _detail_group(
    spec: ApiSpec,
    plan: PlanTool,
    options: CurationOptions,
    caller: _Caller,
    prompt: str,
    index: str,
    accepted: set[str],
) -> tuple[list[Tool], list[Excluded], list[str]]:
    endpoints = [endpoint for endpoint in spec.endpoints if endpoint.id in set(plan.endpoint_ids)]
    request = CurationRequest(
        kind="detail",
        system=prompt,
        index=index,
        group=_detail_group_text(spec, endpoints, plan.name),
    )
    decision = _decision(caller, request, DetailDecision)
    allowed = set(plan.endpoint_ids)
    if not isinstance(decision, DetailDecision):
        added, _out = heuristic_tools(
            spec,
            list(plan.endpoint_ids),
            namespace=options.namespace,
            include_tags=options.include_tags,
            fallback=True,
            reserved_names=accepted,
        )
        return added, [], [tool.name for tool in added]
    result = interpret_detail(
        spec,
        decision,
        allowed=allowed,
        namespace=options.namespace,
        include_tags=options.include_tags,
        accepted_names=accepted,
    )
    if result.errors:
        corrected = _decision(caller, _with_errors(request, list(result.errors)), DetailDecision)
        if isinstance(corrected, DetailDecision):
            result = interpret_detail(
                spec,
                corrected,
                allowed=allowed,
                namespace=options.namespace,
                include_tags=options.include_tags,
                accepted_names=accepted | {tool.name for tool in result.tools},
            )
    if result.errors:
        return [], invalid_exclusions(sorted(allowed), result.errors[0]), []
    tools: list[Tool] = []
    excluded = list(result.excluded)
    fallback: list[str] = []
    for tool in result.tools:
        if tool.name in accepted or tool.name in {item.name for item in tools}:
            excluded.extend(
                invalid_exclusions([step.endpoint_id for step in tool.steps], "duplicate tool name")
            )
            continue
        tools.append(tool)
    covered = {step.endpoint_id for tool in tools for step in tool.steps}
    covered.update(item.endpoint_id for item in excluded)
    missing = [endpoint_id for endpoint_id in plan.endpoint_ids if endpoint_id not in covered]
    if missing:
        added, still_out = heuristic_tools(
            spec,
            missing,
            namespace=options.namespace,
            include_tags=options.include_tags,
            reserved_names=accepted | {tool.name for tool in tools},
        )
        tools.extend(added)
        excluded.extend(still_out)
    return tools, excluded, fallback


def _decision(
    caller: _Caller,
    request: CurationRequest,
    model: type[PlanDecision] | type[DetailDecision],
) -> PlanDecision | DetailDecision | None:
    payload = caller.call(request)
    if payload is None:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


def _dry_run(spec: ApiSpec, options: CurationOptions, client: CurationClient) -> str:
    forced = {item.endpoint_id for item in forced_exclusions(spec, options.include_tags)}
    eligible = [endpoint for endpoint in spec.endpoints if endpoint.id not in forced]
    chunks = _chunks(eligible)
    prompt = system_prompt()
    index = endpoint_index_document(spec, options.include_tags)
    input_tokens = 0
    for chunk in chunks:
        input_tokens += client.count_tokens(
            CurationRequest(
                kind="plan",
                system=prompt,
                index=index,
                group=_plan_group(chunk, options.max_tools),
            )
        )
        sample = chunk or eligible
        input_tokens += client.count_tokens(
            CurationRequest(
                kind="detail",
                system=prompt,
                index=index,
                group=_detail_group_text(spec, sample, "tool"),
            )
        )
    groups = min(options.max_tools, len(eligible))
    calls = len(chunks) + groups
    output_tokens = RESERVE_OUTPUT_TOKENS * calls
    usd = estimate_cost(input_tokens, output_tokens, 0, 0, options.model)
    lines = [
        f"input_tokens: {input_tokens}",
        f"output_tokens_estimate: {output_tokens}",
        f"calls: {calls}",
        f"usd_estimate: {usd}",
    ]
    if options.model not in _PRICES:
        lines.append("price_table: claude-opus-5-5")
    return "\n".join(lines) + "\n"


def _plan_group(endpoints: list[Endpoint], max_tools: int) -> str:
    ids = [endpoint.id for endpoint in endpoints]
    return (
        "Propose tools for this endpoint set. "
        f"The tool target is {max_tools}. "
        "Return one group per tool and exclude endpoints that are not worth a tool.\n"
        "Eligible endpoint ids:\n" + json.dumps(ids, indent=2)
    )


def _detail_group_text(spec: ApiSpec, endpoints: list[Endpoint], name: str) -> str:
    payload = [endpoint_detail(spec, endpoint) for endpoint in endpoints]
    return (
        f"Fill in the tool {name}. Use only these endpoints. Do not write JSON Schema.\n"
        + _shield_doc(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    )


def endpoint_detail(spec: ApiSpec, endpoint: Endpoint) -> JsonObject:
    parameters: list[JsonValue] = []
    for parameter in endpoint.parameters:
        row: JsonObject = {
            "description": _shield_doc(parameter.description or ""),
            "in": parameter.location,
            "name": parameter.name,
            "required": parameter.required,
            "schema": bundle_parameter(spec, parameter),
        }
        parameters.append(row)
    body = body_schema(spec, endpoint)
    responses: JsonObject = {}
    for code, response in endpoint.responses.items():
        if response.schema_ is None:
            continue
        responses[code] = bundle_schema(spec, response.schema_)
    tags: list[JsonValue] = []
    for tag in endpoint.tags or []:
        tags.append(_shield_doc(tag))
    return {
        "description": _shield_doc(endpoint.description or ""),
        "id": endpoint.id,
        "method": endpoint.method,
        "parameters": parameters,
        "request_body": body,
        "responses": responses,
        "summary": _shield_doc(endpoint.summary or ""),
        "tags": tags,
    }


def _chunks(endpoints: list[Endpoint]) -> list[list[Endpoint]]:
    ordered = sorted(endpoints, key=lambda endpoint: endpoint.id)
    if not ordered:
        return []
    if len(ordered) <= PLAN_LIMIT:
        return [ordered]
    grouped: dict[str, list[Endpoint]] = {}
    for endpoint in ordered:
        tags = sorted(tag.lower() for tag in endpoint.tags or [])
        grouped.setdefault(tags[0] if tags else "", []).append(endpoint)
    chunks: list[list[Endpoint]] = []
    for key in sorted(grouped):
        group = grouped[key]
        for start in range(0, len(group), PLAN_LIMIT):
            chunks.append(group[start : start + PLAN_LIMIT])
    return chunks


def _with_errors(request: CurationRequest, errors: list[str]) -> CurationRequest:
    lines = "\n".join(f"- {error}" for error in errors)
    return CurationRequest(
        kind=request.kind,
        system=request.system,
        index=request.index,
        group=request.group + "\nFix these errors and return the same shape:\n" + lines,
    )


def _matching_base(out: Path, digest: str) -> Manifest | None:
    cache = _read_manifest(out / ".doc2mcp" / "base-tools.yaml")
    if cache is not None and cache.curation.input_hash == digest:
        return cache
    if (out / "overrides.yaml").is_file():
        return None
    current = _read_manifest(out / "tools.yaml")
    if current is not None and current.curation.input_hash == digest:
        return current
    return None


def _read_manifest(path: Path) -> Manifest | None:
    if not path.is_file():
        return None
    try:
        return load_manifest(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None


def _read_overrides(path: Path) -> Overrides:
    if not path.is_file():
        return Overrides()
    try:
        return load_overrides(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise CurationUsage("invalid overrides.yaml") from exc


def _names(manifest: Manifest | None) -> set[str] | None:
    if manifest is None:
        return None
    return {tool.name for tool in manifest.tools}


def _name_diff(previous: set[str], current: set[str]) -> list[str]:
    added = sorted(current - previous)
    removed = sorted(previous - current)
    lines: list[str] = []
    if added:
        lines.append("tools added: " + ", ".join(added))
    if removed:
        lines.append("tools removed: " + ", ".join(removed))
    return lines


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _sdk_messages(
    request: CurationRequest,
) -> tuple[list[BetaTextBlockParam], list[BetaUserMessageParam]]:
    system: list[BetaTextBlockParam] = [
        {
            "cache_control": {"type": "ephemeral"},
            "text": request.system,
            "type": "text",
        }
    ]
    index_block: BetaTextBlockParam = {
        "cache_control": {"type": "ephemeral"},
        "text": request.index,
        "type": "text",
    }
    group_block: BetaTextBlockParam = {"text": request.group, "type": "text"}
    message: BetaUserMessageParam = {
        "content": [index_block, group_block],
        "role": "user",
    }
    return system, [message]


def _usage(usage: _TokenUsage) -> ModelUsage:
    return ModelUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        cache_write_input_tokens=usage.cache_creation_input_tokens or 0,
    )


class _Budget:
    def __init__(self, *, tokens_left: int, usd_left: float, usage: Usage) -> None:
        self.tokens_left = tokens_left
        self.usd_left = usd_left
        self.usage = usage

    def allows(self, input_tokens: int, model: str) -> bool:
        tokens = input_tokens + RESERVE_OUTPUT_TOKENS
        usd = estimate_cost(input_tokens, RESERVE_OUTPUT_TOKENS, 0, 0, model)
        return self.tokens_left >= tokens and self.usd_left + 1e-9 >= usd

    def charge(self, spent: ModelUsage, model: str) -> None:
        tokens = (
            spent.input_tokens
            + spent.output_tokens
            + spent.cache_read_input_tokens
            + spent.cache_write_input_tokens
        )
        self.tokens_left -= tokens
        cost = estimate_cost(
            spent.input_tokens,
            spent.output_tokens,
            spent.cache_read_input_tokens,
            spent.cache_write_input_tokens,
            model,
        )
        self.usd_left -= cost
        self.usage = Usage(
            input_tokens=self.usage.input_tokens + spent.input_tokens,
            output_tokens=self.usage.output_tokens + spent.output_tokens,
            cache_read_input_tokens=self.usage.cache_read_input_tokens
            + spent.cache_read_input_tokens,
            cache_write_input_tokens=self.usage.cache_write_input_tokens
            + spent.cache_write_input_tokens,
            estimated_usd=round(self.usage.estimated_usd + cost, 6),
        )


class _Caller:
    def __init__(self, *, client: CurationClient, budget: _Budget, model: str) -> None:
        self.client = client
        self.budget = budget
        self.model = model
        self.stopped = False

    def call(self, request: CurationRequest) -> JsonObject | None:
        payload = self._attempt(request)
        if payload is None and not self.stopped:
            payload = self._attempt(request)
        return payload

    def _attempt(self, request: CurationRequest) -> JsonObject | None:
        if self.stopped:
            return None
        counted = self.client.count_tokens(request)
        if not self.budget.allows(counted, self.model):
            self.stopped = True
            return None
        turn = self.client.complete(request)
        self.budget.charge(turn.usage, self.model)
        if turn.stop_reason != "end_turn" or turn.payload is None:
            return None
        return turn.payload
