# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-28

### Added

- `mcp4api SPEC` runs ingest, curate, emit, and verify on an OpenAPI 3.0, 3.1, 3.2, or Swagger 2.0 document.
- `mcp4api ingest` writes a deterministic `endpoints.json` with every schema in JSON Schema 2020-12.
- `mcp4api curate` writes `tools.yaml` with Claude, a local heuristic, or a hand-edited manifest, within a token and USD budget.
- `overrides.yaml` renames, describes, excludes, restores, or fixes inputs on curated tools without editing `tools.yaml`.
- `mcp4api emit` writes a standalone Python MCP server to `generated/` and refuses to overwrite hand edits without `--force`.
- `mcp4api verify` calls every generated tool behind a host allowlist, writes `report/`, and exits 3 on a known gap.
- `mcp4api record` writes scrubbed cassettes from a sandbox, so later `--offline` verification needs no network.
- The Shelf sample under `examples/sample-api` runs the whole pipeline offline.
