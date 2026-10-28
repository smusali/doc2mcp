from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol, TypedDict, runtime_checkable

import typer

from mcp4api import __version__
from mcp4api.curate.agent import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_BUDGET_USD,
    DEFAULT_MAX_TOOLS,
    DEFAULT_MODEL,
)
from mcp4api.curate.models import CurationMode
from mcp4api.errors import ExitCode, GapError, Mcp4apiError, UsageError
from mcp4api.verify import OnFailure

if TYPE_CHECKING:
    from mcp4api.verify import VerifyOptions

_DESCRIPTION = (
    "Turn API docs into a working MCP server, "
    "then verify every generated tool against the real API."
)
_STAGES = frozenset({"ingest", "curate", "emit", "verify", "record"})
_ENDPOINTS = "endpoints.json"
_TOOLS = "tools.yaml"
_GENERATED = "generated"
_LOG = logging.getLogger("mcp4api")

_Out = Annotated[
    Path | None,
    typer.Option("--out", help="Directory that receives stage artifacts."),
]
_Json = Annotated[bool, typer.Option("--json", help="Print errors as a JSON object.")]
_Quiet = Annotated[bool, typer.Option("--quiet", help="Show errors only.")]
_Verbose = Annotated[bool, typer.Option("--verbose", help="Show debug logs.")]
_Offline = Annotated[bool, typer.Option("--offline", help="Read local files only.")]
_AllowRefHost = Annotated[
    list[str] | None,
    typer.Option("--allow-ref-host", help="Host allowed for remote reference fetches."),
]
_Model = Annotated[str, typer.Option("--model", help="Claude model used for agent curation.")]
_BudgetTokens = Annotated[
    int,
    typer.Option("--budget-tokens", help="Maximum tokens for agent curation."),
]
_BudgetUsd = Annotated[
    float,
    typer.Option("--budget-usd", help="Maximum estimated USD for agent curation."),
]
_Curation = Annotated[CurationMode, typer.Option("--curation", help="Curation mode.")]
_MaxTools = Annotated[
    int,
    typer.Option("--max-tools", help="Tool-count target. Exceeding it requires a rationale."),
]
_Namespace = Annotated[str, typer.Option("--namespace", help="Prefix added to each tool name.")]
_IncludeTag = Annotated[
    list[str] | None,
    typer.Option("--include-tag", help="Tag or category to keep instead of excluding."),
]
_DryRun = Annotated[
    bool,
    typer.Option("--dry-run", help="Print a token and cost estimate without a paid call."),
]
_Recurate = Annotated[
    bool,
    typer.Option("--recurate", help="Curate again even when the input hash matches."),
]
_Force = Annotated[
    bool,
    typer.Option("--force", help="Overwrite generated files that were edited."),
]
_AllowHost = Annotated[
    list[str] | None,
    typer.Option("--allow-host", help="Host the verification proxy may dial."),
]
_OnFailure = Annotated[
    OnFailure,
    typer.Option(
        "--on-failure",
        help="exclude drops a failed tool. keep warns in its description.",
    ),
]
_Sandbox = Annotated[
    str,
    typer.Option(
        "--sandbox",
        help="Sandbox base URL for record, or for verify instead of fixtures.",
    ),
]
_Deterministic = Annotated[
    bool,
    typer.Option("--deterministic", help="Omit timestamps and durations from the report."),
]
_Live = Annotated[bool, typer.Option("--live", help="Call the spec server instead of fixtures.")]
_AllowWrites = Annotated[
    bool,
    typer.Option("--allow-writes", help="Call destructive and non-idempotent tools during --live."),
]
_Isolate = Annotated[
    bool,
    typer.Option("--isolate", help="Run verification in a Linux network namespace."),
]
_Html = Annotated[bool, typer.Option("--html", help="Also write report/verification.html.")]
_Spec = Annotated[str, typer.Argument(help="OpenAPI file path or URL.")]


class ErrorReport(TypedDict):
    ok: bool
    exit_code: int
    error: str


@dataclass(frozen=True)
class RunOptions:
    out: Path
    as_json: bool
    quiet: bool
    verbose: bool
    offline: bool
    allow_ref_hosts: tuple[str, ...]
    model: str
    budget_tokens: int
    budget_usd: float
    curation: CurationMode
    max_tools: int
    namespace: str
    include_tags: tuple[str, ...]
    dry_run: bool
    recurate: bool
    force: bool
    allow_hosts: tuple[str, ...]
    on_failure: OnFailure
    sandbox: str
    deterministic: bool
    live: bool
    allow_writes: bool
    isolate: bool
    html: bool


