"""Verified provider capacity profiles; custom endpoints require explicit capacity."""

from urllib.parse import urlparse

from jdagent.domain.json import JsonObject, require_object

SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
CHECKED_ON = "2026-10-22"
DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"})


def maintenance_options(provider: str, options: JsonObject | None = None) -> JsonObject:
    """Use the provider's JSON mode for tool-free structured maintenance responses."""
    result = dict(options or {})
    if provider == "deepseek":
        result["deepseek"] = {
            **require_object(result.get("deepseek", {}), "deepseek options"),
            "response_format": {"type": "json_object"},
        }
    return result


def model_window(provider: str, model: str, base_url: str, explicit: int | None) -> int | None:
    if explicit is not None:
        return explicit
    if provider == "fake":
        return 64000
    if (
        provider == "deepseek"
        and urlparse(base_url).hostname == "api.deepseek.com"
        and model in DEEPSEEK_MODELS
    ):
        # Official notation is 1M. Decimal 1,000,000 is the conservative admission value.
        return 1_000_000
    return None
