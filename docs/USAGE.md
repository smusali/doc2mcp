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

Exit 4 includes an HTML page, a remote `$ref` whose host is not allowed, a response over 25 MiB, and a YAML document that is too large. Exit 0 is reserved for a run that verified every generated tool. `doc2mcp ingest` exits 0 after it writes `endpoints.json`.
