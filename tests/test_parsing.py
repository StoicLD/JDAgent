from pathlib import Path

import pytest

from jdagent.knowledge.chunking import chunk_document
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.parsing import SourceFormat, parse_source

GOLD = Path(__file__).resolve().parents[1] / "testdata" / "v0.3-gold" / "sources"


def test_txt_paragraph_locators_round_trip() -> None:
    parsed = parse_source(
        (GOLD / "en-expense-handbook.txt").read_bytes(),
        source_format=SourceFormat.TXT,
    )
    assert parsed.locate("txt:p[1]").startswith("Receipts")
    assert "25 US dollars" in parsed.locate("txt:p[1]")
    assert "180 US dollars" in parsed.locate("txt:p[2]")
    assert "14 days" in parsed.locate("txt:p[3]")
    rebuilt = parse_source(
        (GOLD / "en-expense-handbook.txt").read_bytes(),
        source_format=SourceFormat.TXT,
    )
    assert parsed.block_map() == rebuilt.block_map()


def test_markdown_headings_lists_code_and_tables() -> None:
    parsed = parse_source(
        (GOLD / "zh-leave-policy.md").read_bytes(),
        source_format=SourceFormat.MARKDOWN,
    )
    assert "15 天" in parsed.locate("md:h2[0]/p[0]")
    assert "医院证明" in parsed.locate("md:h2[1]/p[0]")
    faq = parse_source((GOLD / "mixed-faq.md").read_bytes(), source_format=SourceFormat.MARKDOWN)
    assert "vpn.example.internal" in faq.locate("md:h2[0]/p[0]")
    assert "uv run pytest" in faq.locate("md:h2[1]/code[0]")
    assert faq.locate("md:h2[2]/table[0]/r3c2") == "Bo"
    assert "09:00" in faq.locate("md:h2[2]/li[0]")
    stipend = parse_source(
        (GOLD / "finance-stipend.md").read_bytes(),
        source_format=SourceFormat.MARKDOWN,
    )
    assert "80 CNY" in stipend.locate("md:h1[0]/p[0]")


def test_csv_multiline_fields_and_cell_locators() -> None:
    parsed = parse_source((GOLD / "employees.csv").read_bytes(), source_format=SourceFormat.CSV)
    assert parsed.locate("csv:r1:c3") == "city: Beijing"
    assert parsed.locate("csv:r2:c4") == "role: Analyst"
    assert "retrieval" in parsed.locate("csv:r1:c5")
    assert parsed.locate("csv:r3:c3") == "city: Beijing"


def test_csv_without_header_and_oversized_field_fail() -> None:
    with pytest.raises(KnowledgeError) as missing:
        parse_source(b"only,one,line\n", source_format=SourceFormat.CSV)
    assert missing.value.code is KnowledgeErrorCode.PARSE_FAILED
    huge = "h\n" + "a" * (10 * 1024 * 1024 + 1)
    with pytest.raises(KnowledgeError) as oversized:
        parse_source(huge.encode("utf-8"), source_format=SourceFormat.CSV)
    assert oversized.value.code is KnowledgeErrorCode.CSV_FIELD_TOO_LARGE


def test_parent_child_chunking_is_stable_and_structure_complete() -> None:
    parsed = parse_source(
        (GOLD / "zh-leave-policy.md").read_bytes(),
        source_format=SourceFormat.MARKDOWN,
    )
    first = chunk_document(parsed)
    second = chunk_document(parsed)
    assert [parent.parent_id for parent in first] == [parent.parent_id for parent in second]
    leave = next(parent for parent in first if parent.locator == "md:h2[0]")
    assert leave.children[0].overlapped is False
    assert "15 天" in leave.children[0].text
    long_text = ("段落。" * 400).encode("utf-8")
    forced = chunk_document(
        parse_source(long_text, source_format=SourceFormat.TXT),
        max_child_chars=40,
        overlap_chars=8,
    )
    assert any(child.overlapped for child in forced[0].children)
    assert all(len(child.text) <= 40 for child in forced[0].children)
