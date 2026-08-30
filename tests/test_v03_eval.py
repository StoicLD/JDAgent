import asyncio
from pathlib import Path

from jdagent.knowledge.eval_gold import (
    fake_hybrid_drop,
    parser_locator_failures,
    run_retrieval_eval,
    write_failures,
)


def test_parser_locator_roundtrip_is_complete() -> None:
    assert parser_locator_failures() == ()


def test_offline_child_and_ptk_eval_meet_gates(tmp_path: Path) -> None:
    async def scenario() -> None:
        child, ptk = await run_retrieval_eval(tmp_path)
        lexical = tuple(item for item in child if item.metric == "recall@10")
        write_failures(
            tmp_path / "failures" / "child.jsonl",
            "child_retrieval",
            tuple(item for item in lexical if item.actual < 1.0),
        )
        write_failures(
            tmp_path / "failures" / "ptk.jsonl",
            "final_ptk",
            tuple(item for item in ptk if item.actual < 1.0),
        )
        child_recall = sum(item.actual for item in lexical) / len(lexical)
        ptk_coverage = sum(item.actual for item in ptk) / len(ptk)
        zh = [item.actual for item in lexical if item.slice == "zh"]
        en = [item.actual for item in lexical if item.slice == "en"]
        csv = [item.actual for item in lexical if item.slice == "csv"]
        assert child_recall >= 0.85
        assert ptk_coverage >= 0.85
        if zh:
            assert sum(zh) / len(zh) >= 0.75
        if en:
            assert sum(en) / len(en) >= 0.75
        if csv:
            assert sum(csv) / len(csv) >= 0.75
        drop = fake_hybrid_drop(child)
        assert drop <= 1.0

    asyncio.run(scenario())
