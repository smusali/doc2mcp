from __future__ import annotations

import html
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from mcp4api.model import JsonObject, JsonObjectField, JsonValue, dump_json, expect_json

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_DOT = " \u00b7 "


class Summary(BaseModel):
    model_config = _FROZEN

    generated: int
    verified: int
    synthesized: int
    gaps: int
    skipped: int


class ToolRow(BaseModel):
    model_config = _FROZEN

    tool: str
    endpoints: list[str]
    status: str
    failed: str
    reason: str
    calls: int
    evidence: str


class Failure(BaseModel):
    model_config = _FROZEN

    tool: str
    call: str
    arguments: JsonObjectField
    assertion: str
    expected: str
    actual: str
    field: str
    upstream_status: int | None
    body: str


class FindingRow(BaseModel):
    model_config = _FROZEN

    code: str
    severity: str
    endpoint_id: str | None
    pointer: str
    message: str


class UsageRow(BaseModel):
    model_config = _FROZEN

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    estimated_usd: float


class Report(BaseModel):
    model_config = _FROZEN

    mcp4api: str
    source_uri: str
    source_sha256: str
    manifest_sha256: str
    model: str
    curation_mode: str
    evidence: str
    allowlist: list[str]
    summary: Summary
    tools: list[ToolRow]
    failures: list[Failure]
    excluded: list[str]
    findings: list[FindingRow]
    usage: UsageRow
    started_at: str | None = None
    duration_ms: int | None = None


def summary_line(report: Report) -> str:
    """Format the one-line verification summary.

    Args:
        report: Verification report.

    Returns:
        The summary line, including a synthesized count only when it is non-zero.
    """
    summary = report.summary
    parts = [
        _counted(summary.generated, "tool generated", "tools generated"),
        f"{summary.verified} verified",
    ]
    if summary.synthesized:
        parts.append(f"{summary.synthesized} synthesized")
    parts.append(_counted(summary.gaps, "known gap", "known gaps"))
    parts.append(f"{summary.skipped} skipped")
    return _DOT.join(parts)


def write_report(
    report: Report, directory: Path, *, html_report: bool, deterministic: bool
) -> None:
    """Write the JSON report and the Markdown view.

    Args:
        report: Verification report.
        directory: Directory that receives ``verification.json`` and ``verification.md``.
        html_report: Also write a single HTML file with inline CSS.
        deterministic: Omit the start time and duration.

    Returns:
        None.
    """
    directory.mkdir(parents=True, exist_ok=True)
    payload = _payload(report, deterministic=deterministic)
    _write(directory / "verification.json", dump_json(payload))
    _write(directory / "verification.md", _markdown(report, deterministic=deterministic))
    if html_report:
        _write(directory / "verification.html", _html(report))


def print_report(report: Report, *, quiet: bool) -> None:
    """Print the verification summary.

    Args:
        report: Verification report.
        quiet: Print the summary line only.

    Returns:
        None.
    """
    line = summary_line(report)
    if quiet:
        sys.stdout.write(f"{line}\n")
        return
    table = Table(title=line)
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Evidence")
    table.add_column("Calls", justify="right")
    for row in report.tools:
        table.add_row(row.tool, row.status, row.evidence, str(row.calls))
    Console(file=sys.stdout).print(table)


def _payload(report: Report, *, deterministic: bool) -> JsonObject:
    excluded = {"started_at", "duration_ms"} if deterministic else set()
    dumped = report.model_dump(mode="json", exclude=excluded)
    parsed = expect_json(dumped)
    if not isinstance(parsed, dict):
        raise TypeError("report must be an object")
    return parsed