@runtime_checkable
class _ParserError(Protocol):
    exit_code: int

    def show(self) -> None: ...


def main(argv: list[str] | None = None) -> int:
    """Run the primary command or one stage.

    Args:
        argv: Arguments to parse. Defaults to the process arguments.

    Returns:
        Process exit code. 0 means every generated tool verified. 1 is an
        internal error. 2 is a usage or configuration error. 3 means at least
        one tool is a known gap. 4 is an ingest, network, model, or upstream
        API failure.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        app(args=_normalize_argv(args), standalone_mode=False, prog_name="mcp4api")
    except GapError as exc:
        if not exc.printed or _json_requested(args):
            _emit_error(exc.message, exc.exit_code, as_json=_json_requested(args))
        return exc.exit_code
    except Mcp4apiError as exc:
        _emit_error(exc.message, exc.exit_code, as_json=_json_requested(args))
        return exc.exit_code
    except typer.TyperException as exc:
        return _parser_exit(exc)
    except Exception as exc:
        _emit_error(
            str(exc) or exc.__class__.__name__,
            ExitCode.INTERNAL,
            as_json=_json_requested(args),
        )
        return ExitCode.INTERNAL
    return ExitCode.OK


def _print_version(value: bool) -> None:
    if not value:
        return
    typer.echo(f"mcp4api {__version__}")
    raise typer.Exit(code=ExitCode.OK)


app = typer.Typer(
    add_completion=False,
    help=_DESCRIPTION,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is None:
        raise UsageError("missing spec")


@app.command("run", hidden=True)
def _run(
    ctx: typer.Context,
    spec: _Spec,
    out: _Out = None,
    as_json: _Json = False,
    quiet: _Quiet = False,
    verbose: _Verbose = False,
    offline: _Offline = False,
    allow_ref_host: _AllowRefHost = None,
    model: _Model = DEFAULT_MODEL,
    budget_tokens: _BudgetTokens = DEFAULT_BUDGET_TOKENS,
    budget_usd: _BudgetUsd = DEFAULT_BUDGET_USD,
    curation: _Curation = CurationMode.AGENT,
    max_tools: _MaxTools = DEFAULT_MAX_TOOLS,
    namespace: _Namespace = "",
    include_tag: _IncludeTag = None,
    dry_run: _DryRun = False,
    recurate: _Recurate = False,
    force: _Force = False,
    allow_host: _AllowHost = None,
    on_failure: _OnFailure = OnFailure.EXCLUDE,
    sandbox: _Sandbox = "",
    deterministic: _Deterministic = False,
    live: _Live = False,
    allow_writes: _AllowWrites = False,
    isolate: _Isolate = False,
    html: _Html = False,
) -> None:
    options = _settings(
        ctx,
        out=out,
        as_json=as_json,
        quiet=quiet,
        verbose=verbose,
        offline=offline,
        allow_ref_host=allow_ref_host,
        model=model,
        budget_tokens=budget_tokens,
        budget_usd=budget_usd,
        curation=curation,
        max_tools=max_tools,
        namespace=namespace,
        include_tag=include_tag,
        dry_run=dry_run,
        recurate=recurate,
        force=force,
        allow_host=allow_host,
        on_failure=on_failure,
        sandbox=sandbox,
        deterministic=deterministic,
        live=live,
        allow_writes=allow_writes,
        isolate=isolate,
        html=html,
    )
    _ingest(spec, options)
    _curate(options)
    if options.dry_run:
        return
    _emit(options)
    _verify(options)


@app.command(help="Read an OpenAPI file path or URL and write endpoints.json.")
def ingest(
    ctx: typer.Context,
    spec: _Spec,
    out: _Out = None,
    as_json: _Json = False,
    quiet: _Quiet = False,
    verbose: _Verbose = False,
    offline: _Offline = False,
    allow_ref_host: _AllowRefHost = None,
) -> None:
    _ingest(
        spec,
        _settings(
            ctx,
            out=out,
            as_json=as_json,
            quiet=quiet,
            verbose=verbose,
            offline=offline,
            allow_ref_host=allow_ref_host,
        ),
    )


@app.command(help="Read endpoints.json and write tools.yaml.")
def curate(
    ctx: typer.Context,
    out: _Out = None,
    as_json: _Json = False,
    quiet: _Quiet = False,
    verbose: _Verbose = False,
    offline: _Offline = False,
    model: _Model = DEFAULT_MODEL,
    budget_tokens: _BudgetTokens = DEFAULT_BUDGET_TOKENS,
    budget_usd: _BudgetUsd = DEFAULT_BUDGET_USD,
    curation: _Curation = CurationMode.AGENT,
    max_tools: _MaxTools = DEFAULT_MAX_TOOLS,
    namespace: _Namespace = "",
    include_tag: _IncludeTag = None,
    dry_run: _DryRun = False,
    recurate: _Recurate = False,
) -> None:
    _curate(
        _settings(
            ctx,
            out=out,
            as_json=as_json,
            quiet=quiet,
            verbose=verbose,
            offline=offline,
            model=model,
            budget_tokens=budget_tokens,
            budget_usd=budget_usd,
            curation=curation,
            max_tools=max_tools,
            namespace=namespace,
            include_tag=include_tag,
            dry_run=dry_run,
            recurate=recurate,
        )
    )


@app.command(help="Read tools.yaml and endpoints.json and write generated/.")
def emit(
    ctx: typer.Context,
    out: _Out = None,
    as_json: _Json = False,
    quiet: _Quiet = False,
    verbose: _Verbose = False,
    force: _Force = False,
) -> None:
    options = _settings(ctx, out=out, as_json=as_json, quiet=quiet, verbose=verbose, force=force)
    _require_file(options.out / _TOOLS)
    _require_file(options.out / _ENDPOINTS)
    _emit(options)


@app.command(help="Call every generated tool and write report/.")
def verify(
    ctx: typer.Context,
    out: _Out = None,
    as_json: _Json = False,
    quiet: _Quiet = False,
    verbose: _Verbose = False,
    offline: _Offline = False,
    allow_host: _AllowHost = None,
    on_failure: _OnFailure = OnFailure.EXCLUDE,
    sandbox: _Sandbox = "",
    deterministic: _Deterministic = False,
    live: _Live = False,
    allow_writes: _AllowWrites = False,
    isolate: _Isolate = False,
    html: _Html = False,
) -> None:
    options = _settings(
        ctx,
        out=out,
        as_json=as_json,
        quiet=quiet,
        verbose=verbose,
        offline=offline,
        allow_host=allow_host,
        on_failure=on_failure,
        sandbox=sandbox,
        deterministic=deterministic,
        live=live,
        allow_writes=allow_writes,
        isolate=isolate,
        html=html,
    )
    _require_dir(options.out / _GENERATED)
    _require_file(options.out / _ENDPOINTS)
    _require_file(options.out / _TOOLS)
    _verify(options)


@app.command(help="Record cassettes against a sandbox URL.")
def record(
    ctx: typer.Context,
    out: _Out = None,
    as_json: _Json = False,
    quiet: _Quiet = False,
    verbose: _Verbose = False,
    allow_host: _AllowHost = None,
    sandbox: _Sandbox = "",
) -> None:
    _record(
        _settings(
            ctx,
            out=out,
            as_json=as_json,
            quiet=quiet,
            verbose=verbose,
            allow_host=allow_host,
            sandbox=sandbox,
        )
    )


def _normalize_argv(argv: list[str]) -> list[str]:
    values, flags = _flag_sets()
    equals = tuple(name + "=" for name in values)
    hoisted: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            rest.extend(argv[index:])
            break
        if token in values:
            if index + 1 >= len(argv):
                hoisted.append(token)
                index += 1
                continue
            hoisted.extend((token, argv[index + 1]))
            index += 2
            continue
        if token.startswith(equals):
            hoisted.append(token)
            index += 1
            continue
        if token in flags:
            hoisted.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    if not rest:
        return hoisted
    if rest[0] in _STAGES:
        command, positionals = rest[0], rest[1:]
    elif rest[0].startswith("-"):
        return [*hoisted, *rest]
    else:
        command, positionals = "run", rest
    return [command, *hoisted, *positionals]


def _flag_sets() -> tuple[frozenset[str], frozenset[str]]:
    command = typer.main.get_command(app)
    values: set[str] = set()
    flags: set[str] = set()
    grouped = getattr(command, "commands", {})
    commands = [command, *grouped.values()]
    for item in commands:
        for param in item.params:
            opts = getattr(param, "opts", None) or ()
            names = [name for name in opts if name.startswith("--")]
            if not names:
                continue
            if getattr(param, "is_flag", False):
                flags.update(names)
            else:
                values.update(names)
    return frozenset(values), frozenset(flags)


def _settings(
    ctx: typer.Context,
    *,
    out: Path | None = None,
    as_json: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    offline: bool = False,
    allow_ref_host: list[str] | None = None,
    model: str = DEFAULT_MODEL,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    budget_usd: float = DEFAULT_BUDGET_USD,
    curation: CurationMode = CurationMode.AGENT,
    max_tools: int = DEFAULT_MAX_TOOLS,
    namespace: str = "",
    include_tag: list[str] | None = None,
    dry_run: bool = False,
    recurate: bool = False,
    force: bool = False,
    allow_host: list[str] | None = None,
    on_failure: OnFailure = OnFailure.EXCLUDE,
    sandbox: str = "",
    deterministic: bool = False,
    live: bool = False,
    allow_writes: bool = False,
    isolate: bool = False,
    html: bool = False,
) -> RunOptions:
    if quiet and verbose:
        raise UsageError("use only one of --quiet and --verbose")
    if live and sandbox:
        raise UsageError("use only one of --live and --sandbox")
    _configure_logging(quiet=quiet, verbose=verbose)
    options = RunOptions(
        out=Path(".") if out is None else out,
        as_json=as_json,
        quiet=quiet,
        verbose=verbose,
        offline=offline,
        allow_ref_hosts=tuple(allow_ref_host or ()),
        model=model,
        budget_tokens=budget_tokens,
        budget_usd=budget_usd,
        curation=curation,
        max_tools=max_tools,
        namespace=namespace,
        include_tags=tuple(include_tag or ()),
        dry_run=dry_run,
        recurate=recurate,
        force=force,
        allow_hosts=tuple(allow_host or ()),
        on_failure=on_failure,
        sandbox=sandbox,
        deterministic=deterministic,
        live=live,
        allow_writes=allow_writes,
        isolate=isolate,
        html=html,
    )
    ctx.obj = options
    return options


def _ingest(spec: str, options: RunOptions) -> None:
    from mcp4api.ingest import ingest as load_spec
    from mcp4api.ingest import write_endpoints

    _LOG.info("ingest")
    loaded = load_spec(
        spec,
        offline=options.offline,
        allow_ref_hosts=options.allow_ref_hosts,
    )
    write_endpoints(loaded, options.out)


def _curate(options: RunOptions) -> None:
    from mcp4api.curate.agent import CurationOptions, curate

    _LOG.info("curate")
    result = curate(
        options.out,
        CurationOptions(
            model=options.model,
            mode=options.curation,
            max_tools=options.max_tools,
            namespace=options.namespace,
            include_tags=options.include_tags,
            budget_tokens=options.budget_tokens,
            budget_usd=options.budget_usd,
            dry_run=options.dry_run,
            recurate=options.recurate,
            offline=options.offline,
        ),
    )
    if result.report:
        sys.stdout.write(result.report)
    for warning in result.warnings:
        sys.stderr.write(f"{warning}\n")


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise UsageError(f"not found: {path}")


def _require_dir(path: Path) -> None:
    if not path.is_dir():
        raise UsageError(f"not found: {path}")


def _emit(options: RunOptions) -> None:
    from mcp4api.emit import emit

    _LOG.info("emit")
    emit(options.out, force=options.force)


def _verify(options: RunOptions) -> None:
    from mcp4api.verify import verify

    _LOG.info("verify")
    outcome = verify(options.out, _verify_options(options))
    if outcome.gaps:
        raise GapError(outcome.summary, printed=not options.as_json)


def _record(options: RunOptions) -> None:
    from mcp4api.verify import record as record_cassettes

    if not options.sandbox:
        raise UsageError("record needs --sandbox")
    _LOG.info("record")
    record_cassettes(options.out, _verify_options(options))


def _verify_options(options: RunOptions) -> VerifyOptions:
    from mcp4api.verify import VerifyOptions as Options

    return Options(
        offline=options.offline,
        live=options.live,
        sandbox=options.sandbox,
        allow_hosts=options.allow_hosts,
        allow_writes=options.allow_writes,
        on_failure=options.on_failure,
        deterministic=options.deterministic,
        html=options.html,
        quiet=options.quiet,
        as_json=options.as_json,
        isolate=options.isolate,
    )


def _configure_logging(*, quiet: bool, verbose: bool) -> None:
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s", force=True)


def _json_requested(argv: list[str]) -> bool:
    return "--json" in argv


def _emit_error(message: str, code: ExitCode, *, as_json: bool) -> None:
    if as_json:
        report: ErrorReport = {"ok": False, "exit_code": code, "error": message}
        sys.stdout.write(json.dumps(report) + "\n")
        return
    sys.stderr.write(f"{message}\n")


def _parser_exit(exc: typer.TyperException) -> int:
    if isinstance(exc, _ParserError):
        exc.show()
        return exc.exit_code
    sys.stderr.write(f"{exc}\n")
    return exc.exit_code
