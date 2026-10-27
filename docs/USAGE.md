# Usage

`doc2mcp` reads an OpenAPI document and writes one artifact per stage under `--out`.

## Commands

```text
doc2mcp SPEC
doc2mcp ingest SPEC
doc2mcp curate
doc2mcp emit
doc2mcp verify
```

`SPEC` is an OpenAPI file path or URL. The primary command runs every stage. Each stage command reads the previous artifact from disk, so a run can resume after an earlier stage.

| Command | Reads | Writes |
| --- | --- | --- |
| `doc2mcp SPEC` | OpenAPI file or URL | Every stage artifact |
| `doc2mcp ingest SPEC` | OpenAPI file or URL | `endpoints.json` |
| `doc2mcp curate` | `endpoints.json` | `tools.yaml` |
| `doc2mcp emit` | `tools.yaml`, `endpoints.json` | `generated/` |
| `doc2mcp verify` | `generated/`, `tests/` | `report/` |

Paths other than `SPEC` are relative to `--out`.

`doc2mcp ingest SPEC --out DIR` reads an OpenAPI 3.0, 3.1, or 3.2 document, or a Swagger 2.0 document, and writes `endpoints.json`.

## Flags

Flags may appear before or after the command.

| Flag | Meaning |
| --- | --- |
| `--out DIR` | Artifact root. Defaults to the current directory. |
| `--json` | Print application errors as one JSON object on stdout. |
| `--version` | Print `doc2mcp <version>` and exit 0. |
| `--quiet` | Log errors only. |
| `--verbose` | Log debug messages. |
| `--offline` | Read local files only. A URL, or a `$ref` that needs the network, is a usage error. |
| `--allow-ref-host HOST` | Allow a remote `$ref` from `HOST`. Repeat the flag for more than one host. |
| `--model ID` | Claude model for agent curation. Defaults to `claude-opus-5-5`. |
| `--budget-tokens N` | Token budget for agent curation. Defaults to 200000. |
| `--budget-usd N` | Estimated USD budget for agent curation. Defaults to 5. |
| `--curation MODE` | `agent`, `heuristic`, or `manual`. Defaults to `agent`. |
| `--max-tools N` | Tool-count target. Defaults to 20. Exceeding it requires a rationale. |
| `--namespace NAME` | Prefix added to each tool name. |
| `--include-tag TAG` | Keep a tag or a category (`admin`, `internal`, `deprecated`, `webhook-management`, `destructive-bulk`). Repeat the flag to include more than one. |
| `--dry-run` | Print a token and cost estimate and do not write `tools.yaml`. |
| `--recurate` | Curate again even when the input hash matches. |
| `--force` | Overwrite generated files that were edited after the last emit. |

`--quiet` and `--verbose` together is a usage error.

Remote `$ref`s are followed only for the input URL's host, plus each `--allow-ref-host`. Credentials are not sent on those fetches. A file input has no host, so every remote `$ref` needs the flag.

`--json` applies to errors raised by doc2mcp. The parser's own usage errors, including an unknown flag, stay as text and still exit 2.

```json
{"ok": false, "exit_code": 2, "error": "not found: endpoints.json"}
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Every generated tool verified |
| 1 | Internal error (a doc2mcp bug) |
| 2 | Usage or configuration error |
| 3 | Generated, but at least one tool is a known gap (failed or unverified) |
| 4 | Ingest, network, model API, or upstream API failure |

A missing input is exit 2, and the message names that path. `--offline` refusing a URL or a network `$ref` is also exit 2.

`doc2mcp curate` reads `endpoints.json` and writes `tools.yaml`. Agent mode needs `ANTHROPIC_API_KEY`. A missing key is exit 2 with `missing ANTHROPIC_API_KEY`, before a client is created. `--dry-run` counts tokens and prints the estimate on stdout, then exits 0 without writing `tools.yaml`. `--offline --dry-run` exits 2 with `cannot estimate tokens offline`. `--offline` without a matching `tools.yaml` exits 2 unless `--curation heuristic` is set. `--curation manual` loads `tools.yaml`, applies `overrides.yaml`, and exits 2 without rewriting the file when that manifest is missing or invalid. A model or network failure during curation is exit 4.

`doc2mcp emit` reads `tools.yaml` and `endpoints.json` and writes `generated/`. A file under `generated/` that was edited after the last emit is exit 2. The message lists that path and says to change `tools.yaml` or `overrides.yaml`, or to pass `--force`. `--force` overwrites the edited files. The primary command ingests, curates, emits, then stops at verify until `report/` can be written. `--dry-run` on that command returns after the estimate and does not emit.

Exit 4 includes an HTML page, a remote `$ref` whose host is not allowed, a response over 25 MiB, and a YAML document that is too large. Exit 0 is reserved for a run that verified every generated tool. `doc2mcp ingest` exits 0 after it writes `endpoints.json`.
