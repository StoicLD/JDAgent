"""Answer and citation eval scoring. Live DeepSeek runs stay opt-in."""

from __future__ import annotations

from dataclasses import dataclass

from jdagent.domain.json import JsonObject
from jdagent.knowledge.eval_gold import locator_covers


@dataclass(frozen=True, slots=True)
class CitationMetrics:
    precision: float
    completeness: float
    unsupported_rate: float


def median(values: tuple[float, ...]) -> float:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = tuple(sorted(values))
    return ordered[len(ordered) // 2]


def score_citations(
    annotation: JsonObject,
    cited_locators: tuple[str, ...],
    *,
    protocol_failed: bool,
) -> CitationMetrics:
    if protocol_failed:
        unanswerable = annotation.get("unanswerable") is True
        return CitationMetrics(0.0, 0.0, 1.0 if unanswerable else 0.0)
    gold = _gold_locators(annotation)
    if cited_locators:
        matched = sum(
            1 for locator in cited_locators if any(locator_covers(item, locator) for item in gold)
        )
        precision = matched / len(cited_locators)
    else:
        precision = 1.0
    must_cite = _must_cite(annotation)
    if must_cite:
        covered = sum(
            1
            for locator in must_cite
            if any(locator_covers(locator, cited) for cited in cited_locators)
        )
        completeness = covered / len(must_cite)
    else:
        completeness = 1.0
    unanswerable = annotation.get("unanswerable") is True
    unsupported = 1.0 if unanswerable and cited_locators else 0.0
    return CitationMetrics(precision, completeness, unsupported)


def _gold_locators(annotation: JsonObject) -> tuple[str, ...]:
    gold = annotation.get("gold_locators")
    locators: list[str] = []
    if not isinstance(gold, list):
        return ()
    for item in gold:
        if isinstance(item, dict):
            locators.append(str(item.get("locator")))
    return tuple(locators)


def _must_cite(annotation: JsonObject) -> tuple[str, ...]:
    claims = annotation.get("claims")
    locators: list[str] = []
    if not isinstance(claims, list):
        return ()
    for item in claims:
        if not isinstance(item, dict):
            continue
        if item.get("must_cite") is True:
            locators.append(str(item.get("evidence_locator")))
    return tuple(locators)
