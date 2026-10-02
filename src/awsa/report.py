"""Report rendering: JSON for machines, Markdown for humans, plain for pipes.

The Markdown renderer is the primary artefact. Its job is to make the evidence
legible at a glance: every claim shows its confidence bucket, its corroborating
domain count, and a clickable quote, so a reader can audit any line without
re-running the agent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from .errors import SerializationError
from .models import Claim, ResearchReport

__all__ = [
    "dump_json",
    "render",
    "render_json",
    "render_markdown",
    "render_plain",
]

_LABEL_MARK = {"high": "HIGH", "medium": "MED", "low": "LOW", "unknown": "----"}


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _fence(text: str, limit: int = 220) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def render_markdown(report: ResearchReport, *, include_sources: bool = True) -> str:
    """Render a full Markdown research report.

    Args:
        report: The completed report.
        include_sources: Append the source list and run metadata.

    Returns:
        Markdown text with a claims table, per-claim evidence, and a footer.
    """
    lines: list[str] = [f"# Research report: {report.subject}", ""]

    answered = report.answered
    total = len(report.claims)
    corroborated = sum(1 for c in report.claims if c.corroborated and c.value)
    lines.append(
        f"**{answered}/{total}** fields answered · "
        f"**{corroborated}** corroborated by 2+ domains · "
        f"{len(report.sources)} page(s) read · {report.duration_seconds:.1f}s"
    )
    lines.append("")

    if not report.claims:
        lines.append("> No claims were produced. See `notes` in the JSON output.")
        return "\n".join(lines) + "\n"

    lines.append("## Claims")
    lines.append("")
    lines.append("| Field | Value | Confidence | Sources | Corroborated |")
    lines.append("| --- | --- | --- | --- | --- |")
    for claim in report.claims:
        label = str(claim.label)
        lines.append(
            "| {field} | {value} | {label} | {support} | {corr} |".format(
                field=_escape_cell(claim.field),
                value=_escape_cell(claim.value) or "_(not found)_",
                label=f"{_LABEL_MARK.get(label, label)} {claim.confidence:.2f}",
                support=claim.support,
                corr="yes" if claim.corroborated else "no",
            )
        )
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    for claim in report.claims:
        lines.append(f"### {claim.field}")
        lines.append("")
        if not claim.value:
            lines.append("_No value was found._")
            lines.append("")
            continue
        for evidence in claim.evidence:
            lines.append(f"- > {_fence(evidence.quote)}")
            lines.append(
                f"  — [{_fence(evidence.source_title or evidence.source_url, 60)}]"
                f"({evidence.source_url})"
            )
        if claim.conflicts:
            lines.append("")
            lines.append("Conflicting values found:")
            for conflict in claim.conflicts:
                lines.append(f"- `{_fence(conflict.value, 80)}` (support {conflict.support})")
        lines.append("")

    if report.unresolved_fields:
        lines.append("## Unresolved")
        lines.append("")
        for name in report.unresolved_fields:
            lines.append(f"- {name}")
        lines.append("")

    if include_sources:
        lines.append("## Sources")
        lines.append("")
        for source in report.sources:
            lines.append(
                f"- [{_fence(source.title or source.host, 70)}]({source.final_url}) "
                f"— {source.word_count} words" + (" _(cached)_" if source.from_cache else "")
            )
        lines.append("")
        usage = report.usage
        lines.append("## Run")
        lines.append("")
        lines.append(
            f"- Queries: {len(report.queries)} (`{'`, `'.join(report.queries) or 'none'}`)"
        )
        lines.append(
            f"- Pages fetched: {usage.pages_fetched} (cache hits: {usage.pages_from_cache})"
        )
        lines.append(f"- Bytes downloaded: {usage.bytes_downloaded:,}")
        lines.append(
            f"- LLM calls: {usage.llm_calls} "
            f"({usage.llm_input_tokens} in / {usage.llm_output_tokens} out)"
        )
        lines.append(
            f"- Blocked: {usage.robots_denials} robots, {usage.blocked_urls} unsafe, "
            f"{usage.fetch_errors} fetch errors"
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(report: ResearchReport, *, indent: int = 2) -> str:
    """Render the report as a JSON document."""
    return report.to_json(indent=indent)


def render_plain(report: ResearchReport) -> str:
    """Render ``field: value`` lines, suitable for piping into other tools."""
    lines = [f"# {report.subject}"]
    for claim in report.claims:
        lines.append(f"{claim.field}: {claim.value or '(not found)'}")
    return "\n".join(lines) + "\n"


def render(report: ResearchReport, fmt: str = "markdown") -> str:
    """Dispatch to a renderer by name.

    Args:
        report: The report to render.
        fmt: One of ``markdown``, ``json``, ``plain``.

    Returns:
        Rendered text. Unknown formats fall back to Markdown.
    """
    if fmt == "json":
        return render_json(report)
    if fmt in {"plain", "text", "kv"}:
        return render_plain(report)
    return render_markdown(report)


def claims_to_rows(claims: Sequence[Claim]) -> list[dict[str, Any]]:
    """Flatten claims into rows, for callers writing their own spreadsheet export."""
    rows: list[dict[str, Any]] = []
    for claim in claims:
        row: dict[str, Any] = {
            "field": claim.field,
            "value": claim.value,
            "confidence": round(claim.confidence, 3),
            "label": str(claim.label),
            "corroborated": claim.corroborated,
            "domains": list(claim.independent_domains),
            "evidence_urls": [e.source_url for e in claim.evidence],
            "conflicts": [c.value for c in claim.conflicts],
        }
        rows.append(row)
    return rows


def dump_json(payload: object, *, indent: int = 2) -> str:
    """JSON-serialise a payload, refusing values JSON cannot represent.

    ``default=str`` is the usual shortcut, and it is a trap here: it turns a
    malformed claim field into a plausible-looking string and ships it. A report
    that silently coerces is worse than one that fails, because the corruption
    surfaces downstream, in someone else's analysis, with no error to trace.
    So an unserialisable value is a hard error naming the offending key.
    """
    try:
        return json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False)
    except TypeError as exc:
        raise SerializationError(_explain_unsupported(payload, exc)) from exc


def _explain_unsupported(payload: object, exc: TypeError) -> str:
    """Name the path of the first unserialisable value, for an actionable error.

    ``json`` only ever reports the offending *type*, not the field it came from,
    so a bare message leaves the caller hunting through a nested structure.
    """
    bad = re.search(r"of type (\w+)", str(exc))
    kind = bad.group(1) if bad else "unknown"
    for path, value in _walk(payload):
        if type(value).__name__ == kind:
            return (
                f"cannot serialise {path} of type {kind} to JSON; "
                "fix the field or add a serializer for it"
            )
    return f"cannot serialise payload to JSON: {exc}"


def _walk(payload: object, path: str = "$") -> Iterable[tuple[str, object]]:
    """Yield ``(json-ish path, value)`` for every value reachable from a payload."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield from _walk(value, f"{path}[{index}]")
    else:
        yield path, payload
