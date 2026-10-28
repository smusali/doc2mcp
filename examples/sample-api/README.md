# Shelf sample API

Shelf is a small library circulation API. It is the shared input for mcp4api: an OpenAPI 3.1 document and an in-memory reference server, with no mcp4api code in either file.

The server listens on `127.0.0.1` and a port you pass on the command line. Its data is fixed: the same requests against a fresh process return the same response bytes. Send the sample API key on every request:

```text
X-API-Key: shelf-sample-key
```

## Deliberate traits

| Trait | What it exercises |
| --- | --- |
| Cursor pagination (`cursor`, `has_more`) on every list | A list cursor that can be hidden behind a fixed value |
| API-key header `X-API-Key` on every operation | Auth on the emitted server |
| `createUpload`, `uploadPart`, `completeUpload` | One composed multi-step upload |
| Operations tagged `admin` | Admin operations excluded by default |
| `getBookPageCount` marked `deprecated: true` | A deprecated operation excluded with that reason |
| `getBookStats` documents `page_count` as an integer | The one schema mismatch: the server returns that field as a string |

`page_count` on `getBookStats` is an integer in the response schema and in the response example (`320`). The reference server returns `"320"`. `getBookPageCount` returns the same number as an integer.

## Start the server

From the repository root:

```text
python examples/sample-api/server.py 8765
```

The process binds to `127.0.0.1:8765`. That host and port match the server URL in `openapi.yaml`. Choose another port by passing it instead of `8765`.

## Record cassettes

```text
mcp4api record --sandbox http://127.0.0.1:8765
```

Use the port the server was given. Set `SHELF_API_KEY` to the sample key before recording. The key is a request header, and cassettes do not store headers.

That command uses one server process for the whole plan. The committed cassettes were recorded with a new process for each call, so each call sees the seed data. A second checkout of `copy_002` in the same process fails because the copy is already on loan. `delete_book` uses `book_004`, created with one `POST /books` in that same process before the delete. Reads against a fresh process return the same bytes.

## Verification

From the repository root:

```text
mcp4api examples/sample-api/openapi.yaml --offline --deterministic --out examples/sample-api
```

That command exits 3. Fourteen tools verify from the committed cassettes. `get_book` is the known gap: `GET /books/{book_id}/stats` returns `page_count` as a string, and the spec says integer. The failure names `page_count`. `tools.yaml` still lists `get_book`. The default `--on-failure exclude` rewrites `generated/` without that tool.

`pytest examples/sample-api/tests` fails on that same gap. The report is the record of it.

## Directory

This directory is the artifact root for the sample.

```text
examples/sample-api/
  README.md
  openapi.yaml
  server.py
  endpoints.json
  tools.yaml
  generated/
  tests/
  report/
```

`endpoints.json` is the ingest of `openapi.yaml`. `tools.yaml` is a replayed curation with at most 15 tools and a reason for every other operation. Reuse it without a model call:

```text
mcp4api curate --out examples/sample-api --offline
```

A later refresh with a key is `mcp4api curate --recurate --out examples/sample-api`. Emit and verify still write `generated/`, `tests/`, and `report/` next to these files.
