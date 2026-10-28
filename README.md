# mcp4api

mcp4api turns your API docs into a working MCP server, then calls every generated tool against your API and reports which ones work. Built for platform teams who want their product callable by agents.

[![CI](https://github.com/smusali/mcp4api/actions/workflows/ci.yml/badge.svg)](https://github.com/smusali/mcp4api/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp4api.svg)](https://pypi.org/project/mcp4api/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/smusali/mcp4api/blob/main/LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/mcp4api.svg)](https://pypi.org/project/mcp4api/)

![Offline run against the Shelf sample, from ingest through the verification summary](https://raw.githubusercontent.com/smusali/mcp4api/main/examples/demo.gif)

[Recording](https://github.com/smusali/mcp4api/blob/main/examples/demo.cast)

## Install

```bash
pip install mcp4api
uvx mcp4api
```

## Quickstart

These commands need no API key. They reuse the committed manifest and cassettes, regenerate the other artifacts in place, and exit 3. The sample's one known gap is `get_book`: `page_count` is a string in the API and an integer in the spec.

```bash
git clone https://github.com/smusali/mcp4api && cd mcp4api
uvx mcp4api examples/sample-api/openapi.yaml --offline --out examples/sample-api
cat examples/sample-api/report/verification.md
```

## Generated tool

`upload_book_file` from the Shelf sample is four calls composed into one tool.

```python
@mcp.tool(
    title="Upload Book File",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def upload_book_file(
    book_id: Annotated[str, Field(description="book id")],
    content_type: Annotated[str, Field(description="content type")],
    data: Annotated[str, Field(description="data")],
    filename: Annotated[str, Field(description="filename")],
) -> UploadBookFileResult:
    """Upload a file and attach it to a book."""
    create_upload = await api.request(
        "POST",
        "/uploads",
        body={"content_type": content_type, "filename": filename},
        auth=(("api_key", "header", "X-API-Key"),),
    )
    _upload_part = await api.request(
        "PUT",
        "/uploads/{upload_id}/parts/{part_number}",
        path_params={
            "part_number": UPLOAD_BOOK_FILE_PART_NUMBER,
            "upload_id": api.pick(create_upload, "id"),
        },
        body={"data": data},
        auth=(("api_key", "header", "X-API-Key"),),
    )
    _complete_upload = await api.request(
        "POST",
        "/uploads/{upload_id}/complete",
        path_params={"upload_id": api.pick(create_upload, "id")},
        auth=(("api_key", "header", "X-API-Key"),),
    )
    attach_book_file = await api.request(
        "PUT",
        "/books/{book_id}/file",
        path_params={"book_id": book_id},
        body={"upload_id": api.pick(create_upload, "id")},
        auth=(("api_key", "header", "X-API-Key"),),
    )

    return UploadBookFileResult.model_validate(attach_book_file)
```

## Verification report

![Verification report for the Shelf sample, with get_book as the known gap](https://raw.githubusercontent.com/smusali/mcp4api/main/examples/verification.png)

## CI

`mcp/` holds the committed `tools.yaml` and cassettes. Exit 3 fails the job.

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
  with:
    persist-credentials: false
- uses: astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e  # v10.3.2
- run: uvx mcp4api openapi.yaml --offline --out mcp/
```

## Limitations

- OpenAPI input only (3.0, 3.1, 3.2, and Swagger 2.0). No HTML docs pages, runbooks, Postman collections, or GraphQL.
- The emitted server authenticates with an API key or a bearer token from environment variables. Endpoints that need another scheme are excluded and listed in the report.
- The emitted server does not paginate, retry, or rate-limit. List tools return the first page.
- No repair loop. A failing tool is reported as a known gap and left out of the server. It is not fixed automatically.
- No PII redaction. The emitted server never logs bodies, but the report's failure details include trimmed upstream responses, which can hold real data when you verify against a sandbox or a live API.
- Python output only.
- No hosted service and no GitHub Action. The exit codes are the CI interface.
- Agent curation needs an Anthropic API key. `--curation heuristic` does not.
- Model output is not deterministic, so reproducibility comes from committing `tools.yaml`.
- `--isolate` is Linux-only, and synthesized fixtures are weaker evidence than recorded ones.

The same limits, with the mechanics behind them, are in [Limitations](https://github.com/smusali/mcp4api/blob/main/docs/limitations.md).

## Contributing

See [Contributing](https://github.com/smusali/mcp4api/blob/main/CONTRIBUTING.md).

## License

[MIT](https://github.com/smusali/mcp4api/blob/main/LICENSE)
