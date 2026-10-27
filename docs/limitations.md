# Limitations

doc2mcp is not a general code generator, an API gateway, or a hosted service.

Ingest accepts OpenAPI 3.0, 3.1, and 3.2, and Swagger 2.0, as a local file or a URL. An HTML page is rejected. Pass the URL of the OpenAPI document itself.

Callbacks, links, and webhooks are reported as findings and are not part of the endpoint model. When a media type has both `schema` and `itemSchema`, the schema for the complete content is the one kept.

Emit writes a Python MCP server. Authentication is an API key, in a header, query parameter, or cookie, or a bearer token. There is no pagination, retry, rate limit, problem+json parser, PII redaction, or MCP OAuth, and there is no output language other than Python. A list operation returns the first page. Streamable HTTP listens on localhost.

`--isolate` runs only on Linux, and only when `bwrap` or `unshare` is installed. A synthesized fixture is weak evidence: the body was invented from the schema, so a pass does not show that a real server accepts the call. Verification reports a failure and does not repair the spec, the manifest, or the server. With the default `--on-failure exclude`, the failed tool is left out of `generated/` and still listed in `tools.yaml`. Report excerpts are trimmed upstream bodies and are not redacted.
