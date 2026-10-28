# Architecture

mcp4api runs four stages. Each stage reads the previous stage's artifact from disk and writes its own. The files stay on disk so a later stage can resume without repeating earlier work, a failure can be inspected, and two runs of the same docs can be diffed.

`--out` is the artifact root. It defaults to the current directory.

| Stage | Reads | Writes |
| --- | --- | --- |
| ingest | OpenAPI file or URL | `endpoints.json` |
| curate | `endpoints.json` | `tools.yaml` |
| emit | `tools.yaml` and `endpoints.json` | `generated/` |
| verify | `generated/`, `tools.yaml`, `endpoints.json`, and `tests/cassettes/` when present | `tests/` and `report/` |

`ingest` is the only stage that reads an external spec. The other inputs are artifacts under `--out`. The primary command `mcp4api SPEC` runs the stages in that order.

Ingest writes `endpoints.json` as version `endpoint-v1`. Later stages read that file and do not convert schema dialects again. Every schema in the file is JSON Schema 2020-12. Shared and recursive schemas are stored once under `defs`, and a schema refers to one with `$ref: "#/defs/{name}"`.

`endpoint-v1` changes only by adding fields. A change that is not additive is published as `endpoint-v2`. `docs/schemas/endpoint-v1.json` is generated from the models, and the test suite fails when the two drift.

Curate writes `tools.yaml` as version `manifest-v1`. The model chooses names, groupings, and field paths. mcp4api copies every JSON Schema from `endpoints.json`, checks the manifest, and drops a tool that is still invalid after one correction. `docs/schemas/manifest-v1.json` is generated from the models the same way. The pre-override manifest is cached at `.mcp4api/base-tools.yaml`. A later run with the same input hash reuses that cache and applies `overrides.yaml` again. Emit still treats `tools.yaml` as an input and does not write it.

The artifact is deterministic. Endpoints are ordered by path and method, object keys are sorted, and the file is UTF-8 with LF line endings. For a local spec, `source.uri` is the file name without its directory, and `source.sha256` is the digest of the raw input bytes. Copying the spec to another directory does not change the artifact when the file name stays the same.

`.mcp4api/` is the local cache. It is not a stage artifact, and it is gitignored. Stage artifacts are whatever `--out` points at.

Emit reads `tools.yaml` and `endpoints.json` and writes a Python package under `generated/`. The package is a stdio MCP server by default, and it can also listen for Streamable HTTP on localhost. It calls the upstream API with `httpx2`. Generated code does not import mcp4api.

Templates live in `mcp4api/emit/templates`, ship inside the installed package, and are rendered with Jinja's package loader. The writer saves UTF-8 bytes with LF line endings. `.mcp4api-files.json` records the sha256 of every generated file except itself. On a later run, a missing file or a different hash stops the write and names the path relative to `--out`. `--force` overwrites those files. A ledger entry that is no longer generated is deleted when its bytes still match, so the tree matches a fresh emit. Files that were never generated are left in place.

Schema models are emitted in this repository. `datamodel-code-generator` was not used: its output changes with the formatter, the Python version, and class order, its defaults pull in Black and isort, and an untyped schema becomes `Any`. Curation already projects each tool schema onto a small JSON Schema subset, and the emitter turns that subset into pydantic models. A schema outside the subset fails emit and names the tool.

Python and TOML files start with two comments. The first names the mcp4api version, the spec file, and the full sha256 of the spec and of `tools.yaml`. The second says not to edit the file. Each Python comment ends with a length noqa so the full hash can stay on one line. `README.md` puts the same two sentences in an HTML comment. The ledger stores them in a `header` field.

Verify reads `endpoints.json`, `tools.yaml`, and `generated/`. It emits the full server again so a previous exclusion does not hide a tool, calls every tool over stdio, then applies `--on-failure`. The runner is `mcp4api/suite.py`. Verify copies that file to `<out>/tests/suite.py` and loads the copy, so the command and pytest execute the same file. `mcp4api record` uses the same runner against a sandbox URL and writes `tests/cassettes/`. It is not a step of the primary command.

The report is version `report-v1`. `docs/schemas/report-v1.json` is generated from the model, and the test suite fails when the two drift. `report/verification.json` is sorted JSON, UTF-8, with LF line endings. `--deterministic` omits `started_at` and durations. See `docs/VERIFICATION.md` for calls, cassettes, the allowlist, and exit 3.

The package requires Python 3.11 through 3.15.
