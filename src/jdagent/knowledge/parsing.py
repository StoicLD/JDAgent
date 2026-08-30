"""Deterministic TXT, Markdown, and CSV parsers with stable locators."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from jdagent.knowledge.decoding import decode_source
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode

LOCATOR_SCHEMA_VERSION = 1
PARSER_VERSION = "v1"
MAX_CSV_FIELD_BYTES = 10 * 1024 * 1024
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE = re.compile(r"^```")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_QUOTE = re.compile(r"^>\s?(.*)$")


class SourceFormat(StrEnum):
    TXT = "txt"
    MARKDOWN = "markdown"
    CSV = "csv"


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    locator: str
    text: str
    kind: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    format: SourceFormat
    encoding: str
    encoding_method: str
    parser_profile: str
    locator_schema_version: int
    blocks: tuple[ParsedBlock, ...]

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "format": self.format.value,
                "parser_profile": self.parser_profile,
                "schema": self.locator_schema_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def block_map(self) -> dict[str, str]:
        return {block.locator: block.text for block in self.blocks}

    def locate(self, locator: str) -> str:
        mapping = self.block_map()
        if locator not in mapping:
            raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, f"Unknown locator: {locator}")
        return mapping[locator]


def parse_source(
    data: bytes,
    *,
    source_format: SourceFormat,
    encoding: str | None = None,
    csv_dialect: str = "excel",
) -> ParsedDocument:
    decoded = decode_source(data, encoding=encoding)
    if source_format is SourceFormat.TXT:
        blocks = _parse_txt(decoded.text)
        profile = f"txt-{PARSER_VERSION}"
    elif source_format is SourceFormat.MARKDOWN:
        blocks = _parse_markdown(decoded.text)
        profile = f"markdown-{PARSER_VERSION}"
    else:
        blocks = _parse_csv(decoded.text, dialect=csv_dialect)
        profile = f"csv-{PARSER_VERSION}"
    return ParsedDocument(
        format=source_format,
        encoding=decoded.encoding,
        encoding_method=decoded.method,
        parser_profile=profile,
        locator_schema_version=LOCATOR_SCHEMA_VERSION,
        blocks=blocks,
    )


def format_from_path(path: str) -> SourceFormat:
    lowered = path.lower()
    if lowered.endswith(".md") or lowered.endswith(".markdown"):
        return SourceFormat.MARKDOWN
    if lowered.endswith(".csv"):
        return SourceFormat.CSV
    if lowered.endswith(".txt"):
        return SourceFormat.TXT
    raise KnowledgeError(
        KnowledgeErrorCode.PARSE_FAILED,
        "Unsupported source format; v0.3 accepts txt, markdown, and csv",
    )


def _parse_txt(text: str) -> tuple[ParsedBlock, ...]:
    parts = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip("\n"))
    blocks: list[ParsedBlock] = []
    for index, part in enumerate(parts):
        stripped = part.strip()
        if not stripped:
            continue
        blocks.append(ParsedBlock(f"txt:p[{index}]", stripped, "paragraph"))
    if not blocks:
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "TXT source is empty")
    return tuple(blocks)


def _parse_markdown(text: str) -> tuple[ParsedBlock, ...]:
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[ParsedBlock] = []
    heading_counts: dict[int, int] = {}
    current_heading: tuple[int, int] | None = None
    counters: dict[str, int] = {}
    index = 0

    def prefix() -> str:
        if current_heading is None:
            return "md:preamble"
        level, heading_index = current_heading
        return f"md:h{level}[{heading_index}]"

    def next_index(kind: str) -> int:
        key = f"{prefix()}/{kind}"
        value = counters.get(key, 0)
        counters[key] = value + 1
        return value

    while index < len(lines):
        line = lines[index]
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_index = heading_counts.get(level, 0)
            heading_counts[level] = heading_index + 1
            current_heading = (level, heading_index)
            blocks.append(ParsedBlock(prefix(), heading.group(2).strip(), "heading"))
            index += 1
            continue
        if _FENCE.match(line):
            index += 1
            body: list[str] = []
            while index < len(lines) and not _FENCE.match(lines[index]):
                body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code_index = next_index("code")
            blocks.append(
                ParsedBlock(f"{prefix()}/code[{code_index}]", "\n".join(body).strip("\n"), "code")
            )
            continue
        if "|" in line and _looks_like_table(lines, index):
            table_lines, index = _consume_table(lines, index)
            table_index = next_index("table")
            blocks.extend(_table_cells(prefix(), table_index, table_lines))
            continue
        list_match = _LIST.match(line)
        if list_match:
            item_index = next_index("li")
            blocks.append(
                ParsedBlock(
                    f"{prefix()}/li[{item_index}]", list_match.group(1).strip(), "list_item"
                )
            )
            index += 1
            continue
        quote_match = _QUOTE.match(line)
        if quote_match:
            quote_index = next_index("quote")
            blocks.append(
                ParsedBlock(
                    f"{prefix()}/quote[{quote_index}]",
                    quote_match.group(1).strip(),
                    "quote",
                )
            )
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        paragraph: list[str] = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _starts_block(lines[index]):
            paragraph.append(lines[index])
            index += 1
        para_index = next_index("p")
        blocks.append(
            ParsedBlock(f"{prefix()}/p[{para_index}]", "\n".join(paragraph).strip(), "paragraph")
        )
    if not blocks:
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "Markdown source is empty")
    return tuple(blocks)


def _starts_block(line: str) -> bool:
    return bool(
        _HEADING.match(line)
        or _FENCE.match(line)
        or _LIST.match(line)
        or _QUOTE.match(line)
        or ("|" in line)
    )


def _looks_like_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return "|" in lines[index] and (
        _TABLE_SEP.match(lines[index + 1]) is not None or "|" in lines[index + 1]
    )


def _consume_table(lines: list[str], index: int) -> tuple[list[str], int]:
    collected: list[str] = []
    while index < len(lines) and "|" in lines[index]:
        collected.append(lines[index])
        index += 1
    return collected, index


def _table_cells(prefix: str, table_index: str | int, table_lines: list[str]) -> list[ParsedBlock]:
    rows: list[list[str]] = []
    for line in table_lines:
        if _TABLE_SEP.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    blocks: list[ParsedBlock] = []
    for row_number, cells in enumerate(rows, start=1):
        for column_number, cell in enumerate(cells, start=1):
            locator = f"{prefix}/table[{table_index}]/r{row_number}c{column_number}"
            blocks.append(ParsedBlock(locator, cell, "table_cell"))
    return blocks


def _parse_csv(text: str, *, dialect: str) -> tuple[ParsedBlock, ...]:
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_CSV_FIELD_BYTES)
    try:
        reader = csv.reader(io.StringIO(text), dialect=dialect)
        rows = list(reader)
    except csv.Error as error:
        message = str(error).lower()
        if "field larger than field limit" in message or "field size" in message:
            raise KnowledgeError(
                KnowledgeErrorCode.CSV_FIELD_TOO_LARGE,
                "CSV field exceeds 10 MiB",
            ) from error
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "CSV parsing failed") from error
    finally:
        csv.field_size_limit(previous_limit)
    if not rows:
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "CSV source is empty")
    header = [cell.strip() for cell in rows[0]]
    if not header or not any(header) or all(not cell for cell in header):
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "CSV header is required")
    if any(not name for name in header):
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "CSV header contains an empty column")
    blocks: list[ParsedBlock] = []
    for row_number, row in enumerate(rows[1:], start=1):
        if len(row) > len(header):
            raise KnowledgeError(
                KnowledgeErrorCode.PARSE_FAILED,
                f"CSV row {row_number} has more fields than the header",
            )
        padded = row + [""] * (len(header) - len(row))
        for column_number, (name, value) in enumerate(zip(header, padded, strict=True), start=1):
            encoded = value.encode("utf-8")
            if len(encoded) > MAX_CSV_FIELD_BYTES:
                raise KnowledgeError(
                    KnowledgeErrorCode.CSV_FIELD_TOO_LARGE,
                    f"CSV field exceeds 10 MiB at r{row_number}:c{column_number}",
                )
            locator = f"csv:r{row_number}:c{column_number}"
            display = f"{name}: {value}"
            blocks.append(ParsedBlock(locator, display, "csv_field"))
    if len(blocks) == 0:
        raise KnowledgeError(KnowledgeErrorCode.PARSE_FAILED, "CSV source has no data rows")
    return tuple(blocks)
