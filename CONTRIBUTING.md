# Contributing

## Setup

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
```

## Checks

These commands stay offline and do not need an API key:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict mcp4api
uv run mypy --strict examples/sample-api/generated/shelf_mcp
uv run pytest
```

`pytest` disables network sockets and skips the `eval` marker.

## Paid eval

`uv run pytest -m eval` calls the live curation model. It needs `ANTHROPIC_API_KEY` and is not part of the default run or CI. Without that variable the tests are skipped.

## Demo recording

`tools/record_demo.sh` rewrites `examples/demo.cast` and `examples/demo.gif`. It needs [asciinema](https://docs.asciinema.org/) and [agg](https://github.com/asciinema/agg). Run it when the CLI output changes.
