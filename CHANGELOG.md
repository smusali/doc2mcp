# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Curation writes `tools.yaml` (`manifest-v1`) from `endpoints.json`, with an agent, a heuristic fallback, human overrides, and a budget.
- Sample Shelf library API and in-memory reference server under `examples/sample-api`.
- OpenAPI 3.0, 3.1, 3.2, and Swagger 2.0 ingest that writes a deterministic `endpoints.json` in the `endpoint-v1` model.
- Installable `doc2mcp` package and command line interface, with stage commands, shared flags, and documented exit codes.
- Continuous integration for Python 3.11 through 3.15 on Linux, macOS, and Windows, including lint, format, strict typing, and offline tests.
- Security policy, changelog check, and a bug report form.
