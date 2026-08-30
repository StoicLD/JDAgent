from jdagent.domain.events import CitationRecord
from jdagent.knowledge.citation import evidence_system_part, finalize_answer
from jdagent.knowledge.ptk import (
    Evidence,
    EvidenceBudget,
    PreparedTurnKnowledge,
    RetrievalOutcome,
)


def _ptk() -> PreparedTurnKnowledge:
    evidence = Evidence(
        "ev_1",
        "K:tok:E1",
        "kb",
        "src",
        "sv",
        "snap",
        "md:h2[0]/p[0]",
        1,
        "hash",
        "年假 15 天",
        4,
        1,
    )
    return PreparedTurnKnowledge(
        "turn-1",
        "tok",
        RetrievalOutcome.COMPLETE,
        "fp",
        (),
        (evidence,),
        EvidenceBudget(20, 20, 1, 40, 1, 4, 6000, 8),
        (),
        "ret",
    )


def test_finalize_rewrites_machine_references_and_rejects_unknown() -> None:
    knowledge = _ptk()
    ok = finalize_answer("Leave is 15 days K:tok:E1.", knowledge)
    assert ok.illegal is False
    assert ok.display_text == "Leave is 15 days [1]."
    assert ok.citations == (
        CitationRecord(1, "ev_1", "kb", "src", "sv", "snap", "md:h2[0]/p[0]", 1, "hash"),
    )
    bad = finalize_answer("See K:other:E1 and [1].", knowledge)
    assert bad.illegal is True


def test_evidence_prompt_requires_exact_machine_references() -> None:
    prompt = evidence_system_part(_ptk())
    assert "K:tok:E1" in prompt
    assert "machine reference" in prompt
    assert "[1]" in prompt
