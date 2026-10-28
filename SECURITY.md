# Security Policy

## Supported versions

Only the latest published release receives security fixes.

| Version | Supported |
| --- | --- |
| Latest published release | Yes |
| Older releases | No |

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub security advisories](https://github.com/smusali/mcp4api/security/advisories/new). Do not open a public issue, and do not disclose the report before a fix is published. You can expect an initial response within 3 days.

## Threat model

mcp4api fetches documentation the operator did not write and passes that text to a model. A hostile page can attempt prompt injection. Curation wraps that text and then enforces exclusions in code. The tool generates code, executes it, and can call live APIs with real credentials. The methodology is in [Methodology](docs/METHODOLOGY.md).

## Guarantees

- Credentials are read from the environment and are not written into generated code, fixtures, or prompts.
- Cassettes store no headers, and secret values are replaced before the file is written.
- Verification dials only loopback or a host passed to `--allow-host`.
- Destructive and non-idempotent tools are not called under `--live` unless `--allow-writes` is set.
- Report excerpts are trimmed upstream bodies. Treat `report/` as sensitive when a live body may contain secrets.
