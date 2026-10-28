# Contributing

## Setup

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
```

## Checks

These commands stay offline and do not need an API key:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

`pytest` disables network sockets.

## Demo recording

`tools/record_demo.sh` rewrites `examples/demo.cast` and `examples/demo.gif`. It needs [asciinema](https://docs.asciinema.org/) and [agg](https://github.com/asciinema/agg). Run it when the CLI output changes.
