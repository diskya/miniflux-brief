"""Render the narrow slice of markdown the summarizers emit as safe HTML.

The prompt asks for headings, lists, a rule and short paragraphs, so a full
markdown parser would be more surface than this needs. Everything is escaped
first, which keeps the output safe to hand to Miniflux and to the reading page.
"""

from __future__ import annotations

import html
import re


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", re.S)
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_UNORDERED = re.compile(r"^\s*[-*+]\s+(.*)$")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    out = _LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noopener">{m.group(1)}</a>', out)
    return out


def markdown_to_html(markdown: str) -> str:
    """Convert summarizer markdown into a small, predictable set of tags."""

    lines = markdown.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    paragraph: list[str] = []
    list_tag: str | None = None

    def close_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            parts.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = None

    def open_list(tag: str) -> None:
        nonlocal list_tag
        if list_tag != tag:
            close_list()
            parts.append(f"<{tag}>")
            list_tag = tag

    for line in lines:
        if not line.strip():
            close_paragraph()
            close_list()
            continue

        if _RULE.match(line):
            close_paragraph()
            close_list()
            parts.append("<hr>")
            continue

        heading = _HEADING.match(line)
        if heading:
            close_paragraph()
            close_list()
            # Entry content sits under the page's own h1/h2, so start at h3.
            level = min(6, len(heading.group(1)) + 2)
            parts.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue

        ordered = _ORDERED.match(line)
        if ordered:
            close_paragraph()
            open_list("ol")
            parts.append(f"<li>{_inline(ordered.group(1).strip())}</li>")
            continue

        unordered = _UNORDERED.match(line)
        if unordered:
            close_paragraph()
            open_list("ul")
            parts.append(f"<li>{_inline(unordered.group(1).strip())}</li>")
            continue

        close_list()
        paragraph.append(line.strip())

    close_paragraph()
    close_list()
    return "".join(parts)
