from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, TypedDict, runtime_checkable

import typer

from mcp4api import __version__

if TYPE_CHECKING:
    from mcp4api.verify import VerifyOptions

_DESCRIPTION = (
    "Turn API docs into a working MCP server, "
    "then verify every generated tool against the real API."
)
_STAGES = frozenset({"ingest", "curate", "emit", "verify", "record"})
_VALUE_FLAGS = frozenset(
    {
        "--out",
        "--allow-ref-host",
        "--allow-host",
        "--model",
        "--budget-tokens",
        "--budget-usd",
        "--curation",
        "--max-tools",
        "--namespace",
        "--include-tag",
        "--on-failure",
        "--sandbox",
    }
)
_BOOL_FLAGS = frozenset(
    {
        "--json",
        "--quiet",
        "--verbose",
        "--offline",
        "--dry-run",
        "--recurate",
        "--force",
        "--deterministic",
        "--live",
        "--allow-writes",
        "--isolate",
        "--html",
    }
)
_EQUALS_FLAGS = (
    "--out=",
    "--allow-ref-host=",
    "--allow-host=",
    "--model=",
    "--budget-tokens=",
    "--budget-usd=",
    "--curation=",
    "--max-tools=",
    "--namespace=",
    "--include-tag=",
    "--on-failure=",
    "--sandbox=",
)
_ENDPOINTS = "endpoints.json"
_TOOLS = "tools.yaml"
_GENERATED = "generated"


class ExitCode(IntEnum):
    VERIFIED = 0
    INTERNAL = 1
    USAGE = 2
    GAP = 3
    UPSTREAM = 4


class UsageError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UpstreamError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GapError(Exception):
    def __init__(self, message: str, *, printed: bool) -> None:
        super().__init__(message)
        self.message = message
        self.printed = printed


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
    curation: str
    max_tools: int
    namespace: str
    include_tags: tuple[str, ...]
    dry_run: bool
    recurate: bool
    force: bool
    allow_hosts: tuple[str, ...]
    on_failure: str
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
    except UsageError as exc:
        _emit_error(exc.message, ExitCode.USAGE, as_json=_json_requested(args))
        return ExitCode.USAGE
    except UpstreamError as exc:
        _emit_error(exc.message, ExitCode.UPSTREAM, as_json=_json_requested(args))
        return ExitCode.UPSTREAM
    except GapError as exc:
        if not exc.printed or _json_requested(args):
            _emit_error(exc.message, ExitCode.GAP, as_json=_json_requested(args))
        return ExitCode.GAP
    except typer.TyperException as exc:
        return _parser_exit(exc)
    except Exception as exc:
        _emit_error(
            str(exc) or exc.__class__.__name__,
            ExitCode.INTERNAL,
            as_json=_json_requested(args),
        )
        return ExitCode.INTERNAL
    return ExitCode.VERIFIED


def _print_version(value: bool) -> None:
    if not value:
        return
    typer.echo(f"mcp4api {__version__}")
    raise typer.Exit(code=ExitCode.VERIFIED)


