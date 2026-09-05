from typing import cast

from jdagent.domain.json import JsonObject
from jdagent.knowledge.eval_answer import median, score_citations


def test_median_uses_the_middle_of_three_runs() -> None:
    assert median((0.9, 0.1, 0.5)) == 0.5


def test_citation_metrics_cover_precision_completeness_and_unanswerable() -> None:
    annotation = cast(
        JsonObject,
        {
            "unanswerable": False,
            "gold_locators": [{"locator": "md:h2[0]/p[0]", "required": True}],
            "claims": [{"must_cite": True, "evidence_locator": "md:h2[0]/p[0]"}],
        },
    )
    ok = score_citations(annotation, ("md:h2[0]/p[0]",), protocol_failed=False)
    assert ok.precision == 1.0
    assert ok.completeness == 1.0
    assert ok.unsupported_rate == 0.0

    wrong = score_citations(annotation, ("txt:p[9]",), protocol_failed=False)
    assert wrong.precision == 0.0
    assert wrong.completeness == 0.0

    failed = score_citations(annotation, ("md:h2[0]/p[0]",), protocol_failed=True)
    assert failed.precision == 0.0
    assert failed.completeness == 0.0

    unanswerable = cast(
        JsonObject,
        {"unanswerable": True, "claims": [], "gold_locators": []},
    )
    unsupported = score_citations(
        unanswerable,
        ("md:h2[0]/p[0]",),
        protocol_failed=False,
    )
    assert unsupported.unsupported_rate == 1.0
    assert unsupported.precision == 0.0


def test_citation_eval_rejects_the_right_locator_from_the_wrong_source() -> None:
    annotation = cast(
        JsonObject,
        {
            "unanswerable": False,
            "gold_locators": [{"source_key": "policy", "locator": "txt:p[0]", "required": True}],
            "claims": [
                {"must_cite": True, "evidence_source_key": "policy", "evidence_locator": "txt:p[0]"}
            ],
        },
    )
    wrong = score_citations(annotation, ("txt:p[0]",), protocol_failed=False)
    assert wrong.precision == 0.0
    assert wrong.completeness == 0.0
    unrelated = score_citations(
        annotation, ("txt:p[0]",), protocol_failed=False, cited_source_keys=("unrelated",)
    )
    assert unrelated.precision == 0.0
    assert unrelated.completeness == 0.0
    correct = score_citations(
        annotation, ("txt:p[0]",), protocol_failed=False, cited_source_keys=("policy",)
    )
    assert correct.precision == 1.0
    assert correct.completeness == 1.0
