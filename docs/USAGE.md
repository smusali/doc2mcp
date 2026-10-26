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

`SPEC` is a path to an OpenAPI file or a docs URL. The primary command runs every stage. Each stage command reads the previous artifact from disk, so a run can resume after an earlier stage.

| Command | Reads | Writes |
| --- | --- | --- |
| `doc2mcp SPEC` | OpenAPI file or docs URL | Every stage artifact |
| `doc2mcp ingest SPEC` | OpenAPI file or docs URL | `endpoints.json` |
| `doc2mcp curate` | `endpoints.json` | `tools.yaml` |
| `doc2mcp emit` | `tools.yaml`, `endpoints.json` | `generated/` |
| `doc2mcp verify` | `generated/`, `tests/` | `report/` |

Paths other than `SPEC` are relative to `--out`.

## Flags

Flags may appear before or after the command.

| Flag | Meaning |
| --- | --- |
| `--out DIR` | Artifact root. Defaults to the current directory. |
| `--json` | Print application errors as one JSON object on stdout. |
| `--version` | Print `doc2mcp <version>` and exit 0. |
| `--quiet` | Log errors only. |
| `--verbose` | Log debug messages. |

`--quiet` and `--verbose` together is a usage error.

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

A missing input is exit 2, and the message names that path. Exit 0 is reserved for a run that verified every generated tool.
