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
- Credentials are never written into generated code, fixtures, cassettes, reports, or prompts.
- Verification runs behind a network allowlist.
