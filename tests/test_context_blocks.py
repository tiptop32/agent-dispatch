"""Deterministic boundary/retention cases; no selector, network or Markdown parser."""

from dataclasses import FrozenInstanceError, replace
from itertools import combinations

import pytest

from agent_dispatch.dispatch.context_blocks import (
    ContextBlock,
    ContextSpan,
    assemble_context,
    split_context,
)


def test_all_some_none_retain_exact_source_sections_and_original_order():
    parts = (
        "  preamble\r\n\r\n",
        "# First\t###\r\nKeep café e\u0301 😀\r\n\r\n",
        "# Second\r\n删除\t\x00\r\n\r\n",
        "# Third\r\nlast line without newline",
    )
    context = "".join(parts)
    blocks = split_context(context)
    assert blocks == (
        ContextBlock("b0000", 0, len(parts[0]), "user", None),
        ContextBlock("b0001", len(parts[0]), len(parts[0] + parts[1]), "user", None),
        ContextBlock("b0002", len(parts[0] + parts[1]), len(context) - len(parts[3]), "user", None),
        ContextBlock("b0003", len(context) - len(parts[3]), len(context), "user", None),
    )
    # Every subset has an independent literal-section oracle, including none/all.
    for size in range(5):
        for indices in combinations(range(4), size):
            selected = frozenset(f"b{i:04d}" for i in reversed(indices))
            expected = "".join(parts[i] for i in indices)
            assert assemble_context(context, blocks, selected) == expected


def test_shallowest_headings_own_nested_sections_and_preceding_separators():
    preamble = "## Deeper heading before any root\nintro\n\n"
    first = "# Root A\n## Child\nchild fact\n### Grandchild\nrequired exception\n\n"
    second = "# Root B\nother fact\n"
    context = preamble + first + second
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0001"})) == first
    assert assemble_context(context, blocks, frozenset({"b0000", "b0002"})) == preamble + second


def test_consecutive_headings_are_nonempty_sections_without_synthetic_separators():
    context = "##\tOne\n## Two\n## Three"
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0000", "b0002"})) == ("##\tOne\n## Three")
    assert assemble_context(context, blocks, frozenset({"b0001"})) == "## Two\n"


@pytest.mark.parametrize(
    "body",
    [
        "- item\n  continuation\n  - nested item\n\n1. numbered\n",
        "| name | value |\n| --- | --- |\n| key | required |\n",
        "    # indented code\n\t# tab-indented code\n",
        " # space-indented heading is not a boundary\n",
        "####### seven hashes are not an ATX boundary\n#no-space\n",
    ],
    ids=["list", "table", "indented-code", "indented-heading", "non-headings"],
)
def test_structures_stay_with_their_section_without_suppressing_root_boundaries(body):
    first = "# First\n" + body + "\n"
    last = "# Last\nkeep me\n"
    context = first + last
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0000"})) == first
    assert assemble_context(context, blocks, frozenset({"b0001"})) == last


@pytest.mark.parametrize(
    ("opening", "inside", "closing"),
    [
        ("```python", "# hidden\n\n## nested\n", "```"),
        ("   ~~~language", "# hidden\n---\n<div>\n- # embedded\n", "  ~~~~~\t "),
        ("````python", "```\n# still inside\n~~~~\n", " `````\t"),
        ("~~~", "~~~ trailing text\n# still inside\n", "~~~"),
        ("```", "    ```\n# still inside\n", "```"),
        ("~~~info`allowed", "# hidden\n", "~~~"),
    ],
    ids=["backticks", "tildes", "short-or-wrong-close", "close-info", "close-indent", "tilde-info"],
)
def test_fences_hide_headings_and_ambiguities_until_a_valid_close(opening, inside, closing):
    first = "# First\n" + opening + "\n" + inside + closing + "\n\n"
    last = "# Last\nvisible\n"
    context = first + last
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0000"})) == first
    assert assemble_context(context, blocks, frozenset({"b0001"})) == last


