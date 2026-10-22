import pytest

from jdagent.context_budget import ContextBudget, InputEstimator
from jdagent.domain.model import MessageRole, ModelMessage, ModelRequest, ModelSettings, Usage


def test_input_budget_reserves_output_once_and_keeps_independent_input_limit() -> None:
    budget = ContextBudget(
        model_window=1000,
        application_window=800,
        output_tokens=100,
        safety_tokens=50,
        provider_input_limit=700,
    )
    assert budget.input_limit == 650
    assert budget.should_compact(585)
    assert not budget.should_compact(584)
    assert budget.reached_target(259)
    assert not budget.reached_target(260)


def test_usage_calibrates_the_same_estimate_used_for_categories_and_limits() -> None:
    estimator = InputEstimator()
    request = ModelRequest(
        "test",
        (ModelMessage(MessageRole.USER, "hello"),),
        settings=ModelSettings(max_output_tokens=100),
    )
    cold = estimator.measure(request)
    estimator.observe(request, Usage(input_tokens=10, output_tokens=200))
    warm = estimator.measure(request)
    assert warm.input_tokens == 10
    assert sum(value for _, value in warm.categories) == 10
    assert warm.input_tokens < cold.input_tokens
    assert warm.method == "usage_calibrated"
    other = ModelRequest("different-model", request.messages, settings=request.settings)
    assert estimator.measure(other).method == "uncalibrated"


def test_missing_model_capacity_is_not_inferred_from_application_limit() -> None:
    with pytest.raises(ValueError, match="model_context_window"):
        ContextBudget(model_window=None, application_window=64000, output_tokens=4096)