app = typer.Typer(
    add_completion=False,
    help=_DESCRIPTION,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Directory that receives stage artifacts."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print errors as a JSON object."),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Show errors only.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show debug logs.")] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Read local files only."),
    ] = False,
    allow_ref_host: Annotated[
        list[str] | None,
        typer.Option("--allow-ref-host", help="Host allowed for remote reference fetches."),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", help="Claude model used for agent curation."),
    ] = "claude-opus-5-5",
    budget_tokens: Annotated[
        int,
        typer.Option("--budget-tokens", help="Maximum tokens for agent curation."),
    ] = 200_000,
    budget_usd: Annotated[
        float,
        typer.Option("--budget-usd", help="Maximum estimated USD for agent curation."),
    ] = 5,
    curation: Annotated[
        str,
        typer.Option("--curation", help="Curation mode: agent, heuristic, or manual."),
    ] = "agent",
    max_tools: Annotated[
        int,
        typer.Option("--max-tools", help="Tool-count target. Exceeding it requires a rationale."),
    ] = 20,
    namespace: Annotated[
        str,
        typer.Option("--namespace", help="Prefix added to each tool name."),
    ] = "",
    include_tag: Annotated[
        list[str] | None,
        typer.Option("--include-tag", help="Tag or category to keep instead of excluding."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print a token and cost estimate without a paid call."),
    ] = False,
    recurate: Annotated[
        bool,
        typer.Option("--recurate", help="Curate again even when the input hash matches."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite generated files that were edited."),
    ] = False,
    allow_host: Annotated[
        list[str] | None,
        typer.Option("--allow-host", help="Host the verification proxy may dial."),
    ] = None,
    on_failure: Annotated[
        str,
        typer.Option(
            "--on-failure", help="exclude drops a failed tool. keep warns in its description."
        ),
    ] = "exclude",
    sandbox: Annotated[
        str,
        typer.Option(
            "--sandbox", help="Sandbox base URL for record, or for verify instead of fixtures."
        ),
    ] = "",
    deterministic: Annotated[
        bool,
        typer.Option("--deterministic", help="Omit timestamps and durations from the report."),
    ] = False,
    live: Annotated[
        bool,
        typer.Option("--live", help="Call the spec server instead of fixtures."),
    ] = False,
    allow_writes: Annotated[
        bool,
        typer.Option(
            "--allow-writes", help="Call destructive and non-idempotent tools during --live."
        ),
    ] = False,
    isolate: Annotated[
        bool,
        typer.Option("--isolate", help="Run verification in a Linux network namespace."),
    ] = False,
    html: Annotated[
        bool,
        typer.Option("--html", help="Also write report/verification.html."),
    ] = False,
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
    if quiet and verbose:
        raise UsageError("use only one of --quiet and --verbose")
    if live and sandbox:
        raise UsageError("use only one of --live and --sandbox")
    if on_failure not in {"exclude", "keep"}:
        raise UsageError("on-failure must be exclude or keep")
    _configure_logging(quiet=quiet, verbose=verbose)
    ctx.obj = RunOptions(
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
    if ctx.invoked_subcommand is None:
        raise UsageError("missing spec")


@app.command("run", hidden=True)
def _run(
    ctx: typer.Context,
    spec: Annotated[str, typer.Argument(help="OpenAPI file path or URL.")],
) -> None:
    options = _options(ctx)
    _ingest(spec, options)
    _curate(options)
    if options.dry_run:
        return
    _emit(options)
    _verify(options)


@app.command(help="Read an OpenAPI file path or URL and write endpoints.json.")
def ingest(
    ctx: typer.Context,
    spec: Annotated[str, typer.Argument(help="OpenAPI file path or URL.")],
) -> None:
    _ingest(spec, _options(ctx))


@app.command(help="Read endpoints.json and write tools.yaml.")
def curate(ctx: typer.Context) -> None:
    _curate(_options(ctx))


@app.command(help="Read tools.yaml and endpoints.json and write generated/.")
def emit(ctx: typer.Context) -> None:
    options = _options(ctx)
    _require_file(options.out / _TOOLS)
    _require_file(options.out / _ENDPOINTS)
    _emit(options)


@app.command(help="Call every generated tool and write report/.")
def verify(ctx: typer.Context) -> None:
    options = _options(ctx)
    _require_dir(options.out / _GENERATED)
    _require_file(options.out / _ENDPOINTS)
    _require_file(options.out / _TOOLS)
    _verify(options)


@app.command(help="Record cassettes against a sandbox URL.")
def record(ctx: typer.Context) -> None:
    _record(_options(ctx))


def _normalize_argv(argv: list[str]) -> list[str]:
    hoisted: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_FLAGS:
            if index + 1 >= len(argv):
                rest.append(token)
                index += 1
                continue
            hoisted.extend((token, argv[index + 1]))
            index += 2
            continue
        if token.startswith(_EQUALS_FLAGS):
            hoisted.append(token)
            index += 1
            continue
        if token in _BOOL_FLAGS:
            hoisted.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    if not rest or rest[0] in _STAGES or rest[0].startswith("-"):
        return [*hoisted, *rest]
    return [*hoisted, "run", *rest]


def _options(ctx: typer.Context) -> RunOptions:
    found = ctx.obj
    if isinstance(found, RunOptions):
        return found
    raise UsageError("missing run options")


def _ingest(spec: str, options: RunOptions) -> None:
    from mcp4api.ingest import ingest as load_spec
    from mcp4api.ingest import write_endpoints

    loaded = load_spec(
        spec,
        offline=options.offline,
        allow_ref_hosts=options.allow_ref_hosts,
    )
    write_endpoints(loaded, options.out)


def _curate(options: RunOptions) -> None:
    from mcp4api.curate.agent import CurationOptions, CurationUpstream, CurationUsage, curate

    mode = _curation_mode(options.curation)
    try:
        result = curate(
            options.out,
            CurationOptions(
                model=options.model,
                mode=mode,
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
    except CurationUsage as exc:
        raise UsageError(exc.message) from exc
    except CurationUpstream as exc:
        raise UpstreamError(exc.message) from exc
    if result.report:
        sys.stdout.write(result.report)
    for warning in result.warnings:
        sys.stderr.write(f"{warning}\n")


def _curation_mode(value: str) -> Literal["agent", "heuristic", "manual"]:
    if value == "agent":
        return "agent"
    if value == "heuristic":
        return "heuristic"
    if value == "manual":
        return "manual"
    raise UsageError("curation mode is not supported")


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise UsageError(f"not found: {path}")


def _require_dir(path: Path) -> None:
    if not path.is_dir():
        raise UsageError(f"not found: {path}")


def _emit(options: RunOptions) -> None:
    from mcp4api.emit import EmitError, emit

    try:
        emit(options.out, force=options.force)
    except EmitError as exc:
        raise UsageError(exc.message) from exc


def _verify(options: RunOptions) -> None:
    from mcp4api.verify import VerifyUpstream, VerifyUsage, verify

    try:
        outcome = verify(options.out, _verify_options(options))
    except VerifyUsage as exc:
        raise UsageError(exc.message) from exc
    except VerifyUpstream as exc:
        raise UpstreamError(exc.message) from exc
    if outcome.gaps:
        raise GapError(outcome.summary, printed=not options.as_json)


def _record(options: RunOptions) -> None:
    from mcp4api.verify import VerifyUpstream, VerifyUsage
    from mcp4api.verify import record as record_cassettes

    if not options.sandbox:
        raise UsageError("record needs --sandbox")
    try:
        record_cassettes(options.out, _verify_options(options))
    except VerifyUsage as exc:
        raise UsageError(exc.message) from exc
    except VerifyUpstream as exc:
        raise UpstreamError(exc.message) from exc


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
    logging.basicConfig(level=level, force=True)


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