@pytest.mark.parametrize(
    "ambiguous",
    [
        "```lang`bad\n```\n",
        "```unclosed\n# not a boundary\n",
        "~~~unclosed\n# not a boundary\n",
        "````\n```\n",
        "---\n",
        " \t===\t\n",
        "--\n",
        "<!-- comment\n",
        "  <?instruction\n",
        "<!DOCTYPE html>\n",
        "   <section>\n",
        "</section>\n",
        "> # quote heading\n",
        "   >\t###### quote heading\n",
        "- # list heading\n",
        "+ ## list heading\n",
        "  * ### list heading\n",
        "1. # numbered heading\n",
        "123456789)\t## numbered heading\n",
    ],
    ids=[
        "backtick-info",
        "open-backticks",
        "open-tildes",
        "short-close",
        "thematic",
        "setext",
        "short-dashes",
        "comment",
        "processing",
        "declaration",
        "html-open",
        "html-close",
        "quote",
        "indented-quote",
        "dash-list",
        "plus-list",
        "star-list",
        "numbered-dot",
        "numbered-parenthesis",
    ],
)
def test_ambiguity_keeps_entire_user_span_atomic(ambiguous):
    context = "# First\nneeded\n" + ambiguous + "# Last\nexception\n"
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0000"})) == context
    assert assemble_context(context, blocks, frozenset()) == ""
    with pytest.raises(ValueError, match="Unknown"):
        assemble_context(context, blocks, frozenset({"b0001"}))


@pytest.mark.parametrize(
    "context",
    [
        "first paragraph\n\nsecond paragraph\n",
        " \r\n\t ",
        "plain\u2028# not a physical line\u2029## neither\x85# neither\n",
    ],
)
def test_no_physical_atx_headings_means_one_whole_span(context):
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0000"})) == context
    assert assemble_context(context, blocks, frozenset()) == ""


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_physical_line_endings_preserved_without_normalization(ending):
    first = f"# One{ending}é😀{ending}{ending}"
    second = f"# Two{ending}e\u0301{ending}"
    context = first + second
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0000"})) == first
    assert assemble_context(context, blocks, frozenset({"b0001"})) == second
    assert assemble_context(context, blocks, frozenset({"b0000", "b0001"})) == context


def test_provenance_pins_atomic_daemon_spans_with_separators_and_global_ids():
    user = "# One\nremove\n# Two\nkeep\n"
    handoff = "\r\n\r\n# Handoff\r\n## Nested\r\n# Another\r\n"
    review = "\n\n# Review\n---\n# Feedback\n"
    final_user = "# Three\nremove\n# Four\nkeep\n"
    context = user + handoff + review + final_user
    a, b, c = len(user), len(user + handoff), len(user + handoff + review)
    blocks = split_context(
        context,
        provenance=(
            ContextSpan(0, a, "user", None),
            ContextSpan(a, b, "daemon_handoff", "parent-1"),
            ContextSpan(b, c, "daemon_review", "review-1"),
            ContextSpan(c, len(context), "user", "parent-2"),
        ),
    )
    assert blocks[2:4] == (
        ContextBlock("b0002", a, b, "daemon_handoff", "parent-1"),
        ContextBlock("b0003", b, c, "daemon_review", "review-1"),
    )
    assert blocks[5].source_task_id == "parent-2"
    assert assemble_context(context, blocks, frozenset({"b0002", "b0003"})) == handoff + review
    assert assemble_context(context, blocks, frozenset({"b0001", "b0002", "b0003", "b0005"})) == (
        "# Two\nkeep\n" + handoff + review + "# Four\nkeep\n"
    )
    assert assemble_context(context, blocks, frozenset(b.id for b in blocks)) == context
    for selected in (frozenset(), frozenset({"b0002"}), frozenset({"b0003"})):
        with pytest.raises(ValueError, match="Pinned"):
            assemble_context(context, blocks, selected)


def test_all_pinned_context_and_user_text_with_daemon_like_heading():
    context = "# Review of the previous attempt\ncaller-owned\n# Next\ncaller too\n"
    user_blocks = split_context(context)
    assert assemble_context(context, user_blocks, frozenset()) == ""
    pinned = split_context(
        context, provenance=(ContextSpan(0, len(context), "daemon_review", "review-task"),)
    )
    assert assemble_context(context, pinned, frozenset({"b0000"})) == context
    with pytest.raises(ValueError, match="Pinned"):
        assemble_context(context, pinned, frozenset())


def test_ambiguity_is_local_to_its_user_span():
    first = "# First\n---\n# Still first span\n"
    second = "## Second\nfact\n## Third\nexception\n"
    context = first + second
    blocks = split_context(
        context,
        provenance=(
            ContextSpan(0, len(first), "user", None),
            ContextSpan(len(first), len(context), "user", None),
        ),
    )
    assert assemble_context(context, blocks, frozenset({"b0001"})) == "## Second\nfact\n"
    assert assemble_context(context, blocks, frozenset({"b0002"})) == "## Third\nexception\n"


