# Demo

Run mcp4api on your own OpenAPI document.

## 1. Run it the night before

```bash
mcp4api openapi.yaml --out .
```

Agent curation reads `ANTHROPIC_API_KEY`. `--curation heuristic` does not. Commit `tools.yaml` and `tests/cassettes/` when the same result should come back without a model call:

```bash
mcp4api openapi.yaml --offline --out .
```

## 2. Show the tool list

The run prints each tool, its status, and the evidence. `generated/README.md` lists the tools that shipped.

## 3. Add the server to an agent

From the artifact root, add the server to Claude Code ([MCP](https://docs.claude.com/en/docs/claude-code/mcp)):

```bash
claude mcp add <slug> -- uvx --from ./generated <slug>-mcp
```

Set the API key or bearer token named in `generated/README.md`, then call one tool against the real API.

## 4. Show the report, including failures

`report/verification.md` lists every tool. Known gaps name the field that failed. Exit code 3 means at least one tool is a known gap.
