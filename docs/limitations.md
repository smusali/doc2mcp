# Limitations

doc2mcp is not a general code generator, an API gateway, or a hosted service.

Ingest accepts OpenAPI 3.0, 3.1, and 3.2, and Swagger 2.0, as a local file or a URL. An HTML page is rejected. Pass the URL of the OpenAPI document itself.

Callbacks, links, and webhooks are reported as findings and are not part of the endpoint model. When a media type has both `schema` and `itemSchema`, the schema for the complete content is the one kept.

Curate writes `tools.yaml`. Emit and verify still only check that their inputs exist. A full run stops at emit, so exit 0 stays reserved for a run that verified every generated tool.