def _markdown(report: Report, *, deterministic: bool) -> str:
    lines = [
        "# Verification",
        "",
        f"- mcp4api: {report.mcp4api}",
        f"- source: {report.source_uri} (`{report.source_sha256}`)",
        f"- manifest: `{report.manifest_sha256}`",
        f"- model: {report.model}",
        f"- curation: {report.curation_mode}",
        f"- evidence: {report.evidence}",
        f"- allowlist: {', '.join(report.allowlist)}",
    ]
    if not deterministic and report.started_at is not None:
        lines.append(f"- started: {report.started_at}")
        lines.append(f"- duration_ms: {report.duration_ms}")
    lines.extend(["", summary_line(report), "", "## Tools", ""])
    lines.append("| Tool | Endpoints | Status | Evidence | Calls |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in report.tools:
        endpoints = ", ".join(row.endpoints) or "-"
        lines.append(
            f"| {_cell(row.tool)} | {_cell(endpoints)} | {_cell(row.status)} | "
            f"{_cell(row.evidence)} | {row.calls} |"
        )
    lines.extend(["", "## Known gaps", ""])
    if not report.failures:
        lines.append("None.")
    lines.extend(_failure_sections(report.failures))
    lines.extend(["", "## Excluded", ""])
    lines.extend(_sentences(report.excluded, "is excluded."))
    lines.extend(["", "## Findings", ""])
    if len(report.findings) == 1:
        lines.append(_finding_sentence(report.findings[0]))
    elif report.findings:
        for finding in report.findings:
            lines.append(f"- {_finding_sentence(finding)}")
    else:
        lines.append("None.")
    usage = report.usage
    lines.extend(
        [
            "",
            "## Tokens",
            "",
            f"- input: {usage.input_tokens}",
            f"- output: {usage.output_tokens}",
            f"- cache read: {usage.cache_read_input_tokens}",
            f"- cache write: {usage.cache_write_input_tokens}",
            f"- estimated USD: {usage.estimated_usd}",
            "",
        ]
    )
    return "\n".join(lines)


def _counted(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _sentences(items: list[str], suffix: str) -> list[str]:
    if not items:
        return ["None."]
    if len(items) == 1:
        return [f"{items[0]} {suffix}"]
    return [f"- {name}" for name in items]


def _finding_sentence(finding: FindingRow) -> str:
    where = finding.endpoint_id or finding.pointer
    return f"{finding.severity} `{finding.code}` {where}: {finding.message}"


def _failure_sections(failures: list[Failure]) -> list[str]:
    grouped: dict[str, list[Failure]] = {}
    for failure in failures:
        grouped.setdefault(failure.tool, []).append(failure)
    lines: list[str] = []
    for tool, rows in grouped.items():
        if lines:
            lines.append("")
        lines.extend([f"### {tool}", ""])
        for index, failure in enumerate(rows):
            if index:
                lines.append("")
            lines.extend(_failure_lines(failure))
    return lines


def _failure_lines(failure: Failure) -> list[str]:
    status = "none" if failure.upstream_status is None else str(failure.upstream_status)
    field = failure.field or "-"
    return [
        f"- call: `{failure.call}`",
        f"- assertion: {failure.assertion}",
        f"- field: {field}",
        f"- expected: {failure.expected}",
        f"- actual: {failure.actual}",
        f"- upstream status: {status}",
        "",
        "```json",
        failure.body,
        "```",
    ]


def _html(report: Report) -> str:
    rows = []
    for row in report.tools:
        rows.append(
            "<tr>"
            f"<td>{_esc(row.tool)}</td>"
            f"<td>{_esc(', '.join(row.endpoints))}</td>"
            f"<td>{_esc(row.status)}</td>"
            f"<td>{_esc(row.evidence)}</td>"
            f"<td>{row.calls}</td>"
            "</tr>"
        )
    gaps = []
    for failure in report.failures:
        gaps.append(
            "<section>"
            f"<h3>{_esc(failure.tool)}</h3>"
            f"<p>{_esc(failure.assertion)} {_esc(failure.field)}</p>"
            f"<pre>{_esc(failure.body)}</pre>"
            "</section>"
        )
    excluded = ", ".join(report.excluded) if report.excluded else "None."
    body = (
        f"<h1>Verification</h1><p>{_esc(summary_line(report))}</p>"
        "<table><thead><tr><th>Tool</th><th>Endpoints</th><th>Status</th>"
        "<th>Evidence</th><th>Calls</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"<h2>Known gaps</h2>{''.join(gaps) or '<p>None.</p>'}"
        f"<h2>Excluded</h2><p>{_esc(excluded)}</p>"
    )
    style = (
        "body{font-family:sans-serif;margin:2rem;color:#222}"
        "table{border-collapse:collapse;margin:1rem 0}"
        "td,th{border:1px solid #ccc;padding:.4rem;text-align:left}"
        "pre{white-space:pre-wrap}"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>Verification</title><style>{style}</style></head><body>{body}</body></html>\n"
    )


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


Failure.model_rebuild(_types_namespace={"JsonValue": JsonValue, "JsonObject": JsonObject})
Report.model_rebuild(_types_namespace={"JsonValue": JsonValue, "JsonObject": JsonObject})
