# Security Policy

## Supported versions

No release is published yet. There is no supported version until the first release. After that, only the latest published release receives security fixes.

| Version | Supported |
| --- | --- |
| Latest published release | Yes |
| Older releases | No |

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub security advisories](https://github.com/smusali/doc2mcp/security/advisories/new). Do not open a public issue, and do not disclose the report before a fix is published. You can expect an initial response within 3 days.

## Threat model

doc2mcp fetches documentation the operator did not write and passes that text to a model. A hostile page can attempt prompt injection.

Curation sends summaries, descriptions, tags, and examples to the model inside `<<<UNTRUSTED DOC>>>` markers and tells the model that text is data. Admin, internal, deprecated, webhook-management, and bulk-destructive operations are removed in code after the model responds. A description that asks to publish those operations does not put them in `tools.yaml`. Unsupported authentication is excluded the same way.

The tool then generates code, and that code is executed. It also runs a generated test suite. Verification can call live APIs with real credentials.

## Guarantees

- Credentials are read from environment variables only.
- Credentials are never written into generated code, fixtures, or prompts.
- Recorded cassettes drop `Authorization`, `Cookie`, `Set-Cookie`, and API-key headers by storing no headers. Secret values from the process environment, and bearer-shaped strings in bodies, are replaced before the file is written.
- Verification runs behind a network allowlist. The default allowlist is `127.0.0.1`. The generated client checks `DOC2MCP_ALLOWED_HOSTS` in process, and a local proxy allows only those hosts. A rejected host is logged as `blocked-by-allowlist` and is not dialed.
- Loopback (`127.0.0.1`, `localhost`, `::1`) is a sandbox. Fixture mode does not dial a spec host. `--live` dials the spec server, and a host outside the sandbox set needs `--allow-host` before any tool call. `--sandbox` to a host outside that set also needs `--allow-host`.
- Under `--live`, tools marked destructive, and tools that are not idempotent, are not called unless `--allow-writes` is set.
- The proxy can read HTTP request and response bodies. An HTTPS `CONNECT` tunnel is checked against the allowlist and the body is not inspected.
- Report excerpts are trimmed upstream bodies and are not redacted. Treat `report/` as sensitive when a live or sandbox body may contain secrets.