@pytest.mark.parametrize(
    "ranges",
    [
        (),
        ((1, 6),),
        ((0, 5),),
        ((0, 7),),
        ((-1, 6),),
        ((0, 0), (0, 6)),
        ((0, 2), (3, 6)),
        ((0, 4), (3, 6)),
        ((3, 6), (0, 3)),
        ((False, 6),),
        ((0, 6.0),),
    ],
    ids=[
        "missing",
        "prefix",
        "suffix",
        "past-end",
        "negative",
        "empty",
        "gap",
        "overlap",
        "reordered",
        "boolean",
        "float",
    ],
)
def test_invalid_provenance_and_block_covers_are_rejected_even_when_deleting_everything(ranges):
    context = "abcdef"
    spans = tuple(ContextSpan(start, end, "user", None) for start, end in ranges)
    with pytest.raises(ValueError):
        split_context(context, provenance=spans)
    blocks = tuple(
        ContextBlock(f"b{i:04d}", start, end, "user", None) for i, (start, end) in enumerate(ranges)
    )
    with pytest.raises(ValueError):
        assemble_context(context, blocks, frozenset())


@pytest.mark.parametrize(
    ("origin", "source_task_id"),
    [
        ("unknown", None),
        ("daemon_handoff", None),
        ("daemon_review", ""),
        ("daemon_review", " \t"),
        ("user", 123),
        ("user", ""),
    ],
)
def test_invalid_origins_and_source_ids_cannot_bypass_pinning(origin, source_task_id):
    span = ContextSpan(0, 3, origin, source_task_id)
    with pytest.raises(ValueError):
        split_context("abc", provenance=(span,))
    block = ContextBlock("b0000", 0, 3, origin, source_task_id)
    with pytest.raises(ValueError):
        assemble_context("abc", (block,), frozenset({"b0000"}))


@pytest.mark.parametrize("ids", [("b0000", "b0000"), ("b0001", "b0000"), ("b0000", "custom")])
def test_duplicate_or_noncanonical_block_ids_are_rejected(ids):
    blocks = (
        ContextBlock(ids[0], 0, 1, "user", None),
        ContextBlock(ids[1], 1, 2, "user", None),
    )
    with pytest.raises(ValueError, match="IDs"):
        assemble_context("ab", blocks, frozenset(ids))


@pytest.mark.parametrize("selected", [frozenset({"b9999"}), frozenset({"b0000", "b9999"})])
def test_unknown_selected_ids_are_not_silently_ignored(selected):
    with pytest.raises(ValueError, match="Unknown"):
        assemble_context("abc", split_context("abc"), selected)


def test_duplicate_selected_sequence_is_not_coerced_to_a_set():
    with pytest.raises(ValueError, match="frozenset"):
        assemble_context("abc", split_context("abc"), ["b0000", "b0000"])


def test_empty_source_has_empty_cover_but_nonempty_source_cannot_use_empty_provenance():
    assert split_context("") == ()
    assert split_context("", provenance=()) == ()
    assert assemble_context("", (), frozenset()) == ""
    with pytest.raises(ValueError):
        split_context("abc", provenance=())


def test_segmentation_does_not_merge_sections_to_evade_later_envelope_admission():
    parts = tuple(f"# Section {i}\nfact {i}\n" for i in range(10))
    context = "".join(parts)
    blocks = split_context(context)
    assert assemble_context(context, blocks, frozenset({"b0008", "b0009"})) == parts[8] + parts[9]


def test_public_ranges_are_immutable():
    span = ContextSpan(0, 3, "user", None)
    block = split_context("abc", provenance=(span,))[0]
    with pytest.raises(FrozenInstanceError):
        span.origin = "daemon_review"
    with pytest.raises(FrozenInstanceError):
        block.start = 1
    # A separately constructed corrupted range still cannot enter assembly.
    with pytest.raises(ValueError):
        assemble_context("abc", (replace(block, start=1),), frozenset({"b0000"}))


@pytest.mark.parametrize("operation", ["split", "assemble"])
def test_invalid_utf8_source_is_rejected(operation):
    context = "bad\ud800source"
    with pytest.raises(UnicodeEncodeError):
        if operation == "split":
            split_context(context)
        else:
            assemble_context(
                context,
                (ContextBlock("b0000", 0, len(context), "user", None),),
                frozenset({"b0000"}),
            )
