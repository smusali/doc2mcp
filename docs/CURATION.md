# Curation

Curation reads `endpoints.json` and writes `tools.yaml`. The file is a `manifest-v1` document: the API title and base URL, one tool per user intent, the endpoints left out, and a record of how the run was produced.

Emit reads `tools.yaml` and does not write it. Verify still only checks that its inputs exist.

## What the curator keeps

A tool names one intent, such as `search_books` or `return_loan`. `get` and `delete` are allowed as the first word. `post`, `put`, `patch`, `head`, `options`, `trace`, a version segment, and an `api_` prefix are rejected. A namespace from `--namespace` is added in code.

Multi-step flows collapse into one tool. The Shelf upload is one tool: create the upload, send the part, complete it, and attach the file. The heuristic does not collapse that flow. It writes one tool per remaining endpoint.

Pagination and other plumbing become fixed values when they are optional or have a default. `Idempotency-Key` is fixed to the scalar `$generated`. A required parameter with no default stays an input. List descriptions say they return the first page.

These operations are excluded unless `--include-tag` names the tag or the category:

| Reason | When |
| --- | --- |
| `admin` | Tag or path segment `admin` |
| `internal` | Tag or path segment `internal` |
| `deprecated` | The operation is deprecated |
| `webhook-management` | Tag or path contains `webhook` |
| `destructive-bulk` | A DELETE with no path parameter, or bulk, batch, or purge in the path, tag, operation id, or summary |
| `unsupported` | No auth alternative is entirely API key or HTTP bearer |
| `low-value` | The curator omitted it |
| `duplicate` | Another tool already covers it |
| `invalid-curation` | The tool was still invalid after one correction |

Empty security is supported. An operation that accepts either an API key or OAuth stays supported. Basic-only, OAuth-only, mutual TLS, and OpenID Connect are `unsupported`. Category rules are applied in code, including after the model responds.

Every endpoint appears once, either in a tool or in `excluded`, and every exclusion has a reason. More than `--max-tools` tools is allowed when each rationale is non-empty. The default target is 20. Coverage is not dropped to hit that number.

## Manifest

`tools.yaml` is UTF-8 YAML with LF line endings. Tools are sorted by name. Schemas are copied from the endpoint model. The model does not write JSON Schema. It names parameters, fixed scalars, step bindings, and output field paths such as `id` or `data[].id`.

`curation.mode` is `agent`, `heuristic`, or `manual`. `curation.fallback_tools` lists tools the heuristic wrote after the agent budget ran out. A full heuristic run leaves that list empty. `curation.usage` records input, output, cache-read, and cache-write tokens, plus the estimated USD.

`docs/schemas/manifest-v1.json` is generated from the models. The test suite fails if it drifts.

## Agent, heuristic, and manual

Agent mode calls Claude. The default model is `claude-opus-5-5`. `--model` selects another. Sampling parameters are not sent. Specs of 80 endpoints or fewer are planned in one call. Larger specs are split by tag, then into chunks of 80. Each planned tool gets one detail call. A bad stop retries that call once. A validation error is sent back once. Endpoints that stay invalid are `invalid-curation`. Endpoints the plan never covers are filled by the heuristic.

`--curation heuristic` builds the manifest locally and does not call a model. `--curation manual` loads `tools.yaml` and does not call a model.

## Budget and dry run

The defaults are `--budget-tokens 200000` and `--budget-usd 5`. The token budget is the sum of input, output, cache-read, and cache-write tokens. `count_tokens` is free. Before each paid call, mcp4api reserves the counted input plus 4096 output tokens and the matching USD. If the remainder cannot cover that reservation, later groups use the heuristic, those names are listed in `fallback_tools`, and their rationales start with `Heuristic fallback:`. A call that already returned is kept, even if it crosses the cap.

USD for `claude-opus-5-5` is `(input * 4 + cache_write * 5 + cache_read * 0.2 + output * 20) / 1000000`. `claude-sonnet-5-5` uses `(input * 2 + cache_write * 2.5 + cache_read * 0.1 + output * 10) / 1000000`. Any other model uses the Opus 5.5 table, and `--dry-run` prints `price_table: claude-opus-5-5`.

`--dry-run` prints `input_tokens`, `output_tokens_estimate`, `calls`, and `usd_estimate` to stdout and exits 0. It does not write `tools.yaml`.

## Hash reuse

mcp4api hashes `endpoints.json`, the criteria version, the model id, the mcp4api `major.minor`, and the curation mode, include tags, max tools, and namespace. Budgets, `--dry-run`, `--recurate`, and `--offline` are not part of the hash.

A matching hash and no `--recurate` makes no model call. The base manifest is `.mcp4api/base-tools.yaml` when that file's hash matches, otherwise `tools.yaml` when `overrides.yaml` is absent. Overrides are applied to that base, so a second run does not apply them twice. `--recurate` ignores the match.

## Overrides

`overrides.yaml` sits next to `tools.yaml`. mcp4api never writes it. It is applied to a fresh base:

```yaml
renames:
  list_books: search_books
descriptions:
  list_books: Search the catalog.
exclude_endpoints:
  - endpoint_id: GET /books/{book_id}/stats
    reason: low-value
    note: Stats are folded into get_book.
include_endpoints:
  - GET /admin/audit-events
fixed:
  - tool: list_books
    endpoint_id: GET /books
    param: limit
    in: query
    value: 2
    reason: hidden plumbing
outputs:
  get_book:
    - id
    - title
```

Descriptions, fixed values, and outputs use the tool name from the base, before renames. Renames do not chain. An included endpoint is restored as a one-endpoint heuristic tool, including an admin endpoint. Unsupported auth stays excluded. Unknown names and endpoints print `stale override: ...` on stderr and are skipped. A re-curation prints `tools added:` and `tools removed:` using the final names.

## Live eval

The default test run does not call the model. The live comparison is marked `eval` and is skipped without `ANTHROPIC_API_KEY`. It curates the Petstore, Stripe, and GitHub corpus specs with an explicit budget and checks that the agent is at least as complete as the heuristic and better on the remaining scores.

```text
pytest -m eval -o addopts=
```

`-o addopts=` is required. The default options disable the network and exclude the `eval` marker.
