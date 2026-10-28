# Limitations

mcp4api is not a general code generator, an API gateway, or a hosted service.

- OpenAPI input only (3.0, 3.1, 3.2, and Swagger 2.0). No HTML docs pages, runbooks, Postman collections, or GraphQL. An HTML page is rejected. Pass the URL of the OpenAPI document itself.
- The emitted server authenticates with an API key or a bearer token from environment variables. An API key may be sent in a header, a query parameter, or a cookie. Endpoints that need another scheme are excluded and listed in the report.
- The emitted server does not paginate, retry, or rate-limit. List tools return the first page. There is no problem+json parser and no MCP OAuth.
- No repair loop. A failing tool is reported as a known gap and left out of the server. It is not fixed automatically. With the default `--on-failure exclude`, the failed tool is left out of `generated/` and still listed in `tools.yaml`.
- No PII redaction. The emitted server never logs bodies. The report's failure details include trimmed upstream responses, which can hold real data when you verify against a sandbox or a live API.
- Python output only.
- No hosted service and no GitHub Action. The exit codes are the CI interface. Streamable HTTP listens on localhost.
- Agent curation needs an Anthropic API key. `--curation heuristic` does not.
- Model output is not deterministic, so reproducibility comes from committing `tools.yaml`.
- `--isolate` is Linux-only, and only when `bwrap` or `unshare` is installed. Synthesized fixtures are weaker evidence than recorded ones: the body was invented from the schema, so a pass does not show that a real server accepts the call.

Callbacks, links, and webhooks are reported as findings and are not part of the endpoint model. When a media type has both `schema` and `itemSchema`, the schema for the complete content is the one kept.
