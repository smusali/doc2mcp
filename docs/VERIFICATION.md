# Verification

`mcp4api verify` calls every generated tool through the MCP client over stdio and writes a report. The primary command does this after emit. `mcp4api record` runs the same plan against a sandbox and writes cassettes.

Verify emits the full server first, so a tool left out by an earlier failure is called again. It then runs the plan and applies `--on-failure`.

## Calls

Each tool is called in this order:

1. Required inputs only.
2. Every input.
3. One negative call that omits one required input. A tool with no required inputs skips this call.

A negative call passes when the server returns an error result with a message and the fixture saw no upstream request. An upstream response on that call fails the status check.

Argument values come from `verify.example_args`, then from parameter and body examples on the endpoint, then from one value drawn from the input schema. The draw is deterministic. `depends_on` orders tools. After a producer call, a consumer input is replaced when the producer output has the same property name, or when the input ends in `_id` and the output has `id`.

Tool order is the dependency order, and otherwise read-only tools, then other non-destructive tools, then destructive tools, then name.

## Assertions

Each call records:

| Assertion | Passes when |
| --- | --- |
| `transport` | A tool result came back. Timeouts and crashes fail here. |
| `status` | Each upstream status is the endpoint's documented success code, the lowest 2xx. |
| `schema` | `structuredContent` matches the tool output schema, and each upstream body matches that step's response schema. |
| `required` | Every required field in the output projection is present. Null fails unless the schema allows null. |
| `error-shape` | A failure is an error result with a message, not an exception or a protocol error. |

The field name on a schema failure comes from the schema error path. A missing required field is reported as `required`.

## Evidence

A tool is `verified` when every planned call passed and at least one happy-path call used a cassette, a sandbox response, or a live response. A pass that used only synthesized bodies is `verified (synthesized)` and is counted on its own. Anything else that ran and failed is a known gap. A skipped write is `skipped: write-guard`.

Exit 3 when any tool is a known gap, including `blocked-by-allowlist`. Synthesized passes and write-guard skips do not by themselves exit 3.

`--on-failure exclude` re-emits `generated/` without the failed tools. `tools.yaml` is not rewritten. `--on-failure keep` leaves the tool in place and prefixes its description with `WARNING: verification failed. `.

## Cassettes

The default upstream is a fixture server on `127.0.0.1`. `{SLUG}_BASE_URL` points at it. Cassettes live in `<out>/tests/cassettes/<tool>.json`. Replay matches method, path, query, and body. Headers are not stored.

When a request has no cassette, the fixture builds a body from the response example, or from the response schema, and marks that call `synthesized`.

`mcp4api record --sandbox URL` runs the plan against that URL and writes cassettes. At record time the runner drops authorization, cookie, and API-key headers by not storing headers, and replaces secret values from the process environment plus bearer-shaped strings in bodies. `--offline` makes `record`, `--sandbox`, and `--live` exit 2 before any connection.

## Allowlist

The default allowlist is `127.0.0.1`. `--allow-host` adds hosts.

The runner sets `MCP4API_ALLOWED_HOSTS`. The generated client refuses any other host before it sends. A local HTTP proxy is the second layer: it allows only those hosts, sets `HTTP_PROXY` and `HTTPS_PROXY`, and clears `NO_PROXY`. A rejected host is logged as `blocked-by-allowlist` and is not dialed.

The proxy can read HTTP request and response bodies, which is how status and body checks see the upstream exchange. An HTTPS `CONNECT` tunnel is checked against the allowlist, and the body inside the tunnel is not visible.

Loopback (`127.0.0.1`, `localhost`, `::1`) is a sandbox. Fixture mode never dials a spec host. `--live` dials the spec server. A host outside the sandbox set needs `--allow-host`, and verify exits 2 before any tool call when that flag is missing. `--sandbox` to a host outside that set also needs `--allow-host`.

Under `--live`, a tool with `destructiveHint: true`, or with `idempotentHint: false`, is not called unless `--allow-writes` is set. The report marks it `skipped: write-guard`.

`--isolate` reruns the proxy, fixture, and server inside `bwrap --unshare-net` or `unshare --net`. It is Linux-only. A missing helper, or any other operating system, exits 2.

## Report

`report/verification.json` has `report_version: "report-v1"`. It includes the mcp4api version, the source URI and sha256, the manifest sha256, the model, the curation mode, the evidence mode, the allowlist, summary counts, one row per tool, and for each failure the arguments, the expected and actual values, and a trimmed upstream body. It also lists exclusions, ingest findings, and curation tokens and cost.

`--deterministic` omits `started_at` and durations. Keys are sorted. The encoding is UTF-8 and newlines are LF.

`report/verification.md` is the same report for a pull request. The summary line looks like `15 tools generated · 14 verified · 1 known gaps · 0 skipped`. A non-zero synthesized count is inserted after `verified`. `--html` writes one `report/verification.html` file with inline CSS and no scripts. `--quiet` prints the summary line only. Otherwise a table goes to stdout.

`docs/schemas/report-v1.json` is generated from the report model. A test fails when the file drifts.

## Limits

`--isolate` is Linux-only. Synthesized fixtures are weak evidence. Failures are reported rather than repaired. Failure excerpts are trimmed upstream bodies and are not redacted.
