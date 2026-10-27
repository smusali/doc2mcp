# Shelf sample API

Shelf is a small library circulation API. It is the shared input for doc2mcp: an OpenAPI 3.1 document and an in-memory reference server, with no doc2mcp code in either file.

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

Start the server, then record against it:

```text
doc2mcp record --sandbox http://127.0.0.1:8765
```

Use the same port the server was given. A fresh process returns byte-identical bodies, so a re-record does not churn.

## Directory

This directory is the artifact root for the sample.

```text
examples/sample-api/
  README.md
  openapi.yaml
  server.py
  endpoints.json
  tools.yaml
```

`endpoints.json` is the ingest of `openapi.yaml`. `tools.yaml` is a replayed curation with at most 15 tools and a reason for every other operation. Reuse it without a model call:

```text
doc2mcp curate --out examples/sample-api --offline
```

A later refresh with a key is `doc2mcp curate --recurate --out examples/sample-api`. Emit and verify still write `generated/`, `tests/`, and `report/` next to these files.
