"""统一估算模型输入的用量，供上下文报告、压缩和发送前检查使用。"""

import json
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from math import ceil

from jdagent.domain.model import MessageRole, ModelRequest, Usage


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """根据模型容量、应用上限、预留输出和安全余量计算本次请求可用的输入额度。"""

    model_window: int | None
    application_window: int | None = None
    output_tokens: int = 4096
    safety_tokens: int | None = None
    provider_input_limit: int | None = None
    trigger_ratio: float = 0.9
    target_ratio: float = 0.4

    def __post_init__(self) -> None:
        if self.model_window is None:
            raise ValueError("model_context_window must be configured for this model")
        for name, value in (
            ("model_context_window", self.model_window),
            ("max_context_tokens", self.application_window),
            ("provider_input_limit", self.provider_input_limit),
        ):
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        if self.output_tokens < 0 or self.safety < 0:
            raise ValueError("Output reservation and safety margin cannot be negative")
        if not 0 < self.target_ratio < self.trigger_ratio < 1:
            raise ValueError("Context ratios must satisfy 0 < target < trigger < 1")
        if self.input_limit <= 0:
            raise ValueError("Context window cannot hold output reservation and safety margin")

    @property
    def window(self) -> int:
        assert self.model_window is not None
        return min(self.model_window, self.application_window or self.model_window)

    @property
    def safety(self) -> int:
        return (
            self.safety_tokens
            if self.safety_tokens is not None
            else max(1024, ceil(self.window * 0.05))
        )

    @property
    def input_limit(self) -> int:
        available = self.window - self.output_tokens - self.safety
        return min(available, self.provider_input_limit or available)

    def should_compact(self, tokens: int) -> bool:
        return tokens >= self.input_limit * self.trigger_ratio

    def reached_target(self, tokens: int) -> bool:
        return tokens < self.input_limit * self.target_ratio


@dataclass(frozen=True, slots=True)
class InputEstimate:
    """本地对请求输入量的估算，分项记录各类内容的占用与估算方法。"""

    input_tokens: int
    categories: tuple[tuple[str, int], ...]
    method: str
    proxy_bytes: int


class InputEstimator:
    """按服务商和模型，用已报告的输入用量校准本地估算，使容量检查和上下文压缩使用同一尺度。"""

    def __init__(self) -> None:
        self._samples: dict[tuple[str, str], deque[Fraction]] = {}
        # 容量被拒绝后提高的估算下限只在当前轮次生效；普通校准样本可以跨轮复用。
        self._floors: dict[tuple[str, str, str], Fraction] = {}

    @staticmethod
    def _key(request: ModelRequest) -> tuple[str, str]:
        return request.metadata.get("provider", ""), request.model

    @staticmethod
    def _proxy(request: ModelRequest) -> dict[str, int]:
        """按指令、消息、工具定义等类别估算未校准的输入用量，并计入协议开销。"""
        values: dict[str, int] = dict.fromkeys(
            (
                "instructions",
                "tools",
                "user",
                "assistant_tools",
                "summaries",
                "memory",
                "knowledge",
                "protocol",
            ),
            0,
        )
        for part in request.system_parts:
            category = {
                "history_summary": "summaries",
                "memory_evidence": "memory",
                "untrusted_evidence": "knowledge",
                "protected_user": "user",
            }.get(part.source, "instructions")
            values[category] += len(part.content.encode("utf-8"))
        for message in request.messages:
            category = "user" if message.role is MessageRole.USER else "assistant_tools"
            values[category] += len(message.content.encode("utf-8"))
            for call in message.tool_calls:
                values[category] += len(
                    json.dumps(
                        {"id": call.call_id, "name": call.name, "arguments": call.arguments},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            values["protocol"] += 32 + len(
                ((message.name or "") + (message.tool_call_id or "")).encode("utf-8")
            )
        values["tools"] = (
            len(
                json.dumps(
                    [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        }
                        for tool in request.tools
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if request.tools
            else 0
        )
        values["protocol"] += 32 + 16 * len(request.system_parts)
        return values

    def measure(self, request: ModelRequest) -> InputEstimate:
        """计算请求的输入用量。预留的输出空间由窗口预算另外扣除。"""
        values = self._proxy(request)
        samples = self._samples.get(self._key(request))
        coefficient = max(samples) if samples else Fraction(1)
        floor_key = (*self._key(request), request.metadata.get("turn_id", ""))
        coefficient = max(coefficient, self._floors.get(floor_key, Fraction(0)))
        total = sum(values.values())
        tokens = ceil(coefficient * total)
        categories = {name: int(value * coefficient) for name, value in values.items()}
        categories["protocol"] += tokens - sum(categories.values())
        return InputEstimate(
            tokens,
            tuple(categories.items()),
            "usage_calibrated" if samples else "uncalibrated",
            total,
        )

    def observe(self, request: ModelRequest, usage: Usage) -> None:
        """输入用量大于零时，记录实际用量与本地估算的比值，供同一服务商和模型的后续请求使用。"""
        if usage.input_tokens <= 0:
            return
        samples = self._samples.setdefault(self._key(request), deque(maxlen=20))
        samples.append(Fraction(usage.input_tokens, sum(self._proxy(request).values())))

    def reject(self, request: ModelRequest) -> None:
        """在服务商拒绝过长请求后提高该请求的估算下限，供本轮重新选择上下文。"""
        samples = self._samples.get(self._key(request))
        key = (*self._key(request), request.metadata.get("turn_id", ""))
        current = max(max(samples) if samples else Fraction(1), self._floors.get(key, Fraction(0)))
        self._floors[key] = current * Fraction(5, 4)
