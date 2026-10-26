from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Protocol, TypedDict, runtime_checkable

import typer

from doc2mcp import __version__

_DESCRIPTION = (
    "Turn API docs into a working MCP server, "
    "then verify every generated tool against the real API."
)
_STAGES = frozenset({"ingest", "curate", "emit", "verify"})
_VALUE_FLAGS = frozenset({"--out", "--allow-ref-host"})
_BOOL_FLAGS = frozenset({"--json", "--quiet", "--verbose", "--offline"})
_EQUALS_FLAGS = ("--out=", "--allow-ref-host=")
_ENDPOINTS = "endpoints.json"
_TOOLS = "tools.yaml"
_GENERATED = "generated"
_TESTS = "tests"
_REPORT = "report"


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
        app(args=_normalize_argv(args), standalone_mode=False, prog_name="doc2mcp")
    except UsageError as exc:
        _emit_error(exc.message, ExitCode.USAGE, as_json=_json_requested(args))
        return ExitCode.USAGE
    except UpstreamError as exc:
        _emit_error(exc.message, ExitCode.UPSTREAM, as_json=_json_requested(args))
        return ExitCode.UPSTREAM
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
    typer.echo(f"doc2mcp {__version__}")
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
    _configure_logging(quiet=quiet, verbose=verbose)
    ctx.obj = RunOptions(
        out=Path(".") if out is None else out,
        as_json=as_json,
        quiet=quiet,
        verbose=verbose,
        offline=offline,
        allow_ref_hosts=tuple(allow_ref_host or ()),
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
    _require_file(options.out / _ENDPOINTS)
    _unwritten(options.out / _TOOLS)


@app.command(help="Read an OpenAPI file path or URL and write endpoints.json.")
def ingest(
    ctx: typer.Context,
    spec: Annotated[str, typer.Argument(help="OpenAPI file path or URL.")],
) -> None:
    _ingest(spec, _options(ctx))


@app.command(help="Read endpoints.json and write tools.yaml.")
def curate(ctx: typer.Context) -> None:
    options = _options(ctx)
    _require_file(options.out / _ENDPOINTS)
    _unwritten(options.out / _TOOLS)


@app.command(help="Read tools.yaml and endpoints.json and write generated/.")
def emit(ctx: typer.Context) -> None:
    options = _options(ctx)
    _require_file(options.out / _TOOLS)
    _require_file(options.out / _ENDPOINTS)
    _unwritten(options.out / _GENERATED)


@app.command(help="Read generated/ and tests/ and write report/.")
def verify(ctx: typer.Context) -> None:
    options = _options(ctx)
    _require_dir(options.out / _GENERATED)
    _require_dir(options.out / _TESTS)
    _unwritten(options.out / _REPORT)


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
    from doc2mcp.ingest import ingest as load_spec
    from doc2mcp.ingest import write_endpoints

    loaded = load_spec(
        spec,
        offline=options.offline,
        allow_ref_hosts=options.allow_ref_hosts,
    )
    write_endpoints(loaded, options.out)


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise UsageError(f"not found: {path}")


def _require_dir(path: Path) -> None:
    if not path.is_dir():
        raise UsageError(f"not found: {path}")


def _unwritten(path: Path) -> None:
    raise UsageError(f"cannot write {path}")


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
