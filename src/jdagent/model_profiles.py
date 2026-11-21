"""定义已知模型的容量；自定义端点需明确提供容量信息。"""

from urllib.parse import urlparse

from jdagent.domain.json import JsonObject, require_object

SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
CHECKED_ON = "2026-10-22"
DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"})


def maintenance_options(provider: str, options: JsonObject | None = None) -> JsonObject:
    """为无需调用工具的后台结构化任务选择服务商支持的 JSON 输出设置。"""
    result = dict(options or {})
    if provider == "deepseek":
        result["deepseek"] = {
            **require_object(result.get("deepseek", {}), "deepseek options"),
            "response_format": {"type": "json_object"},
        }
    return result


def model_window(provider: str, model: str, base_url: str, explicit: int | None) -> int | None:
    """根据显式配置或已知模型档案确定容量，使用自定义端点时需要明确的窗口大小。"""
    if explicit is not None:
        return explicit
    if provider == "fake":
        return 64000
    if (
        provider == "deepseek"
        and urlparse(base_url).hostname == "api.deepseek.com"
        and model in DEEPSEEK_MODELS
    ):
        # 服务商以 1M 标注容量，这里按十进制的一百万计算可接收的输入量。
        return 1_000_000
    return None
