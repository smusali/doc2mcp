# Limitations

doc2mcp is not a general code generator, an API gateway, or a hosted service.

Ingest accepts OpenAPI 3.0, 3.1, and 3.2, and Swagger 2.0, as a local file or a URL. An HTML page is rejected. Pass the URL of the OpenAPI document itself.

Callbacks, links, and webhooks are reported as findings and are not part of the endpoint model. When a media type has both `schema` and `itemSchema`, the schema for the complete content is the one kept.

Emit writes a Python MCP server. Authentication is an API key, in a header, query parameter, or cookie, or a bearer token. There is no pagination, retry, rate limit, problem+json parser, PII redaction, or MCP OAuth, and there is no output language other than Python. A list operation returns the first page. Streamable HTTP listens on localhost. Verify still only checks that its inputs exist. A full run stops at verify, so exit 0 stays reserved for a run that verified every generated tool.
