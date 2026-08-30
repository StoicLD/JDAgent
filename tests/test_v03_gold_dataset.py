"""Offline validation of the v0.3 gold corpus schema and file inventory."""

from __future__ import annotations

import json
from pathlib import Path

GOLD_ROOT = Path(__file__).resolve().parents[1] / "testdata" / "v0.3-gold"
REQUIRED_SLICES = {"zh", "en", "csv", "mixed", "multikb", "negative", "injection", "lifecycle"}
REQUIRED_SOURCES = {
    "zh-leave-policy.md",
    "en-expense-handbook.txt",
    "mixed-faq.md",
    "employees.csv",
    "overlap-a.txt",
    "overlap-b.txt",
    "injection.md",
    "finance-stipend.md",
}
REQUIRED_ENCODINGS = {
    "utf8.txt",
    "utf8-bom.txt",
    "utf16-le.txt",
    "gb18030.txt",
    "replacement.txt",
    "corrupt.bin",
    "ambiguous.bin",
}


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        loaded = json.loads(line)
        if not isinstance(loaded, dict):
            raise AssertionError(f"{path.name}:{line_number} must be a JSON object")
        rows.append(loaded)
    return rows


def test_gold_corpus_inventory_and_query_annotation_join() -> None:
    queries = _load_jsonl(GOLD_ROOT / "queries.jsonl")
    annotations = _load_jsonl(GOLD_ROOT / "annotations.jsonl")
    source_names = {path.name for path in (GOLD_ROOT / "sources").iterdir() if path.is_file()}
    encoding_names = {
        path.name for path in (GOLD_ROOT / "encodings").iterdir() if path.is_file() and path.name != "README.md"
    }

    assert REQUIRED_SOURCES <= source_names
    assert REQUIRED_ENCODINGS <= encoding_names
    assert (GOLD_ROOT / "DATASET.md").is_file()
    assert (GOLD_ROOT / "BASELINE.md").is_file()

    query_ids = [row["query_id"] for row in queries]
    annotation_ids = [row["query_id"] for row in annotations]
    assert query_ids == annotation_ids
    assert len(set(query_ids)) == len(query_ids)

    slices = {row["slice"] for row in queries}
    assert REQUIRED_SLICES <= slices

    for query, annotation in zip(queries, annotations, strict=True):
        assert isinstance(query["text"], str) and query["text"].strip()
        assert isinstance(query["answerable"], bool)
        assert query["answerable"] is not annotation["unanswerable"]
        assert isinstance(annotation["gold_locators"], list)
        assert isinstance(annotation["claims"], list)
        if query["answerable"] is False:
            assert annotation["gold_locators"] == []
            assert annotation["claims"] == []
        else:
            assert annotation["gold_locators"] or query["slice"] == "lifecycle"
