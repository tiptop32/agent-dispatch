"""Source-only context blocks; conservative boundaries, never Markdown rewriting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Origin = Literal["user", "daemon_handoff", "daemon_review"]


@dataclass(frozen=True)
class ContextSpan:
    start: int
    end: int
    origin: Origin
    source_task_id: str | None


@dataclass(frozen=True)
class ContextBlock:
    id: str
    start: int
    end: int
    origin: Origin
    source_task_id: str | None


# Physical lines, not str.splitlines(): Unicode separators are source characters.
_LINE = re.compile(r"[^\r\n]*(?:\r\n?|\n|$)")
_HEADING = re.compile(r"(#{1,6})[ \t]")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_AMBIGUOUS = re.compile(
    r"(?:[ \t]*(?:=+|-+)[ \t]*$"
    r"| {0,3}<(?:!--|\?|!|/?[A-Za-z])"
    r"| {0,3}>[ \t]*#{1,6}[ \t]"
    r"| {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+#{1,6}[ \t])"
)


def _validate_cover(
    context: str, ranges: tuple[ContextSpan, ...] | tuple[ContextBlock, ...]
) -> None:
    cursor = 0
    for part in ranges:
        if (
            type(part.start) is not int
            or type(part.end) is not int
            or part.start != cursor
            or not part.start < part.end <= len(context)
        ):
            raise ValueError("Context ranges must be a contiguous nonempty source cover")
        if part.origin not in ("user", "daemon_handoff", "daemon_review"):
            raise ValueError("Invalid context origin")
        if part.source_task_id is not None and (
            not isinstance(part.source_task_id, str) or not part.source_task_id.strip()
        ):
            raise ValueError("Invalid context source task ID")
        if part.origin != "user" and part.source_task_id is None:
            raise ValueError("Daemon context requires a source task ID")
        cursor = part.end
    if cursor != len(context):
        raise ValueError("Context ranges must cover the entire source")


def _user_boundaries(context: str, span: ContextSpan) -> list[int]:
    """Return shallowest heading offsets, or none when the span is ambiguous."""
    headings: list[int] = []
    shallowest = 7
    fence_char: str | None = None
    fence_length = 0
    for match in _LINE.finditer(context, span.start, span.end):
        if match.start() == match.end():
            continue
        line = match.group().rstrip("\r\n")
        fence = _FENCE.fullmatch(line)
        if fence_char is not None:
            if (
                fence is not None
                and fence[1][0] == fence_char
                and len(fence[1]) >= fence_length
                and not fence[2].strip(" \t")
            ):
                fence_char = None
            continue
        if fence is not None:
            marker, info = fence[1], fence[2]
            if marker[0] == "`" and "`" in info:
                return []
            fence_char, fence_length = marker[0], len(marker)
            continue
        if _AMBIGUOUS.match(line):
            return []
        heading = _HEADING.match(line)
        if heading is not None:
            level = len(heading[1])
            if level < shallowest:
                shallowest = level
                headings.clear()
            if level == shallowest:
                headings.append(match.start())
    return [] if fence_char is not None else headings


def split_context(
    context: str, *, provenance: tuple[ContextSpan, ...] | None = None
) -> tuple[ContextBlock, ...]:
    """Partition source offsets; supplied provenance is authoritative, even if empty.

    Empty source has an empty cover. Admission of an opt-in request and envelope
    limits belong to the caller, not to this segmentation primitive.
    """
    context.encode("utf-8", errors="strict")
    if provenance is None:
        provenance = (ContextSpan(0, len(context), "user", None),) if context else ()
    if not isinstance(provenance, tuple) or any(
        not isinstance(span, ContextSpan) for span in provenance
    ):
        raise ValueError("Invalid context provenance")
    _validate_cover(context, provenance)
    blocks: list[ContextBlock] = []
    for span in provenance:
        start = span.start
        boundaries = _user_boundaries(context, span) if span.origin == "user" else []
        for end in (*boundaries, span.end):
            if end == start:  # A heading at the span start is not a preamble.
                continue
            blocks.append(
                ContextBlock(f"b{len(blocks):04d}", start, end, span.origin, span.source_task_id)
            )
            start = end
    return tuple(blocks)


def assemble_context(
    context: str, blocks: tuple[ContextBlock, ...], kept_ids: frozenset[str]
) -> str:
    """Validate the complete source cover before deleting any unselected slices."""
    context.encode("utf-8", errors="strict")
    if not isinstance(blocks, tuple) or any(not isinstance(b, ContextBlock) for b in blocks):
        raise ValueError("Invalid context blocks")
    _validate_cover(context, blocks)
    if any(block.id != f"b{index:04d}" for index, block in enumerate(blocks)):
        raise ValueError("Context block IDs must be unique and in stable source order")
    if not isinstance(kept_ids, frozenset) or any(not isinstance(i, str) for i in kept_ids):
        raise ValueError("Selected context IDs must be a frozenset of unique strings")
    if not kept_ids.issubset(block.id for block in blocks):
        raise ValueError("Unknown selected context block ID")
    if any(block.origin != "user" and block.id not in kept_ids for block in blocks):
        raise ValueError("Pinned context blocks must be retained")
    return "".join(context[b.start : b.end] for b in blocks if b.id in kept_ids)
