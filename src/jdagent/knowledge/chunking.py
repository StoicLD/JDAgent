"""Structure-aware parent/child chunking with stable locators."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from jdagent.knowledge.parsing import ParsedBlock, ParsedDocument, SourceFormat

DEFAULT_CHILD_CHARS = 800
FORCED_OVERLAP_CHARS = 80


@dataclass(frozen=True, slots=True)
class ChildChunk:
    chunk_id: str
    parent_id: str
    locator: str
    text: str
    overlapped: bool


@dataclass(frozen=True, slots=True)
class ParentSegment:
    parent_id: str
    locator: str
    text: str
    children: tuple[ChildChunk, ...]


def chunk_document(
    document: ParsedDocument,
    *,
    max_child_chars: int = DEFAULT_CHILD_CHARS,
    overlap_chars: int = FORCED_OVERLAP_CHARS,
) -> tuple[ParentSegment, ...]:
    groups = _group_parents(document)
    parents: list[ParentSegment] = []
    for parent_locator, blocks in groups:
        parent_text = "\n".join(block.text for block in blocks if block.kind != "heading")
        if not parent_text.strip():
            parent_text = "\n".join(block.text for block in blocks)
        parent_id = _stable_id("par", document.parser_profile, parent_locator, parent_text)
        children = _children_for(
            document,
            parent_id,
            parent_locator,
            blocks,
            parent_text,
            max_child_chars,
            overlap_chars,
        )
        parents.append(ParentSegment(parent_id, parent_locator, parent_text, children))
    return tuple(parents)


def _group_parents(document: ParsedDocument) -> list[tuple[str, tuple[ParsedBlock, ...]]]:
    if document.format is SourceFormat.CSV:
        rows: dict[str, list[ParsedBlock]] = {}
        order: list[str] = []
        for block in document.blocks:
            row_key = block.locator.rsplit(":", 1)[0]
            if row_key not in rows:
                rows[row_key] = []
                order.append(row_key)
            rows[row_key].append(block)
        return [(key, tuple(rows[key])) for key in order]
    if document.format is SourceFormat.TXT:
        return [(block.locator, (block,)) for block in document.blocks]

    groups: list[tuple[str, list[ParsedBlock]]] = []
    current_key: str | None = None
    current: list[ParsedBlock] = []
    for block in document.blocks:
        key = block.locator.split("/p[", 1)[0]
        key = key.split("/code[", 1)[0]
        key = key.split("/li[", 1)[0]
        key = key.split("/quote[", 1)[0]
        key = key.split("/table[", 1)[0]
        if current_key is None:
            current_key = key
        if key != current_key:
            groups.append((current_key, current))
            current_key = key
            current = []
        current.append(block)
    if current_key is not None:
        groups.append((current_key, current))
    return [(key, tuple(blocks)) for key, blocks in groups]


def _children_for(
    document: ParsedDocument,
    parent_id: str,
    parent_locator: str,
    blocks: tuple[ParsedBlock, ...],
    parent_text: str,
    max_child_chars: int,
    overlap_chars: int,
) -> tuple[ChildChunk, ...]:
    structural = [
        block
        for block in blocks
        if block.kind in {"paragraph", "list_item", "quote", "code", "csv_field", "table_cell"}
    ]
    if not structural:
        structural = list(blocks)
    children: list[ChildChunk] = []
    for block in structural:
        if len(block.text) <= max_child_chars:
            children.append(_child(parent_id, block.locator, block.text, overlapped=False))
            continue
        children.extend(
            _forced_split(parent_id, block.locator, block.text, max_child_chars, overlap_chars)
        )
    if not children:
        children.append(_child(parent_id, parent_locator, parent_text, overlapped=False))
    return tuple(children)


def _forced_split(
    parent_id: str,
    locator: str,
    text: str,
    max_child_chars: int,
    overlap_chars: int,
) -> list[ChildChunk]:
    chunks: list[ChildChunk] = []
    start = 0
    part = 0
    while start < len(text):
        end = min(len(text), start + max_child_chars)
        piece = text[start:end]
        child_locator = f"{locator}#s{part}"
        chunks.append(_child(parent_id, child_locator, piece, overlapped=start > 0))
        if end == len(text):
            break
        start = max(end - overlap_chars, start + 1)
        part += 1
    return chunks


def _child(parent_id: str, locator: str, text: str, *, overlapped: bool) -> ChildChunk:
    chunk_id = _stable_id("chk", parent_id, locator, text)
    return ChildChunk(chunk_id, parent_id, locator, text, overlapped)


def _stable_id(*parts: str) -> str:
    payload = "\u001f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]
