# Usage

`mcp4api SPEC` reads an OpenAPI document and writes the artifacts under `--out`.

## Commands

```text
mcp4api SPEC
mcp4api ingest SPEC
mcp4api curate
mcp4api emit
mcp4api verify
mcp4api record --sandbox URL
```

`SPEC` is a file path or URL. The primary command runs ingest, curate, emit, and verify. Each stage command reads the previous artifact from `--out`, so a run can resume.

- `mcp4api SPEC` reads the OpenAPI document and writes every stage artifact.
- `mcp4api ingest SPEC` writes `endpoints.json`.
- `mcp4api curate` reads `endpoints.json` and writes `tools.yaml`.
- `mcp4api emit` reads `tools.yaml` and `endpoints.json` and writes `generated/`.
- `mcp4api verify` reads `generated/`, `tools.yaml`, and `endpoints.json`, and writes `tests/` and `report/`.
- `mcp4api record` reads those same inputs plus a sandbox URL and writes `tests/cassettes/`.

Paths other than `SPEC` are relative to `--out`. Flags may appear before or after the command. A flag that the command does not accept is a parse error.

## Flags

- `--out DIR` is the artifact root. It defaults to the current directory.
- `--json` prints application errors as one JSON object on stdout.
- `--version` prints `mcp4api <version>` and exits 0.
- `--quiet` logs errors only. `--verbose` logs debug messages. Using both is a usage error.
- `--offline` reads local files only. A URL, or a `$ref` that needs the network, is a usage error.
- `--allow-ref-host HOST` allows a remote `$ref` from `HOST`. Repeat it for more than one host.
- `--model ID` selects the Claude model. The default is `claude-opus-5-5`.
- `--budget-tokens N` and `--budget-usd N` cap agent curation. The defaults are 200000 tokens and 5 USD.
- `--curation MODE` is `agent`, `heuristic`, or `manual`. The default is `agent`.
- `--max-tools N` is the tool-count target. The default is 20. Exceeding it requires a rationale.
- `--namespace NAME` prefixes each tool name.
- `--include-tag TAG` keeps a tag or a category. Repeat it to include more than one.
- `--dry-run` prints a token and cost estimate and does not write `tools.yaml`.
- `--recurate` curates again even when the input hash matches.
- `--force` overwrites generated files that were edited after the last emit.
- `--deterministic` omits `started_at` and durations from the verification report.
- `--live` calls the spec server. A host outside the sandbox set needs `--allow-host`.
- `--sandbox URL` calls that URL instead of the fixture server.
- `--allow-host HOST` is a host the verification proxy may dial. Repeat it for more than one host.
- `--allow-writes` calls destructive and non-idempotent tools during `--live`.
- `--isolate` reruns verification inside a Linux network namespace.
- `--html` also writes `report/verification.html`.
- `--on-failure MODE` is `exclude` or `keep`. `exclude` is the default and drops a failed tool from `generated/`. `keep` leaves it and prefixes its description with a warning.

`--json` applies to errors raised by mcp4api. The parser's own usage errors stay as text and still exit 2.

```json
{"ok": false, "exit_code": 2, "error": "not found: endpoints.json"}
```

Remote `$ref`s are followed only for the input URL's host, plus each `--allow-ref-host`. A file input has no host, so every remote `$ref` needs the flag.

## Exit codes

- 0 means every generated tool verified.
- 1 is an internal error.
- 2 is a usage or configuration error, including a missing input. The message names the path.
- 3 means at least one tool is a known gap. `--json` prints the same error object with `"exit_code": 3`.
- 4 is an ingest, network, model, or upstream API failure.

`--offline` with `record`, `--sandbox`, or `--live` exits 2 before any connection. `--live` and `--sandbox` together is a usage error. A missing `ANTHROPIC_API_KEY` in agent mode is exit 2. `--dry-run` exits 0 after the estimate. `--offline --dry-run` exits 2. A file under `generated/` that was edited after the last emit is exit 2 unless `--force` is set.

## overrides.yaml

`overrides.yaml` sits next to `tools.yaml`. mcp4api never writes it. It is applied to the cached base, so a second run does not apply it twice.

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

Descriptions, fixed values, and outputs use the tool name from the base, before renames. Renames do not chain. An included endpoint comes back as a one-endpoint tool. Unsupported auth stays excluded. Unknown names print `stale override:` on stderr and are skipped.

## Run it on your API

```bash
mcp4api openapi.yaml --out .
```

Agent curation reads `ANTHROPIC_API_KEY`. `--curation heuristic` does not. Commit `tools.yaml` and `tests/cassettes/` when the same result should come back without a model call:

```bash
mcp4api openapi.yaml --offline --out .
```

`generated/README.md` names the environment variables. Add the server to Claude Code from the artifact root:

```bash
claude mcp add <slug> -- uvx --from ./generated <slug>-mcp
```

`report/verification.md` lists every tool. A known gap names the field that failed. Exit code 3 means at least one tool is a known gap.
